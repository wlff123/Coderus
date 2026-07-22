# 阶段 0：评测基线实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立不产生外部平台副作用的历史任务评测基线，固定 10 至 20 个样本，并记录结果、阶段耗时、Reviewer 结果和模型代理用量。

**Architecture:** 新增只读 `coderus.evaluation` 包，从现有 SQLite 任务数据生成版本化 JSON 报告。模型代理仅增加请求数和响应字节快照；评测命令不调用 Codex、不修改任务、不推送分支也不创建 PR。

**Tech Stack:** Python 3.12、Pydantic 2、SQLAlchemy 2、SQLite、pytest、argparse。

## Global Constraints

- 保持单机部署、SQLite 和本地 Agent 子进程。
- 阶段 0 的评测操作只读，不执行 Agent，不访问代码平台写接口。
- 报告不得包含 Token、模型 Key、个人路径、完整 Issue 正文、Agent stdout 或源码。
- 固定样本数量为 10 至 20 个，使用稳定任务键 `RE-N`。
- 当前只记录模型请求数和响应字节数，不估算 Token 或费用。
- 所有行为变更先写失败测试；每个任务独立提交。

---

## 文件结构

- Create: `coderus/evaluation/__init__.py`：公开评测类型和入口。
- Create: `coderus/evaluation/models.py`：选择文件和报告契约。
- Create: `coderus/evaluation/collector.py`：只读查询和指标计算。
- Create: `coderus/evaluation/io.py`：原子 JSON 输入输出。
- Create: `tests/evaluation/test_models.py`
- Create: `tests/evaluation/test_collector.py`
- Create: `tests/evaluation/test_io.py`
- Modify: `coderus/model_proxy/broker.py`
- Modify: `coderus/workflow/orchestrator.py`
- Modify: `coderus/cli.py`
- Create: `tests/test_evaluation_cli.py`
- Create: `docs/evaluation.md`
- Modify: `docs/evolution-roadmap.md`

### Task 1: 定义版本化评测契约

**Files:**
- Create: `coderus/evaluation/__init__.py`
- Create: `coderus/evaluation/models.py`
- Test: `tests/evaluation/test_models.py`

**Interfaces:**
- Produces: `BaselineSelection.model_validate_json(str) -> BaselineSelection`
- Produces: `TaskBaseline`、`BaselineSummary`、`BaselineReport`

- [ ] **Step 1: 写选择数量和重复任务的失败测试**

```python
from pydantic import ValidationError
import pytest

from coderus.evaluation.models import BaselineSelection


def test_selection_requires_ten_to_twenty_unique_task_keys() -> None:
    with pytest.raises(ValidationError):
        BaselineSelection(task_keys=("RE-1",) * 10)

    selection = BaselineSelection(
        task_keys=tuple(f"RE-{index}" for index in range(1, 11))
    )
    assert len(selection.task_keys) == 10
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/evaluation/test_models.py -q`

Expected: FAIL，包含 `ModuleNotFoundError: No module named 'coderus.evaluation'`。

- [ ] **Step 3: 实现最小数据契约**

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EVALUATION_CONTRACT_VERSION = 1
TaskOutcome = Literal[
    "pr_created", "manual_intervention", "failed",
    "cancelled", "closed", "incomplete",
]


class TaskAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_key: str
    tests_passed: bool | None = None
    accepted_without_code_changes: bool | None = None
    human_changed_lines: int | None = Field(default=None, ge=0)


class BaselineSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal[1] = EVALUATION_CONTRACT_VERSION
    task_keys: tuple[str, ...] = Field(min_length=10, max_length=20)
    annotations: tuple[TaskAnnotation, ...] = ()

    @field_validator("task_keys")
    @classmethod
    def validate_task_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("task keys must be unique")
        if any(not value.startswith("RE-") or not value[3:].isdigit() for value in values):
            raise ValueError("task keys must use RE-N format")
        return values

    @model_validator(mode="after")
    def validate_annotations(self) -> BaselineSelection:
        annotation_keys = tuple(item.task_key for item in self.annotations)
        if len(set(annotation_keys)) != len(annotation_keys):
            raise ValueError("annotation task keys must be unique")
        if not set(annotation_keys).issubset(self.task_keys):
            raise ValueError("annotations must reference selected tasks")
        return self
class TaskBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_key: str
    provider: Literal["github", "gitcode"]
    repository: str
    issue_number: int
    status: str
    outcome: TaskOutcome
    duration_seconds: float | None
    transition_count: int
    developer_runs: int
    reviewer_runs: int
    reviewer_findings: int
    model_requests: int | None
    model_output_bytes: int | None
    tests_passed: bool | None
    accepted_without_code_changes: bool | None
    human_changed_lines: int | None


class BaselineSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    total: int
    pr_created: int
    manual_intervention: int
    failed: int
    cancelled: int
    closed: int
    incomplete: int
    pr_created_rate: float
    median_duration_seconds: float | None
    verified_test_pass_rate: float | None
    accepted_without_code_changes_rate: float | None
    median_human_changed_lines: float | None


class BaselineReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal[1] = EVALUATION_CONTRACT_VERSION
    generated_at: datetime
    records: tuple[TaskBaseline, ...]
    summary: BaselineSummary
```

`BaselineSelection` 增加 model validator：annotation 的任务键必须唯一且属于 `task_keys`。`coderus/evaluation/__init__.py` 只重导出公开类型，不导入数据库模块。

- [ ] **Step 4: 增加未知字段、错误版本和非法任务键测试**

Run: `uv run pytest tests/evaluation/test_models.py -q`

Expected: PASS。

- [ ] **Step 5: 静态检查并提交**

```bash
uv run ruff check coderus/evaluation tests/evaluation/test_models.py
git add coderus/evaluation tests/evaluation/test_models.py
git commit -m "feat: define evaluation baseline contract"
```

### Task 2: 保存模型代理阶段用量

**Files:**
- Modify: `coderus/model_proxy/broker.py`
- Modify: `coderus/workflow/orchestrator.py`
- Test: `tests/model_proxy/test_broker.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Produces: `CredentialBroker.usage(task_id: str, stage: str) -> UsageSnapshot | None`
- Persists: `AgentRun.structured_result["model_usage"]`

- [ ] **Step 1: 写不可变快照测试**

```python
def test_usage_returns_request_and_output_snapshot() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")
    permit = broker.acquire(
        token, endpoint="/v1/responses", requested_model="test-model"
    )
    permit.record_output(128)
    permit.release()

    assert broker.usage("task-1", "develop") == UsageSnapshot(
        request_count=1, output_bytes=128
    )
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/model_proxy/test_broker.py::test_usage_returns_request_and_output_snapshot -q`

Expected: FAIL，指向缺少 `UsageSnapshot` 或 `usage`。

- [ ] **Step 3: 实现线程安全快照**

```python
@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    request_count: int
    output_bytes: int


def usage(self, task_id: str, stage: str) -> UsageSnapshot | None:
    with self._lock:
        value = self._usage_by_task_stage.get((task_id, stage))
        if value is None:
            return None
        return UsageSnapshot(value.request_count, value.output_bytes)
```

方法放在 `CredentialBroker` 上，不暴露 Token、摘要或限额。

- [ ] **Step 4: 写 AgentRun 保存用量的失败测试**

在现有成功开发阶段测试中注入记录过一次请求的 Broker，并断言：

```python
assert run.structured_result["model_usage"] == {
    "request_count": 1,
    "output_bytes": 128,
}
```

- [ ] **Step 5: 在撤销短期 Token 前读取快照**

将 `_finish_agent_run` 增加参数：

```python
model_usage: UsageSnapshot | None = None
```

无论成功、失败、超时或取消，都在撤销 Token 前读取快照；存在快照时写入 `structured_result["model_usage"]`。保存后仍立即撤销 Token。

- [ ] **Step 6: 运行测试并提交**

```bash
uv run pytest tests/model_proxy/test_broker.py tests/test_workflow.py -q
git add coderus/model_proxy/broker.py coderus/workflow/orchestrator.py tests/model_proxy/test_broker.py tests/test_workflow.py
git commit -m "feat: record model proxy usage per agent run"
```

### Task 3: 实现只读基线收集器

**Files:**
- Create: `coderus/evaluation/collector.py`
- Test: `tests/evaluation/test_collector.py`

**Interfaces:**
- Consumes: `BaselineSelection`
- Produces: `collect_baseline(session: Session, selection: BaselineSelection, *, now: datetime | None = None) -> BaselineReport`

- [ ] **Step 1: 写完整聚合测试**

测试数据库建立 10 个任务，覆盖成功 PR、需人工处理、失败、取消、关闭和运行中状态，并加入 AgentRun、Review、TaskTransition。核心断言：

```python
report = collect_baseline(session, selection, now=fixed_now)
assert report.summary.total == 10
assert report.summary.pr_created == 3
assert report.records[0].task_key == "RE-1"
assert report.records[0].repository == "acme/widgets"
assert report.records[0].model_requests == 4
assert report.records[0].reviewer_findings == 2
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/evaluation/test_collector.py -q`

Expected: FAIL，指向缺少 `collect_baseline`。

- [ ] **Step 3: 实现批量查询和确定性聚合**

实现必须：

1. 将 `RE-N` 转换为整数任务 ID；
2. 一次查询 Task 并预加载 Issue、Repository、AgentRun、Review、TaskTransition；
3. 缺失任务时抛出包含缺失任务键的 `ValueError`；
4. 按选择文件顺序生成记录；
5. 使用 `started_at`、`finished_at` 计算持续时间；
6. 记录原始稳定状态；任何存在 `pr_url` 的任务均分类为 `pr_created`，包括后续已完成或 PR 已关闭的任务；
7. 仅按稳定状态映射其他结果，不解析中文摘要；
8. 聚合合法 `model_usage`，旧任务无数据时返回 `None`；
9. 将人工 annotation 合并到对应记录，不从报告文本推断测试或人工修改；
10. 使用 `statistics.median` 计算持续时间和已标注人工改动行数的中位数。

结果只保存平台、`owner/name`、Issue 编号和指标。

- [ ] **Step 4: 增加缺失任务、旧数据和非法结构化结果测试**

Run: `uv run pytest tests/evaluation/test_collector.py -q`

Expected: PASS。

- [ ] **Step 5: 静态检查并提交**

```bash
uv run ruff check coderus/evaluation/collector.py tests/evaluation/test_collector.py
git add coderus/evaluation/collector.py tests/evaluation/test_collector.py
git commit -m "feat: collect historical task baselines"
```

### Task 4: 增加原子 JSON 输入输出

**Files:**
- Create: `coderus/evaluation/io.py`
- Test: `tests/evaluation/test_io.py`

**Interfaces:**
- Produces: `load_selection(path: Path) -> BaselineSelection`
- Produces: `write_report(path: Path, report: BaselineReport) -> None`

- [ ] **Step 1: 写原子替换和失败保留旧文件测试**

```python
def test_write_report_replaces_destination_atomically(tmp_path: Path, report) -> None:
    destination = tmp_path / "baseline.json"
    destination.write_text("old", encoding="utf-8")
    write_report(destination, report)
    saved = BaselineReport.model_validate_json(destination.read_text("utf-8"))
    assert saved == report
    assert list(tmp_path.glob(".baseline.json.*.tmp")) == []
```

通过 monkeypatch 让 `os.replace` 失败，断言旧文件仍为 `old` 且临时文件被清理。

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/evaluation/test_io.py -q`

Expected: FAIL，指向缺少 `write_report`。

- [ ] **Step 3: 实现原子写入**

使用同目录临时文件、UTF-8、两个空格缩进、末尾换行、`flush`、`fsync` 和 `os.replace`。`load_selection` 对不存在文件、非法 JSON 和错误契约抛出 `ValueError("invalid evaluation selection")`，不得回显内容。

- [ ] **Step 4: 验证序列化报告不含敏感字段**

```python
for forbidden in ("token", "api_key", "workspace_path", "stdout", "issue_body"):
    assert forbidden not in serialized.lower()
```

- [ ] **Step 5: 运行测试并提交**

```bash
uv run pytest tests/evaluation/test_io.py -q
git add coderus/evaluation/io.py tests/evaluation/test_io.py
git commit -m "feat: write evaluation reports atomically"
```

### Task 5: 增加只读评测 CLI

**Files:**
- Modify: `coderus/cli.py`
- Test: `tests/test_evaluation_cli.py`

**Interfaces:**
- Produces: `coderus eval candidates --config PATH --limit N`
- Produces: `coderus eval baseline --config PATH --selection PATH --output PATH`

- [ ] **Step 1: 写 CLI 调度失败测试**

```python
exit_code = run_cli([
    "eval", "baseline",
    "--config", str(config_path),
    "--selection", str(selection_path),
    "--output", str(output_path),
])
assert exit_code == 0
```

现有 `main()` 改为 `raise SystemExit(run_cli())`，已有 `init` 和 `serve` 测试必须保持通过。

- [ ] **Step 2: 运行测试并确认失败**

Run: `uv run pytest tests/test_evaluation_cli.py -q`

Expected: FAIL，指向缺少 `eval` 子命令或 `run_cli`。

- [ ] **Step 3: 实现 `eval candidates`**

只读打开配置对应 SQLite，列出最近最多 `N` 个终态 Issue 任务，输出：

```json
[{"task_key":"RE-17","provider":"github","repository":"acme/widgets","issue_number":42,"status":"awaiting_review"}]
```

`--limit` 范围 10 至 100，默认 20。输出不得包含绝对路径、用户、Issue 标题和失败摘要。

- [ ] **Step 4: 实现 `eval baseline`**

加载选择文件，调用收集器并原子输出。标准输出只包含文件名和任务数。不存在的任务返回非零状态且不生成报告。

- [ ] **Step 5: 验证数据库只读**

测试执行前后数据库 SHA-256 完全一致；测试期间禁止构造 Forge、Runner、Poller 或 Scheduler。

- [ ] **Step 6: 运行测试并提交**

```bash
uv run pytest tests/test_evaluation_cli.py tests/test_cli.py -q
git add coderus/cli.py tests/test_evaluation_cli.py
git commit -m "feat: add read-only evaluation commands"
```

### Task 6: 固定关键故障恢复契约

**Files:**
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/publisher/test_github.py`
- Modify: `tests/publisher/test_gitcode.py`
- Modify: `tests/pr_review/test_orchestrator.py`

**Interfaces:**
- Produces: 阶段 1 重构必须持续通过的故障恢复测试集合。

- [ ] **Step 1: 审核现有测试并只补缺失场景**

用测试名称建立矩阵，确保每个场景只有一个最靠近行为所有者的测试：

| 场景 | 预期 |
| --- | --- |
| 开发阶段返回 `TIMED_OUT` | AgentRun 为 timed_out，Task 为 failed，不调用 publish |
| 非发布阶段租约过期 | Task 为 manual_intervention，failure_code 为 manager_restarted |
| publishing 阶段已有 commit 后租约过期 | Task 重新 queued，failure_code 为 publish_existing |
| PR 创建请求结果不确定后重试 | 使用固定 branch 查询已有 PR，不重复 POST |
| PR 检视进程中断 | 租约到期后重新 queued，旧 claim_token 被清除 |

- [ ] **Step 2: 先运行每个现有测试并记录覆盖缺口**

Run: `uv run pytest tests/test_scheduler.py tests/test_workflow.py tests/publisher/test_github.py tests/publisher/test_gitcode.py tests/pr_review/test_orchestrator.py -q`

Expected: 当前测试通过；没有对应断言的矩阵项才新增测试。

- [ ] **Step 3: 为缺失矩阵项写失败测试并确认测试确实触发目标路径**

PR 不确定结果测试必须让第一次 POST 在服务端已创建 PR 后模拟连接结果未知，第二次调用返回查询到的同一 PR；断言 POST 计数为 1，PR number 和 URL 不变。

- [ ] **Step 4: 若测试暴露现有缺陷，最小修复后重跑矩阵**

不得在本任务重构 Orchestrator 或 Publisher。修复只限于使上述既定恢复规则成立，并单独提交对应测试与最小代码。

- [ ] **Step 5: 提交恢复契约测试**

```bash
git add tests coderus
git commit -m "test: lock task recovery contracts"
```

### Task 7: 文档、实际基线与阶段验收

**Files:**
- Create: `docs/evaluation.md`
- Modify: `docs/evolution-roadmap.md`

- [ ] **Step 1: 编写操作文档**

明确候选任务来自管理员数据库；样本覆盖平台、结果和构建类型；选择文件保存 `RE-N` 和人工核实的测试/接收注解；阶段 0 不重放 Agent；未知数据必须为 `null`，不得根据 Agent 文本猜测。

- [ ] **Step 2: 从实际数据选择 10 至 20 个任务**

Run: `uv run coderus eval candidates --config config.yaml --limit 50`

管理员根据输出建立本地 `data/evaluations/baseline-selection.json`，并逐项填写 `tests_passed`、`accepted_without_code_changes` 和 `human_changed_lines`；无法核实时填写 `null`。该目录已被 Git 忽略，不提交实际样本和报告。

- [ ] **Step 3: 生成首份实际基线**

```bash
uv run coderus eval baseline --config config.yaml \
  --selection data/evaluations/baseline-selection.json \
  --output data/evaluations/baseline-v0.1.0.json
```

Expected: 报告包含 10 至 20 条记录，数据库哈希不变。

- [ ] **Step 4: 运行完整质量门禁**

```bash
uv run python scripts/check-public-release.py --root .
uv run ruff check coderus tests scripts
uv run pytest -q
git diff --check
```

Expected: 扫描、Ruff、pytest 和差异检查全部通过。

- [ ] **Step 5: 提交阶段 0 文档**

```bash
git add docs/evaluation.md docs/evolution-roadmap.md
git commit -m "docs: document evaluation baseline workflow"
```

## 阶段 0 完成标准

- 本地固定选择文件包含 10 至 20 个真实历史任务。
- 基线命令不修改数据库、不调用 Agent、不执行外部写操作。
- 新 AgentRun 记录模型请求数和输出字节数。
- 报告不包含凭据、个人路径、完整 Issue、stdout 或源码。
- 完整测试和公开发布扫描通过。
