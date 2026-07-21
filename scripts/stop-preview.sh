#!/usr/bin/env bash
set -euo pipefail

ROOT="${CODERUS_ROOT:-/opt/coderus}"
[[ $# -eq 1 ]] || { echo "Usage: $0 <release-id>" >&2; exit 2; }
RELEASE_ID="$1"
[[ "$RELEASE_ID" =~ ^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$ ]] || {
  echo "Invalid release id" >&2
  exit 2
}
PID_FILE="$ROOT/validation/$RELEASE_ID/preview.pid"
[[ -f "$PID_FILE" ]] || exit 0
pid="$(<"$PID_FILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  exit 0
fi
command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
if [[ "$command_line" != *"coderus serve"* || "$command_line" != *"--runtime preview"* ]]; then
  echo "PID $pid does not belong to the Coderus preview" >&2
  exit 1
fi
kill "$pid"
for _ in {1..15}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    exit 0
  fi
  sleep 1
done
echo "Preview did not stop within 15 seconds" >&2
exit 1
