#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
UV="${CODERUS_UV:-}"
PUBLIC_KEY="${CODERUS_RELEASE_PUBLIC_KEY:-$ROOT/release-public-key.pem}"
BOOTSTRAP="${CODERUS_RELEASE_BOOTSTRAP:-$ROOT/bootstrap/release-bootstrap.py}"
BOOTSTRAP_PYTHON="${CODERUS_BOOTSTRAP_PYTHON:-python3}"

if [[ -z "$UV" ]]; then
  UV="$(command -v uv || true)"
fi
[[ -n "$UV" && -x "$UV" ]] || { echo "uv executable was not found" >&2; exit 1; }

[[ $# -eq 1 ]] || { echo "Usage: $0 <release.tar.gz>" >&2; exit 2; }
ARCHIVE="$(realpath "$1")"
BOOTSTRAP_PYTHON="$(command -v "$BOOTSTRAP_PYTHON" || true)"

[[ -f "$ARCHIVE" ]] || { echo "Release archive not found: $ARCHIVE" >&2; exit 1; }
[[ -f "$PUBLIC_KEY" ]] || { echo "Release public key not found: $PUBLIC_KEY" >&2; exit 1; }
[[ -f "$BOOTSTRAP" && ! -L "$BOOTSTRAP" ]] || {
  echo "Trusted release bootstrap not found: $BOOTSTRAP" >&2
  exit 1
}
[[ -n "$BOOTSTRAP_PYTHON" && -x "$BOOTSTRAP_PYTHON" ]] || {
  echo "Bootstrap Python was not found" >&2
  exit 1
}
"$BOOTSTRAP_PYTHON" -c "import cryptography" || {
  echo "Bootstrap Python requires cryptography" >&2
  exit 1
}
mkdir -p "$ROOT/releases"

RELEASE="$("$BOOTSTRAP_PYTHON" "$BOOTSTRAP" "$ARCHIVE" --root "$ROOT" \
  --public-key "$PUBLIC_KEY")"
[[ "$RELEASE" == "$ROOT/releases/"* ]] || {
  echo "Installer returned an unexpected path: $RELEASE" >&2
  exit 1
}

cd "$RELEASE"
"$UV" sync --locked --extra dev
CODEX_BINARY="$("$UV" run python -c '
import sys
from pathlib import Path

import yaml

config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
print(config.get("codex", {}).get("binary", "codex"))
' "$ROOT/config.yaml")"
if [[ "$CODEX_BINARY" == */* && -x "$CODEX_BINARY" ]]; then
  export PATH="$(dirname "$CODEX_BINARY"):$PATH"
fi
"$UV" run ruff check coderus tests
env -u CODERUS_ROOT "$UV" run pytest -q
echo "$RELEASE"
