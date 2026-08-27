from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import sys
from base64 import urlsafe_b64encode
from pathlib import Path

import uvicorn
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from coderus.config import Settings, load_settings
from coderus.evaluation.collector import collect_baseline
from coderus.evaluation.io import load_selection, write_report
from coderus.models import Issue, Task
from coderus.tasks.statuses import TERMINAL_TASK_STATES
from coderus.web.app import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_runtime_paths(settings: Settings, config_directory: Path) -> Settings:
    root = config_directory.expanduser().resolve()

    def resolved(path: Path) -> Path:
        path = path.expanduser()
        return path.resolve() if path.is_absolute() else (root / path).resolve()

    return settings.model_copy(
        update={
            "database": settings.database.model_copy(
                update={"path": resolved(settings.database.path)}
            ),
            "workspace": settings.workspace.model_copy(
                update={"root": resolved(settings.workspace.root)}
            ),
            "artifacts": settings.artifacts.model_copy(
                update={"root": resolved(settings.artifacts.root)}
            ),
        }
    )


def prepare_runtime_settings(
    settings: Settings,
    args: argparse.Namespace,
    config_directory: Path,
) -> Settings:
    prepared = resolve_runtime_paths(settings, config_directory)
    overrides = (args.database, args.workspace, args.artifacts)
    if args.runtime == "preview" and any(path is None for path in overrides):
        raise ValueError(
            "preview requires isolated --database, --workspace and --artifacts paths"
        )

    def override_path(value: Path | None, current: Path) -> Path:
        if value is None:
            return current
        return value.expanduser().resolve()

    database = override_path(args.database, prepared.database.path)
    workspace = override_path(args.workspace, prepared.workspace.root)
    artifacts = override_path(args.artifacts, prepared.artifacts.root)
    if args.runtime == "preview" and (
        database == prepared.database.path
        or workspace == prepared.workspace.root
        or artifacts == prepared.artifacts.root
    ):
        raise ValueError("preview paths must differ from production paths")
    if args.port is not None and not 1 <= args.port <= 65535:
        raise ValueError("port must be between 1 and 65535")

    return prepared.model_copy(
        update={
            "database": prepared.database.model_copy(update={"path": database}),
            "workspace": prepared.workspace.model_copy(update={"root": workspace}),
            "artifacts": prepared.artifacts.model_copy(update={"root": artifacts}),
            "server": prepared.server.model_copy(
                update={"port": args.port or prepared.server.port}
            ),
        }
    )


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid secrets line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("CODERUS_") or not value:
            raise ValueError(f"invalid secret entry on line {line_number}")
        values[key] = value
    return values


def initialize_local(
    config_path: Path,
    secrets_path: Path,
    *,
    admin_password: str | None = None,
) -> str:
    if config_path.exists() or secrets_path.exists():
        raise FileExistsError("config or secrets file already exists")
    password = admin_password or secrets.token_urlsafe(15)
    if len(password) < 8:
        raise ValueError("administrator password must contain at least 8 characters")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    example = PROJECT_ROOT / "config.example.yaml"
    config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    secrets_path.write_text(
        "\n".join(
            (
                f"CODERUS_SESSION_SECRET={secrets.token_urlsafe(48)}",
                f"CODERUS_BOOTSTRAP_ADMIN_PASSWORD={password}",
                "CODERUS_CREDENTIAL_ENCRYPTION_KEY="
                f"{urlsafe_b64encode(secrets.token_bytes(32)).decode()}",
                "",
            )
        ),
        encoding="utf-8",
    )
    try:
        secrets_path.chmod(0o600)
    except OSError:
        pass
    return password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coderus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="create local configuration")
    init.add_argument("--config", type=Path, default=Path("config.yaml"))
    init.add_argument("--secrets", type=Path, default=Path("secrets.env"))
    init.add_argument("--admin-password")
    serve = subparsers.add_parser("serve", help="run the control plane")
    serve.add_argument("--config", type=Path, default=Path("config.yaml"))
    serve.add_argument("--secrets", type=Path, default=Path("secrets.env"))
    serve.add_argument(
        "--runtime",
        choices=("active", "preview", "maintenance"),
        default="active",
    )
    serve.add_argument("--port", type=int)
    serve.add_argument("--database", type=Path)
    serve.add_argument("--workspace", type=Path)
    serve.add_argument("--artifacts", type=Path)
    evaluation = subparsers.add_parser(
        "eval", help="read-only evaluation baseline commands"
    )
    eval_commands = evaluation.add_subparsers(dest="eval_command", required=True)
    candidates = eval_commands.add_parser(
        "candidates", help="list recent terminal issue tasks"
    )
    candidates.add_argument("--config", type=Path, default=Path("config.yaml"))
    candidates.add_argument("--limit", type=int, default=20)
    baseline = eval_commands.add_parser(
        "baseline", help="collect a baseline report from a selection file"
    )
    baseline.add_argument("--config", type=Path, default=Path("config.yaml"))
    baseline.add_argument("--selection", type=Path, required=True)
    baseline.add_argument("--output", type=Path, required=True)
    return parser


def _configured_database_path(config_path: Path) -> Path:
    settings = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = Path((settings.get("database") or {}).get("path") or "data/coderus.db")
    raw = raw.expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (config_path.parent.expanduser().resolve() / raw).resolve()


def _read_only_engine(database_path: Path):
    return create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(
            f"file:{database_path.as_posix()}?mode=ro", uri=True
        ),
    )


def _list_candidates(session: Session, limit: int) -> list[dict]:
    tasks = session.scalars(
        select(Task)
        .where(Task.status.in_(TERMINAL_TASK_STATES))
        .options(selectinload(Task.issue).selectinload(Issue.repository))
        .order_by(Task.finished_at.desc().nullslast(), Task.id.desc())
        .limit(limit)
    )
    return [
        {
            "task_key": f"RE-{task.id}",
            "provider": task.issue.repository.provider,
            "repository": (
                f"{task.issue.repository.owner}/{task.issue.repository.name}"
            ),
            "issue_number": task.issue.number,
            "status": task.status,
        }
        for task in tasks
    ]


def run_eval(args: argparse.Namespace) -> int:
    if args.eval_command == "candidates" and not 10 <= args.limit <= 100:
        print("error: --limit must be between 10 and 100", file=sys.stderr)
        return 1
    engine = _read_only_engine(_configured_database_path(args.config))
    try:
        with Session(engine) as session:
            if args.eval_command == "candidates":
                listed = _list_candidates(session, args.limit)
                print(json.dumps(listed, ensure_ascii=False, indent=2))
                return 0
            report = collect_baseline(session, load_selection(args.selection))
            write_report(args.output, report)
            print(f"{args.output.name}: {len(report.records)} tasks")
            return 0
    except (ValueError, OperationalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def run_cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        password = args.admin_password
        if password is None and os.isatty(0):
            password = getpass.getpass("管理员初始密码（留空自动生成）: ") or None
        password = initialize_local(args.config, args.secrets, admin_password=password)
        print(f"配置已创建。管理员用户名：admin，初始密码：{password}")
        return 0
    if args.command == "eval":
        return run_eval(args)

    environment = {**load_env_file(args.secrets), **os.environ}
    settings = load_settings(args.config, environment)
    settings = prepare_runtime_settings(settings, args, args.config.parent)
    app = create_app(
        settings,
        runtime=args.runtime,
        preview_isolated=args.runtime == "preview",
    )
    uvicorn.run(
        app,
        host=settings.server.bind,
        port=settings.server.port,
        workers=1,
        proxy_headers=settings.server.mode == "public",
    )
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
