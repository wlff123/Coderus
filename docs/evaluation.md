# 评测基线操作指南

评测基线用于在提示词、模型或工作流变更前后比较 Agent 效果。整套流程只读：
不重放 Agent、不修改数据库、不执行任何外部写操作。

## 原则

- 候选任务来自管理员数据库中的历史 Issue 任务（RE）终态记录；
- 样本应覆盖不同平台（GitHub/GitCode）、不同结果（成功 PR、需人工处理、失败）与不同构建类型；
- 选择文件只保存 `RE-N` 任务键与人工核实的注解；
- 注解只承载核实过的事实：无法核实时必须填 `null`，不得根据 Agent 报告文本猜测；
- 报告只含平台、`owner/name`、Issue 编号和指标，不含凭据、路径、Issue 正文或 Agent 输出。

## 步骤

### 1. 列出候选任务

```bash
uv run coderus eval candidates --config config.yaml --limit 50
```

以只读方式打开配置对应的 SQLite，输出最近的终态 Issue 任务：

```json
[{"task_key": "RE-17", "provider": "github", "repository": "acme/widgets", "issue_number": 42, "status": "completed"}]
```

`--limit` 取值 10 至 100，默认 20。

### 2. 建立选择文件

管理员根据候选输出，在本地建立 `data/evaluations/baseline-selection.json`
（`data/` 已被 Git 忽略，样本与报告都不提交）：

```json
{
  "contract_version": 1,
  "task_keys": ["RE-17", "RE-21", "RE-24"],
  "annotations": [
    {
      "task_key": "RE-17",
      "tests_passed": true,
      "accepted_without_code_changes": false,
      "human_changed_lines": 12
    }
  ]
}
```

任务键须为 10 至 20 个互不重复的 `RE-N`；`annotations` 逐项填写
`tests_passed`（人工核实的测试结论）、`accepted_without_code_changes`
（PR 是否未经人工改动即被接受）与 `human_changed_lines`（人工修改行数）。

### 3. 生成基线报告

```bash
uv run coderus eval baseline --config config.yaml \
  --selection data/evaluations/baseline-selection.json \
  --output data/evaluations/baseline-v0.1.0.json
```

报告按选择文件顺序输出每个任务的结果分类、耗时、状态迁移数、
开发/Reviewer 运行次数、Reviewer 发现数与模型用量，并汇总
PR 创建率、耗时中位数、测试通过率等指标。写入为原子替换，
失败不会破坏旧报告。

## 指标口径

- 存在 `pr_url` 的任务一律计为 `pr_created`（包括之后已完成或 PR 被关闭的任务）；
- 其余结果只按任务稳定状态映射（`manual_intervention`/`failed`/`cancelled`/`closed`，`dismissed` 计入 `closed`），运行中任务计为 `incomplete`；
- 模型请求数与输出字节数来自 `AgentRun.structured_result["model_usage"]`（新任务自动记录）；旧任务无数据时为 `null`，不做估算；
- 中位数使用 `statistics.median`，仅统计有值的记录。

指标用于比较相同评测环境下的版本差异，不以单次任务结果代替趋势判断。
