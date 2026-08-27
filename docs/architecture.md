# 系统架构

## 设计原则

Coderus 将自动化编码限制在管理员授权的仓库和人工派发的任务内。业务状态持久化在 SQLite，外部动作通过明确的平台接口执行，Codex 进程只能访问当前任务的独立工作目录。

核心原则：

- **人工确认**：Issue 同步后不会自动执行，必须由用户派发。
- **Fork 隔离**：开发分支只推送到机器人 Fork，不直接修改上游仓库。
- **有限闭环**：双 Reviewer 各检视一次，开发 Agent 最多集中修正一次。
- **可追踪**：任务、Agent 运行、检视结论、PR 和通知均持久化。
- **最小凭据暴露**：平台 Token 不进入 Codex、Git 命令参数或任务报告；模型认证只使用任务级短期 Token，Agent 不读取 Codex 长期登录凭据。
- **可验证发布**：候选包使用 Ed25519 来源签名，并声明数据库 Schema 兼容范围；安装、预览、切换和回滚均先执行对应门禁。

## 组件

| 组件 | 职责 |
| --- | --- |
| FastAPI 与 Jinja | 登录、仓库、Issue、任务、检视和系统配置页面 |
| SQLite 与 SQLAlchemy | 用户、仓库、Issue、任务、Agent、PR 和集成设置 |
| Issue Poller | 定时或手动同步已授权仓库的开放 Issue |
| Task Scheduler | 执行全局、单用户和 Agent 进程并发限制 |
| Workflow Orchestrator | 驱动开发、检视、集中修正和 PR 发布状态机 |
| Codex Runner | 在独立工作目录中启动受限 Codex 进程 |
| Forge Registry | 统一 GitHub、GitCode 元数据、Fork、PR 和评论操作 |
| PR Review Orchestrator | 检出固定 PR 版本并调用 Codex 原生 Review |
| Feishu Integration | 长连接收取命令并发送任务和 PR 通知 |
| Release Runtime | 候选安装、隔离预览、排空、切换和自动回滚 |

## 代码分层

阶段 1 架构收敛后，模块依赖方向固定为：入口层（`web/routes`、`integrations/feishu`）只调用应用服务层（`coderus/application`），应用服务层只调用领域层（`issues`、`pr_review`、`workflow`、`forge`），领域层不反向依赖任何入口。

- `coderus/web/app.py` 只负责应用创建、middleware、health/readiness、静态文件和 Router 注册；运行时对象的构建和生命周期在 `coderus/web/runtime.py`（`build_runtime` + `RuntimeComponents.start/stop/close`，通过 FastAPI lifespan 驱动）。
- 页面路由按域拆分在 `coderus/web/routes/`（auth、users、repositories、issues、dashboard、tasks、reviews、system），公共 UI 助手在 `coderus/web/ui.py`；依赖全部通过构造参数显式注入。
- `coderus/application` 提供网页和飞书共用的用例入口：`IssueCommands`（添加与派发）、`ReviewCommands`（PR 检视入队）、`TaskCommands`（取消、关闭、意见同步、再发布），错误统一为 `NotFound`/`Forbidden`/`Conflict`。
- `TaskOrchestrator` 只保留租约、状态推进、分支选择和顶层异常策略；Agent 阶段执行在 `workflow/agent_stage.py`、双 Reviewer 周期在 `workflow/review_cycle.py`、提交封装与 PR 发布在 `workflow/publication.py`、提示词纯函数在 `workflow/prompts.py`。
- Forge 发布使用 typed `PublishRequest`（构造即校验 workspace、owner、分支等），不再接收任意 kwargs；Issue provider 接口暂未迁移。

## 可靠性机制

- 活跃 Manager 使用与 SQLite 同目录的操作系统文件锁，同一数据目录只允许一个调度实例；预览和维护进程不参与任务领取。
- Issue 任务和 PR 检视任务通过数据库租约领取并定时续约，所有状态写入都校验领取令牌，过期执行器不能覆盖新执行器的结果。
- 全局和单用户并发额度在数据库领取事务中检查，进程内信号量仅负责限制 Codex 子进程数。
- Agent 运行和任务状态转换均持久化；服务重启会中断未完成的 Agent 记录，并按任务恢复策略重新排队或进入可人工处理状态。
- PR 发布前记录稳定发布意图；平台请求使用固定分支和 PR 查询进行对账，避免网络超时后重复创建 PR。
- 飞书命令按消息 ID 去重，命令回复和任务完成通知先写入 outbox 再发送；失败后按有上限的指数退避重试，服务重启后继续投递。飞书接口不提供客户端幂等键，因此网络结果不确定时采用至少一次投递，极端情况下可能出现重复消息，但不会静默丢失。
- 只有明确发生在子进程启动前、且没有外部副作用的系统资源耗尽允许最多两次短暂重试。

## Issue 任务生命周期

1. Poller 同步管理员启用仓库的 Issue。
2. 用户人工派发后创建 `RE-N` 任务。
3. 开发 Agent 探索代码、复现问题、分析根因、实施修改并执行测试。
4. 两个 Reviewer 分别检查正确性和回归风险。
5. 开发 Agent 汇总有效意见并最多修正一次。
6. 系统检查工作树、提交和凭据泄漏，再推送 Fork 分支并创建 PR。
7. 任务进入等待人工审核；维护者意见由用户选择后继续处理。

PR 描述中的开发与测试报告使用六段中文结构：问题描述、问题复现、修改方案、修改验证、测试回归、遗留问题。已经修复的检视意见不会作为遗留问题重复展示。

## PR 检视生命周期

PR 检视使用独立的 `RV-N` 任务：读取 PR 元数据、建立独立工作目录、固定 base/head 版本、运行 Codex Review、校验文件和变更行范围，再向 PR 发布一条汇总评论。该流程不修改代码、不安装仓库依赖、不运行仓库脚本，也不推送分支。

## 运行边界

- Manager 和模型凭据代理只监听回环地址。
- 每个 Issue 或 PR 使用独立工作目录。
- 工作区、变更文件数、补丁和 Agent 输出均有硬上限；超限任务终止并保留可诊断错误。
- 执行环境移除 Coderus 自身密钥和平台 Token。
- 模型代理只允许 Responses API、配置模型和受限请求字段，并对单阶段请求数、并发数和响应字节数计量。
- Runner 使用每次运行后销毁的临时 `HOME` 和 `CODEX_HOME`，不复制或读取 Manager 的 Codex 登录态。
- PR 发布前检查工作树干净、提交存在且没有疑似凭据。
- 发布排空期间拒绝新的写操作和任务领取，现有任务完成后再切换版本。
- 发布历史在解除排空前原子落盘并按配置保留，失败时保持排空状态。
