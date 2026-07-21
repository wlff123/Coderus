# 系统架构

## 设计原则

Coderus 将自动化编码限制在管理员授权的仓库和人工派发的任务内。业务状态持久化在 SQLite，外部动作通过明确的平台接口执行，Codex 进程只能访问当前任务的独立工作目录。

核心原则：

- **人工确认**：Issue 同步后不会自动执行，必须由用户派发。
- **Fork 隔离**：开发分支只推送到机器人 Fork，不直接修改上游仓库。
- **有限闭环**：双 Reviewer 各检视一次，开发 Agent 最多集中修正一次。
- **可追踪**：任务、Agent 运行、检视结论、PR 和通知均持久化。
- **最小凭据暴露**：平台 Token 不进入 Codex、Git 命令参数或任务报告。

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
- 执行环境移除 Coderus 自身密钥和平台 Token。
- PR 发布前检查工作树干净、提交存在且没有疑似凭据。
- 发布排空期间拒绝新的写操作和任务领取，现有任务完成后再切换版本。
