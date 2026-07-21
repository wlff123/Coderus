#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
UV="${CODERUS_UV:-}"

if [[ -z "$UV" ]]; then
  UV="$(command -v uv || true)"
fi
[[ -n "$UV" && -x "$UV" ]] || { echo "uv executable was not found" >&2; exit 1; }

[[ $# -eq 1 ]] || { echo "Usage: $0 <release.tar.gz>" >&2; exit 2; }
ARCHIVE="$(realpath "$1")"
if [[ -n "${CODERUS_INSTALL_PYTHON:-}" ]]; then
  PYTHON="$CODERUS_INSTALL_PYTHON"
elif [[ -x "$ROOT/current/.venv/bin/python" ]]; then
  PYTHON="$ROOT/current/.venv/bin/python"
else
  PYTHON="$ROOT/.venv/bin/python"
fi

[[ -f "$ARCHIVE" ]] || { echo "Release archive not found: $ARCHIVE" >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Installer Python not found: $PYTHON" >&2; exit 1; }
mkdir -p "$ROOT/releases"

if [[ -d "$ROOT/current" ]]; then
  cd "$ROOT/current"
else
  cd "$ROOT"
fi
RELEASE="$($PYTHON -m coderus.release_install "$ARCHIVE" --root "$ROOT")"
[[ "$RELEASE" == "$ROOT/releases/"* ]] || {
  echo "Installer returned an unexpected path: $RELEASE" >&2
  exit 1
}

cd "$RELEASE"
"$UV" sync --locked --extra dev
CODEX_BINARY="$("$PYTHON" -c '
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
"$UV" run pytest -q
echo "$RELEASE"
