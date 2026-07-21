# 贡献指南

## 开发环境

需要 Python 3.12 或 3.13、Git、uv 和 Codex CLI。

```bash
git clone <repository-url>
cd Coderus
uv sync --locked --extra dev
```

## 修改原则

- 保持改动聚焦，不在功能修改中混入无关重构。
- 新行为和缺陷修复先增加能复现需求的测试。
- 不提交配置、密钥、Token、数据库、日志、工作区或发布包。
- 不在测试和文档中写入真实账号、个人路径或真实仓库凭据。
- 平台相关行为通过现有 Forge、Provider 和 Publisher 接口实现。
- 所有数据库变化必须向后兼容现有安全回滚流程。

## 提交前检查

```bash
uv run python scripts/check-public-release.py --root .
uv run ruff check coderus tests scripts/check-public-release.py
uv run pytest -q
```

涉及 shell 脚本时还应运行 `bash -n scripts/*.sh`。涉及页面时应验证桌面和移动视口，并检查登录、筛选和主要操作流程。

## Pull Request

PR 描述应说明问题、方案、验证命令和遗留风险。不要在 Issue 或 PR 中粘贴 Token、密钥、完整配置文件、数据库内容或包含个人信息的日志。
