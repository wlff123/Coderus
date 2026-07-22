#!/usr/bin/env bash
set -euo pipefail

umask 077
ROOT="${CODERUS_ROOT:-/opt/coderus}"
RUN_USER="${CODERUS_RUN_USER:-coderus}"
PID_FILE="$ROOT/data/coderus.pid"
LOG_DIR="$ROOT/data/logs"
LOG_FILE="$LOG_DIR/coderus.log"
PORT="${CODERUS_PORT:-18082}"
READY_URL="http://127.0.0.1:${PORT}/readyz"
START_TIMEOUT="${CODERUS_START_TIMEOUT:-15}"
DEADLINE_MS="${CODERUS_CUTOVER_DEADLINE_MS:-}"

now_ms() {
  date +%s%3N
}

remaining_seconds() {
  local remaining_ms=$((DEADLINE_MS - $(now_ms)))
  (( remaining_ms > 0 )) || return 1
  printf '%d.%03d\n' "$((remaining_ms / 1000))" "$((remaining_ms % 1000))"
}

if [[ -z "$DEADLINE_MS" ]]; then
  DEADLINE_MS=$(( $(now_ms) + START_TIMEOUT * 1000 ))
fi

if [[ "${CODERUS_ALLOW_ANY_USER:-0}" != "1" && "$(id -un)" != "$RUN_USER" ]]; then
  echo "Coderus must run as $RUN_USER" >&2
  exit 1
fi
[[ -L "$ROOT/current" ]] || { echo "Current release is not configured" >&2; exit 1; }
RELEASE="$(readlink -f "$ROOT/current")"
PYTHON="$RELEASE/.venv/bin/python"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(<"$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local command_line process_cwd
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
  process_cwd="$(readlink -f "/proc/$pid/cwd")"
  [[ "$command_line" == *"coderus serve"* ]]
  [[ "$command_line" == *"--config $ROOT/config.yaml"* ]]
  [[ "$command_line" != *"--runtime preview"* ]]
  if [[ "$command_line" == *"--runtime active"* ]]; then
    [[ "$process_cwd" == "$RELEASE" ]]
  else
    [[ -f "$RELEASE/LEGACY_RUNTIME" ]]
    if [[ -f "$RELEASE/LEGACY_ROOT_CWD" ]]; then
      [[ "$process_cwd" == "$ROOT" ]]
    else
      [[ "$process_cwd" == "$RELEASE" ]]
    fi
  fi
}

if is_running; then
  echo "Coderus is already running (PID $(<"$PID_FILE"))"
  exit 0
fi

rm -f "$PID_FILE"
[[ -x "$PYTHON" ]] || { echo "Python environment not found: $PYTHON" >&2; exit 1; }
[[ -f "$ROOT/config.yaml" ]] || { echo "Missing config.yaml" >&2; exit 1; }
mkdir -p "$LOG_DIR"
cd "$RELEASE"
export CODERUS_RELEASE_GATE="$ROOT/data/release-draining"
export PYTHONDONTWRITEBYTECODE=1

if [[ -f "$RELEASE/LEGACY_RUNTIME" ]]; then
  rm -f "$RELEASE/LEGACY_ROOT_CWD"
  READY_URL="http://127.0.0.1:${PORT}/healthz"
  nohup "$PYTHON" -m coderus serve \
    --config "$ROOT/config.yaml" \
    --secrets "$ROOT/secrets.env" \
    >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
else
  nohup "$PYTHON" -m coderus serve \
    --runtime active \
    --config "$ROOT/config.yaml" \
    --secrets "$ROOT/secrets.env" \
    --port "$PORT" \
    >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
fi
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

while (( $(now_ms) < DEADLINE_MS )); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Coderus exited during startup; inspect $LOG_FILE" >&2
    exit 1
  fi
  remaining="$(remaining_seconds)" || break
  if curl --connect-timeout "$remaining" --max-time "$remaining" \
    -fsS "$READY_URL" >/dev/null; then
    echo "Coderus started from $RELEASE (PID $pid)"
    exit 0
  fi
  sleep 0.2
done

kill -KILL "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Coderus readiness timed out; inspect $LOG_FILE" >&2
exit 1
