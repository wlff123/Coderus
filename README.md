# Coderus

Coderus 是一个自托管的代码仓管家。它从管理员授权的 GitHub 或 GitCode 公共仓库同步 Issue，由用户人工派发任务，再使用 Codex 完成代码探索、问题复现、修复、测试、双 Reviewer 检视、一次集中修正和 Pull Request 提交。

系统坚持人工治理：不会自动领取未确认的 Issue，不会直接向上游仓库推送代码，也不会自动合并 Pull Request。

## 主要能力

- GitHub、GitCode 公共仓库和开放 Issue 同步；
- 人工分诊、派发、忽略、恢复和关闭任务；
- 每个任务使用独立工作目录和 Codex 进程；
- 开发 Agent 完整分析与修复，两个 Reviewer 各检视一次；
- 最多一次集中修正，随后从机器人 Fork 向上游提交 PR；
- 同步维护者 PR 意见，选择意见后继续更新原 PR；
- 独立的 GitHub、GitCode PR 静态代码检视任务；
- 飞书群聊和单聊查询、派发、检视与通知；
- 用户管理、任务可见性、仓库筛选和三级并发限制；
- SQLite 本地持久化、加密集成凭据和版本化安全发布。

## 工作流程

```text
开放 Issue
  -> 人工派发
  -> 开发 Agent 探索、复现、修复和测试
  -> 两个 Reviewer 各检视一次
  -> 开发 Agent 最多集中修正一次
  -> 从机器人 Fork 提交 PR
  -> 人工审核和后续意见处理
```

代码检视任务独立于 Issue 处理流程。用户提交受信任仓库的 PR 链接后，Coderus 在独立目录检出准确版本，调用 Codex 原生 Review 流程，并在 PR 下发布一条结构化中文汇总评论。

## 环境要求

- Python 3.12 或 3.13；
- Git；
- [uv](https://docs.astral.sh/uv/)；
- 已登录的 Codex CLI，或兼容 OpenAI Responses API 的模型服务；
- 可选：GitHub、GitCode 和飞书应用凭据。

## 快速开始

```bash
git clone <your-coderus-repository-url>
cd Coderus
uv sync --locked --extra dev
uv run coderus init
uv run coderus serve --config config.yaml --secrets secrets.env
```

`coderus init` 生成被 Git 忽略的 `config.yaml` 和 `secrets.env`，并输出初始管理员密码。启动后访问 [http://127.0.0.1:18082](http://127.0.0.1:18082)，使用用户名 `admin` 登录。

使用 Codex 登录态前先确认：

```bash
codex login status
```

Windows 也可以在初始化后运行：

```powershell
.\scripts\run-local.ps1
```

## 首次配置

1. 管理员登录“系统”页，配置 GitHub 或 GitCode 机器人用户名和 Token。
2. 在“仓库”页添加允许使用的公共仓库。
3. 刷新仓库 Issue，在“Issue 收件箱”中人工派发。
4. 在“任务”页跟踪开发、代码检视、修正和 PR 状态。
5. 按需在“系统”页配置飞书机器人。

凭据在写入 SQLite 前使用 `CODERUS_CREDENTIAL_ENCRYPTION_KEY` 加密，网页不会回显已保存的 Token。Coderus 创建或复用机器人 Fork，所有开发分支只推送到 Fork。

## 飞书命令

群聊中需要先 `@` 机器人，单聊可以直接发送命令。

```text
帮助
状态
任务
任务 RE-N
派发 <Issue URL>
检视 <Pull Request URL>
```

## 文档

- [系统架构](docs/architecture.md)
- [配置说明](docs/configuration.md)
- [部署与安全发布](docs/deployment.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)

## 开发验证

```bash
uv run python scripts/check-public-release.py --root .
uv run ruff check coderus tests scripts/check-public-release.py
uv run pytest -q
```

## 许可证

Coderus 使用 [Apache License 2.0](LICENSE)。
