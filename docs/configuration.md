# 配置说明

Coderus 使用两个本地文件：

- `config.yaml`：非敏感运行配置；
- `secrets.env`：会话密钥、管理员初始密码、凭据加密主密钥和可选环境变量凭据。

首次执行 `coderus init` 会生成这两个文件。它们已被 `.gitignore` 排除，不得提交。

## 基础配置

完整结构参见 [`config.example.yaml`](../config.example.yaml)。主要配置包括：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `server.mode` | `local` | `local` 或 `public` |
| `server.bind` | `127.0.0.1` | Manager 只允许监听回环地址 |
| `server.port` | `18082` | 控制面板端口 |
| `database.path` | `data/coderus.db` | SQLite 路径 |
| `workspace.root` | `data/workspaces` | Issue 开发工作目录 |
| `artifacts.root` | `data/artifacts` | Agent 结构化产物目录 |
| `scheduler.global_task_limit` | `8` | 全局并行任务数 |
| `scheduler.per_user_task_limit` | `2` | 单用户并行任务数 |
| `scheduler.max_agent_processes` | `16` | Codex 子进程上限 |
| `codex.binary` | `codex` | Codex CLI 路径或命令名 |
| `codex.sandbox_mode` | `workspace-write` | Issue Agent 沙箱策略 |

## 密钥

`secrets.env` 由初始化命令生成，至少包含：

```text
CODERUS_SESSION_SECRET=<random-session-secret>
CODERUS_BOOTSTRAP_ADMIN_PASSWORD=<initial-admin-password>
CODERUS_CREDENTIAL_ENCRYPTION_KEY=<random-encryption-key>
```

`CODERUS_CREDENTIAL_ENCRYPTION_KEY` 用于加密数据库中的 GitHub、GitCode 和飞书凭据。主密钥丢失后无法解密已有凭据，需要重新录入。

## GitHub 和 GitCode

推荐由管理员在“系统”页面分别保存平台用户名和 Token。系统先验证 Token 身份，再加密保存并立即更新运行时，不在网页回显 Token。

平台账号至少需要：

- 读取公共仓库和 Issue；
- 创建或访问自己的 Fork；
- 向 Fork 推送分支；
- 读取和创建 Pull Request；
- 读取和创建 PR 评论。

也可使用以下环境变量作为回退：

```text
CODERUS_GITHUB_TOKEN=<github-token>
CODERUS_GITCODE_TOKEN=<gitcode-token>
```

GitCode 的 Fork、PR 发布和代码检视要求网页中保存经过用户名验证的 Token。

## Codex 与模型服务

默认使用当前服务用户的 Codex CLI 登录态。先执行：

```bash
codex login status
```

使用兼容 OpenAI Responses API 的服务时，同时配置：

```text
CODERUS_MODEL_BASE_URL=https://api.example.com/v1
CODERUS_MODEL_API_KEY=<model-api-key>
```

并在 `config.yaml` 中设置 `codex.model`。Coderus 通过仅监听回环地址的短期凭据代理向 Codex 提供访问能力，真实模型 Key 不进入任务环境。

## 飞书

飞书设置只在“系统 > 飞书”页面保存。应用需要：

- 开启机器人能力；
- 订阅 `im.message.receive_v1`；
- 授予 `im:message:send_as_bot`；
- 授予 `im:message.p2p_msg:readonly`；
- 授予 `im:message.group_at_msg:readonly`。

使用长连接时不需要公网回调地址。群聊只接收明确 `@` 当前机器人的消息，单聊直接接收文本命令。

## 公网模式

Coderus 即使在公网模式下也只监听回环地址。将 `server.mode` 设为 `public`，并设置 HTTPS `server.public_url`，再通过 Caddy、Nginx 或其他反向代理暴露 80/443。不要暴露 Manager 内部端口、模型代理端口、SQLite 或工作目录。
