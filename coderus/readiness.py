from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from coderus.config import Settings
from coderus.models import Base


def readiness_report(
    settings: Settings,
    *,
    runtime: str,
    template_root: Path,
) -> tuple[int, dict[str, object]]:
    checks = {
        "database": "ok",
        "schema": "ok",
        "workspace": "ok",
        "templates": "ok",
    }
    error_codes: list[str] = []
    database_path = settings.database.path.expanduser().resolve()

    try:
        uri = f"{database_path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            connection.execute("SELECT 1").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            schema_matches = all(
                {column.name for column in table.columns}.issubset(
                    {
                        row[1]
                        for row in connection.execute(
                            f'PRAGMA table_info("{table.name}")'
                        )
                    }
                )
                for table in Base.metadata.sorted_tables
            )
    except (OSError, sqlite3.Error, ValueError):
        checks["database"] = "error"
        checks["schema"] = "skipped"
        error_codes.append("database_unavailable")
    else:
        expected_tables = set(Base.metadata.tables)
        if not expected_tables.issubset(tables) or not schema_matches:
            checks["schema"] = "error"
            error_codes.append("schema_incompatible")

    workspace = settings.workspace.root.expanduser().resolve()
    try:
        if not workspace.is_dir():
            raise OSError("workspace is not a directory")
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".ready-", delete=True):
            pass
    except OSError:
        checks["workspace"] = "error"
        error_codes.append("workspace_unavailable")

    try:
        (template_root / "base.html").read_bytes()
    except OSError:
        checks["templates"] = "error"
        error_codes.append("templates_unavailable")

    if error_codes:
        return 503, {
            "status": "not_ready",
            "runtime": runtime,
            "checks": checks,
            "error_codes": error_codes,
        }
    return 200, {"status": "ready", "runtime": runtime, "checks": checks}
