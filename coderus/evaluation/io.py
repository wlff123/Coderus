"""评测选择文件加载与报告原子写入。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from coderus.evaluation.models import BaselineReport, BaselineSelection


def load_selection(path: Path) -> BaselineSelection:
    try:
        raw = path.read_text(encoding="utf-8")
        return BaselineSelection.model_validate_json(raw)
    except (OSError, ValueError, ValidationError) as exc:
        # 不回显文件内容，避免把本地路径或残缺 JSON 泄漏进日志。
        raise ValueError("invalid evaluation selection") from exc


def write_report(path: Path, report: BaselineReport) -> None:
    payload = json.dumps(
        report.model_dump(mode="json"), ensure_ascii=False, indent=2
    )
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
