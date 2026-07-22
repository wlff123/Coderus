#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
RUN_USER="${CODERUS_RUN_USER:-coderus}"
LOCK_FILE="$ROOT/data/release.lock"
if [[ "${CODERUS_ALLOW_ANY_USER:-0}" != "1" && "$(id -un)" != "$RUN_USER" ]]; then
  echo "Coderus bootstrap must run as $RUN_USER" >&2
  exit 1
fi
[[ ! -e "$ROOT/current" ]] || { echo "Release layout already initialized"; exit 0; }
[[ -d "$ROOT/coderus" && -x "$ROOT/.venv/bin/python" ]] || {
  echo "Existing Coderus runtime is incomplete" >&2
  exit 1
}

mkdir -p "$ROOT/data"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another release operation is running" >&2; exit 1; }

mkdir -p "$ROOT/releases" "$ROOT/incoming" "$ROOT/validation" "$ROOT/backups" "$ROOT/bootstrap"
install -m 0444 "$ROOT/coderus/release_bootstrap.py" \
  "$ROOT/bootstrap/release-bootstrap.py"
digest="$({ find "$ROOT/coderus" -type f -print0; printf '%s\0' "$ROOT/pyproject.toml" "$ROOT/uv.lock"; } \
  | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-8)"
RELEASE_ID="$(date -u +%Y%m%d-%H%M%S)-$digest"
RELEASE="$ROOT/releases/$RELEASE_ID"
mkdir "$RELEASE"

for item in coderus tests scripts pyproject.toml uv.lock README.md LICENSE config.example.yaml; do
  [[ -e "$ROOT/$item" ]] && cp -a "$ROOT/$item" "$RELEASE/$item"
done
cp -a "$ROOT/.venv" "$RELEASE/.venv"
cat >"$RELEASE/release.json" <<EOF
{"release_id":"$RELEASE_ID","created_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","bootstrap":true}
EOF
touch "$RELEASE/VERIFIED"
touch "$RELEASE/LEGACY_RUNTIME"
touch "$RELEASE/LEGACY_ROOT_CWD"
chmod 0444 "$RELEASE/VERIFIED"
ln -s "releases/$RELEASE_ID" "$ROOT/current"
ln -s "releases/$RELEASE_ID" "$ROOT/previous"
echo "$RELEASE_ID"
