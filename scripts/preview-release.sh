#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
DATABASE="${CODERUS_DATABASE:-$ROOT/data/coderus.db}"
PUBLIC_KEY="${CODERUS_RELEASE_PUBLIC_KEY:-$ROOT/release-public-key.pem}"
PREVIEW_PORT="${CODERUS_PREVIEW_PORT:-18084}"
[[ $# -eq 1 ]] || { echo "Usage: $0 <release-id>" >&2; exit 2; }
RELEASE_ID="$1"
[[ "$RELEASE_ID" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$ ]] || {
  echo "Invalid release id" >&2
  exit 2
}
RELEASE="$ROOT/releases/$RELEASE_ID"
VALIDATION="$ROOT/validation/$RELEASE_ID"
PYTHON="$RELEASE/.venv/bin/python"
READY_URL="http://127.0.0.1:${PREVIEW_PORT}/readyz"
LOG_FILE="$VALIDATION/preview.log"
PID_FILE="$VALIDATION/preview.pid"

[[ -d "$RELEASE" ]] || { echo "Release not installed: $RELEASE_ID" >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Release environment missing: $PYTHON" >&2; exit 1; }
[[ -f "$RELEASE/release.json" ]] || { echo "release.json missing" >&2; exit 1; }
[[ ! -e "$RELEASE/VERIFIED" ]] || { echo "Release already verified"; exit 0; }

mkdir -p "$ROOT/validation"
if [[ -e "$VALIDATION" ]]; then
  resolved="$(realpath "$VALIDATION")"
  [[ "$resolved" == "$ROOT/validation/$RELEASE_ID" ]] || {
    echo "Unsafe validation path: $resolved" >&2
    exit 1
  }
  rm -rf -- "$resolved"
fi
mkdir -p "$VALIDATION/workspaces" "$VALIDATION/artifacts"

cd "$RELEASE"
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON" -m coderus.release_ops backup "$DATABASE" "$VALIDATION/coderus.db"
nohup "$PYTHON" -m coderus serve \
  --runtime preview \
  --config "$ROOT/config.yaml" \
  --secrets "$ROOT/secrets.env" \
  --database "$VALIDATION/coderus.db" \
  --workspace "$VALIDATION/workspaces" \
  --artifacts "$VALIDATION/artifacts" \
  --port "$PREVIEW_PORT" \
  >"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"
cleanup() { bash "$ROOT/scripts/stop-preview.sh" "$RELEASE_ID"; }
trap cleanup EXIT

for _ in {1..30}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Preview exited during startup; inspect $LOG_FILE" >&2
    exit 1
  fi
  if curl -fsS "$READY_URL" >/dev/null \
    && curl -fsS "http://127.0.0.1:${PREVIEW_PORT}/login" >/dev/null; then
    cleanup
    trap - EXIT
    "$PYTHON" -m coderus.release_install --write-verification "$RELEASE" \
      --public-key "$PUBLIC_KEY"
    chmod -R a-w "$RELEASE"
    echo "Release verified: $RELEASE_ID"
    exit 0
  fi
  sleep 1
done
echo "Preview readiness timed out; inspect $LOG_FILE" >&2
exit 1
