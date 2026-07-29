# Coderus 原生 Codex PR 检视设计

## 背景

当前 Coderus 先生成完整 unified diff，再把 diff 放入自定义 Prompt。输入超过 90 万字符时，系统按文件和 hunk 分片，并为每个分片启动独立的 `codex exec`。这种方式存在三个问题：

1. 大 Prompt 可能在首次模型请求前超过接口长度限制，Codex 尚未来得及自动压缩。
2. 各分片是独立会话，无法共享跨文件上下文。
3. Coderus 重复实现了 Codex 原生 Reviewer 已具备的 diff 探索和上下文管理能力。

## 目标

- 使用 Codex CLI 原生 Review 流程检视 PR。
- 每个 PR 只启动一个 Reviewer，由 Codex 自主读取 Git diff 和关联代码。
- 允许 Codex 在同一次 Review 中自动压缩上下文并继续执行。
- 保留中文结构化输出、代码范围校验、PR revision 复查和幂等评论。
- 删除手工 Prompt 分片和分片结果合并逻辑。

## 非目标

- 不接入长期运行的 Codex App Server。
- 不实现服务重启后的 Reviewer 会话断点续跑。
- 不强制 Codex 逐个读取所有变更文件，覆盖范围以原生 Reviewer 的策略为准。
- 不保留手工分片作为失败回退。

## 方案选择

采用 `codex exec review --base <固定本地基准分支>`。

未采用的方案：

- App Server `review/start`：支持持久线程和显式压缩，但需要维护额外进程和 JSON-RPC 状态机，超出当前需求。
- 分片后使用 `exec resume`：仍需维护手工 diff 分片，不是真正的原生 Review。

## 执行流程

1. 拉取 PR，并将工作目录固定到平台返回的 Head SHA。
2. 计算 Base SHA 与 Head SHA 的 merge-base。
3. 在任务独占的工作目录中创建固定本地分支 `coderus-review-base`，使其指向 merge-base。
4. 本地解析 diff，计算变更文件数、增删行数和允许发布的代码行范围。该 diff 不进入模型 Prompt。
5. 启动一次原生 Review：

   ```text
   codex exec --json --sandbox read-only ... review --base coderus-review-base
   ```

6. 通过 `developer_instructions` 注入中文输出、检视标准和安全约束，并约束原生 Review 文本格式。
7. 将 Codex 原生 Review 文本确定性归一化为结构化结果，再根据本地 diff 范围校验 finding 的文件、版本侧和行号。
8. 再次读取远端 PR，确认 Base SHA 和 Head SHA 未变化。
9. 幂等发布一条 PR 评论并完成任务。

## Codex 调用边界

- 保留 `--ephemeral`。它只禁止会话落盘，不影响单次运行中的自动上下文压缩。
- 保留 `--ignore-user-config`、`--ignore-rules` 和 `project_doc_max_bytes=0`，防止主机配置或仓库规则改变自动化行为。
- 保留隔离边界和现有短期模型代理凭据。Linux 服务容器不支持 Codex 内层 bwrap namespace，
  因此由 Coderus 外层 Landlock 阻止工作区内容写入，并在运行后复查 revision 和 Git 状态；
  Codex 内层沙箱仅在该边界内关闭。
- 不通过 stdin 发送 unified diff。
- 不执行仓库测试、安装脚本或其他仓库提供的可执行内容；本次仍为静态检视。

## 代码调整

### PR 工作目录

- 在确认 merge-base 后创建或更新任务工作目录内的 `coderus-review-base`。
- Review 输入只保留比较基准、统计和行号范围，不再向 Prompt 暴露完整 diff。

### Runner 协议

- 为 `JobSpec` 增加仅限 `PR_REVIEW` 使用的原生 Review 基准字段。
- `LocalCodexRunner` 对 PR Review 构建 `codex exec ... review --base ...` 命令。
- 其他开发、修正和双 Reviewer Agent 的调用方式保持不变。

### 编排器

- 每个 PR 仅创建一个 `JobSpec`。
- 删除字符预算、文件/hunk 分片、循环执行和结果合并。
- 审计结果记录 `review_mode: native`，不再记录 `review_chunks`。

### 结果适配

- Codex 原生 Review 最终消息采用 `Review comment` 文本协议，不保证遵循 `--output-schema`，
  因此 PR 检视任务不再传递该失效参数和 schema 文件。
- Coderus 只解析原生优先级、标题、显式 LEFT/RIGHT、文件和行号，并将正文归一化为
  问题、影响和建议字段；未知优先级、歧义侧别和超长字段直接拒绝。
- 只有 JSONL 事件中存在针对固定比较基准到 HEAD 的成功 Git diff 检查时才接受结果，
  避免工具执行失败或伪造命令文本后误报“无问题”。
- 未发现问题且缺少内容摘要时，使用本地确定性 diff 统计生成摘要。

### 删除内容

- 删除 `build_review_prompts`、`ReviewPromptTooLarge` 和 diff 分片辅助函数。
- 删除 `merge_review_outputs`。
- 删除对应分片测试，替换为原生 Review 命令和大 PR 单次执行测试。

## 失败处理

- Codex 启动前的资源不足等可重试错误沿用现有有限重试。
- Codex 已开始执行后的失败、超时或非法结构化输出直接使任务失败，不自动重新消费模型。
- 任意失败都不发布部分评论。
- PR revision 在检视期间变化时，拒绝发布旧结果。
- 不回退到旧分片逻辑，避免同一任务出现两种覆盖语义。

## 验证

- Runner 单元测试确认使用 `review --base`，且不再通过 stdin 发送 diff。
- Workspace 测试确认临时基准分支固定到 merge-base。
- 编排器测试确认超大 diff 只启动一个 Reviewer。
- 回归测试确认行号过滤、revision 复查、Token 撤销和评论幂等性不变。
- 全量 Ruff、pytest、公开发布扫描和签名发布流程通过。
- 发布后重新执行 RV-20，确认仅有一个 Codex 进程、任务完成且 PR 评论正常更新。

## 发布策略

- 在独立开发目录完成实现和测试。
- 先安装并运行预览实例，再短暂停机切换正式服务。
- 保留旧发布版本用于回滚。
- 未经明确要求不推送远端仓库。
