"""只读评测 CLI：候选列表、基线生成与数据库只读保证。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from coderus.cli import run_cli
from coderus.config import DatabaseSettings
from coderus.db import create_engine_from_settings
from coderus.evaluation.models import BaselineReport, BaselineSelection
from coderus.models import Base, Issue, Repository, Task, User


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    database = tmp_path / "data" / "coderus.db"
    engine = create_engine_from_settings(DatabaseSettings(path=database))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(username="admin", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="acme",
            name="widgets",
            canonical_url="https://github.com/acme/widgets",
            created_by_user=user,
        )
        session.add(repository)
        started = datetime(2026, 8, 1, tzinfo=UTC)
        for index in range(1, 13):
            issue = Issue(
                repository=repository,
                external_id=str(index),
                number=index,
                title=f"issue {index}",
                body="detail",
                state="open",
            )
            session.add(
                Task(
                    issue=issue,
                    creator=user,
                    status="completed" if index % 2 else "failed",
                    pr_url=(
                        f"https://github.com/acme/widgets/pull/{index}"
                        if index % 2
                        else None
                    ),
                    started_at=started,
                    finished_at=started + timedelta(minutes=index),
                )
            )
        session.commit()
    engine.dispose()

    config = tmp_path / "config.yaml"
    config.write_text("database:\n  path: data/coderus.db\n", encoding="utf-8")
    return {"config": config, "database": database}


def database_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_candidates_lists_recent_terminal_tasks(workspace, capsys) -> None:
    digest = database_digest(workspace["database"])

    exit_code = run_cli(
        ["eval", "candidates", "--config", str(workspace["config"]), "--limit", "10"]
    )

    assert exit_code == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 10
    assert listed[0] == {
        "task_key": "RE-12",
        "provider": "github",
        "repository": "acme/widgets",
        "issue_number": 12,
        "status": "failed",
    }
    serialized = json.dumps(listed).lower()
    for forbidden in ("issue ", "title", "admin", str(workspace["database"]).lower()):
        assert forbidden not in serialized
    assert database_digest(workspace["database"]) == digest


def test_candidates_rejects_out_of_range_limit(workspace, capsys) -> None:
    exit_code = run_cli(
        ["eval", "candidates", "--config", str(workspace["config"]), "--limit", "5"]
    )

    assert exit_code == 1
    assert "--limit" in capsys.readouterr().err


def test_baseline_writes_report_without_touching_database(
    workspace, tmp_path: Path, capsys
) -> None:
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        BaselineSelection(
            task_keys=tuple(f"RE-{index}" for index in range(1, 11))
        ).model_dump_json(),
        encoding="utf-8",
    )
    output_path = tmp_path / "baseline.json"
    digest = database_digest(workspace["database"])

    exit_code = run_cli(
        [
            "eval",
            "baseline",
            "--config",
            str(workspace["config"]),
            "--selection",
            str(selection_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "baseline.json: 10 tasks"
    report = BaselineReport.model_validate_json(output_path.read_text("utf-8"))
    assert report.summary.total == 10
    assert report.summary.pr_created == 5
    assert database_digest(workspace["database"]) == digest


def test_baseline_fails_without_writing_when_tasks_missing(
    workspace, tmp_path: Path, capsys
) -> None:
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        BaselineSelection(
            task_keys=(*tuple(f"RE-{index}" for index in range(1, 10)), "RE-404")
        ).model_dump_json(),
        encoding="utf-8",
    )
    output_path = tmp_path / "baseline.json"

    exit_code = run_cli(
        [
            "eval",
            "baseline",
            "--config",
            str(workspace["config"]),
            "--selection",
            str(selection_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 1
    assert "RE-404" in capsys.readouterr().err
    assert not output_path.exists()
