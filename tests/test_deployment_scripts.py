import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_container_start_script_has_safe_idempotent_contract() -> None:
    script = (ROOT / "scripts" / "container-start.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert 'ROOT="${CODERUS_ROOT:-/opt/coderus}"' in script
    assert 'RUN_USER="${CODERUS_RUN_USER:-coderus}"' in script
    assert 'RELEASE="$(readlink -f "$ROOT/current")"' in script
    assert 'PYTHON="$RELEASE/.venv/bin/python"' in script
    assert 'PID_FILE="$ROOT/data/coderus.pid"' in script
    assert "kill -0" in script
    assert 'readlink -f "/proc/$pid/cwd"' in script
    assert '"--config $ROOT/config.yaml"' in script
    assert 'nohup "$PYTHON" -m coderus serve' in script
    assert script.count("9>&-") >= 2
    assert "--runtime active" in script
    assert 'READY_URL="http://127.0.0.1:${PORT}/readyz"' in script
    assert '[[ -f "$RELEASE/LEGACY_RUNTIME" ]]' in script
    assert 'READY_URL="http://127.0.0.1:${PORT}/healthz"' in script
    assert 'id -un' in script
    assert '"$(id -un)" != "$RUN_USER"' in script


def test_container_stop_script_validates_pid_owner_before_stopping() -> None:
    script = (ROOT / "scripts" / "container-stop.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert 'ROOT="${CODERUS_ROOT:-/opt/coderus}"' in script
    assert 'PID_FILE="$ROOT/data/coderus.pid"' in script
    assert "/proc/$pid/cmdline" in script
    assert "/proc/$pid/cwd" in script
    assert '"--config $ROOT/config.yaml"' in script
    assert "coderus" in script
    assert "kill -0" in script
    assert "CODERUS_FORCE_STOP" in script
    assert "kill -KILL" in script


def test_ssh_tunnel_script_reconnects_without_password() -> None:
    script = (ROOT / "scripts" / "start-ssh-tunnel.ps1").read_text(encoding="utf-8")

    assert "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}" in script
    assert '[string]$SshUser = "coderus"' in script
    assert '[string]$SshHost = "127.0.0.1"' in script
    assert "ExitOnForwardFailure=yes" in script
    assert "ServerAliveInterval=30" in script
    assert "ServerAliveCountMax=3" in script
    assert "BatchMode=yes" in script
    assert "Start-Sleep -Seconds 5" in script
    assert "password" not in script.lower()


def test_deployment_scripts_use_generic_public_defaults() -> None:
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "scripts").iterdir()
        if path.suffix in {".sh", ".ps1"}
    )

    assert "/opt/coderus" in scripts
    assert 'CODERUS_RUN_USER:-coderus' in scripts


def test_ci_uses_the_locked_environment() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "uv sync --locked --extra dev" in workflow
    assert "uv run pytest -q" in workflow


def test_deployment_document_invokes_shell_scripts_through_bash() -> None:
    document = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "bash scripts/bootstrap-release-layout.sh" in document
    assert "bash scripts/container-start.sh" in document
    assert "\nscripts/" not in document


def test_release_install_and_preview_scripts_are_isolated() -> None:
    install = (ROOT / "scripts" / "install-release.sh").read_text(encoding="utf-8")
    preview = (ROOT / "scripts" / "preview-release.sh").read_text(encoding="utf-8")
    stop_preview = (ROOT / "scripts" / "stop-preview.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in install
    assert 'ROOT="${CODERUS_ROOT:-/opt/coderus}"' in install
    assert 'UV="${CODERUS_UV:-}"' in install
    assert 'command -v uv' in install
    assert "coderus.release_install" not in install
    assert "CODERUS_RELEASE_BOOTSTRAP" in install
    assert "CODERUS_BOOTSTRAP_PYTHON" in install
    assert "CODERUS_RELEASE_PUBLIC_KEY" in install
    assert "sync --locked --extra dev" in install
    assert "yaml.safe_load" in install
    assert 'export PATH="$(dirname "$CODEX_BINARY"):$PATH"' in install
    assert 'env -u CODERUS_ROOT "$UV" run pytest -q' in install
    assert "touch \"$RELEASE/VERIFIED\"" not in install
    assert "coderus.release_ops backup" in preview
    assert 'cleanup() { bash "$ROOT/scripts/stop-preview.sh" "$RELEASE_ID"; }' in preview
    assert "--runtime preview" in preview
    assert "--database" in preview
    assert "--workspace" in preview
    assert "--artifacts" in preview
    assert "http://127.0.0.1:${PREVIEW_PORT}/readyz" in preview
    assert "--write-verification" in preview
    assert "/proc/$pid/cmdline" in stop_preview
    assert "--runtime preview" in stop_preview


def test_promotion_script_drains_then_switches_with_automatic_rollback() -> None:
    promote = (ROOT / "scripts" / "promote-release.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts" / "rollback-release.sh").read_text(encoding="utf-8")

    assert "flock" in promote
    assert 'CUTOVER_TIMEOUT="${CODERUS_CUTOVER_TIMEOUT:-30}"' in promote
    assert 'touch "$DRAIN_GATE"' in promote
    assert "coderus.release_ops check-idle" in promote
    assert "--verify-release" in promote
    assert "coderus.release_ops check-schema" in promote
    assert 'bash "$ROOT/scripts/container-stop.sh"' in promote
    assert "coderus.release_ops backup" in promote
    assert "coderus.release_ops migrate" in promote
    assert 'rm -f "$ROOT/$pointer"' in promote
    assert 'ln -s "releases/$release_id" "$ROOT/$pointer"' in promote
    assert 'rm -f "$ROOT/$pointer"' in rollback
    assert 'ln -s "releases/$release_id" "$ROOT/$pointer"' in rollback
    assert "mv -Tf" not in promote
    assert "mv -Tf" not in rollback
    assert "--runtime maintenance" in promote
    assert "9>&-" in promote
    assert 'bash "$ROOT/scripts/container-start.sh"' in promote
    assert "rollback_failed_promotion" in promote
    assert 'timeout --foreground "${remaining}s"' in promote
    assert "CODERUS_CUTOVER_DEADLINE_MS" in promote
    assert "CODERUS_CUTOVER_DEADLINE_MS" in (
        ROOT / "scripts" / "container-start.sh"
    ).read_text(encoding="utf-8")
    assert "CODERUS_CUTOVER_DEADLINE_MS" in (
        ROOT / "scripts" / "container-stop.sh"
    ).read_text(encoding="utf-8")
    assert 'rm -f "$DRAIN_GATE"' in promote
    assert "coderus.release_ops write-history" in promote
    assert "coderus.release_ops prune-artifacts" in promote
    assert "current" in rollback
    assert "previous" in rollback


def test_legacy_promotion_stops_ingress_before_final_idle_check() -> None:
    promote = (ROOT / "scripts" / "promote-release.sh").read_text(encoding="utf-8")
    cutover = promote.split("CUTOVER_DEADLINE_MS=", maxsplit=1)[1]

    assert cutover.index('bash "$ROOT/scripts/container-stop.sh"') < cutover.index(
        "coderus.release_ops check-idle"
    )
    assert cutover.index("coderus.release_ops backup") < cutover.index(
        "coderus.release_ops migrate"
    ) < cutover.index("switch_link current")
    assert 'OLD_RELEASE/LEGACY_RUNTIME' not in cutover


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX process signals")
def test_container_stop_force_kills_within_the_shared_deadline(tmp_path: Path) -> None:
    root = tmp_path / "Coderus"
    release = root / "releases" / "20260721-000000-deadbeef"
    (root / "data").mkdir(parents=True)
    release.mkdir(parents=True)
    (release / "LEGACY_RUNTIME").touch()
    (root / "current").symlink_to(Path("releases") / release.name)
    config = root / "config.yaml"
    config.touch()
    program = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(30)"
    )
    launcher = (
        f"cd {release!s}; "
        f"{sys.executable} -c '{program}' 'coderus serve' "
        f"'--config {config!s}' >/dev/null 2>&1 & echo $!"
    )
    pid = int(
        subprocess.run(
            ["bash", "-c", launcher],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    (root / "data" / "coderus.pid").write_text(f"{pid}\n", encoding="ascii")

    started = time.monotonic()
    result = subprocess.run(
        [str(ROOT / "scripts" / "container-stop.sh")],
        env={
            **os.environ,
            "CODERUS_ROOT": str(root),
            "CODERUS_STOP_TIMEOUT": "1",
            "CODERUS_FORCE_STOP": "1",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 1.5
    assert not (root / "data" / "coderus.pid").exists()


def test_bootstrap_script_preserves_current_runtime_as_rollback_release() -> None:
    script = (ROOT / "scripts" / "bootstrap-release-layout.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in script
    assert '[[ ! -e "$ROOT/current" ]]' in script
    assert 'cp -a "$ROOT/.venv"' in script
    assert 'ln -s "releases/$RELEASE_ID" "$ROOT/current"' in script
    assert 'touch "$RELEASE/VERIFIED"' in script
    assert 'touch "$RELEASE/LEGACY_RUNTIME"' in script
    assert 'touch "$RELEASE/LEGACY_ROOT_CWD"' in script
    assert '"$ROOT/bootstrap/release-bootstrap.py"' in script
    assert '"$ROOT/coderus/release_bootstrap.py"' in script
