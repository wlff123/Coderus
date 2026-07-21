# 部署与安全发布

## 本地运行

```bash
uv sync --locked --extra dev
uv run coderus init
uv run coderus serve --config config.yaml --secrets secrets.env
```

默认访问地址为 `http://127.0.0.1:18082`。

## Linux 目录布局

版本化部署脚本使用以下通用默认值：

- 服务用户：`coderus`；
- 运行根目录：`/opt/coderus`；
- 控制面板端口：`18082`；
- 候选预览端口：`18084`。

主机需要提供 `bash`、Git、curl、tar、uv、Codex CLI，以及包含 `flock`、`realpath`、`sha256sum` 和 `timeout` 的系统工具包。先建立专用服务用户和目录，再以该用户完成首次安装：

```bash
sudo useradd --system --create-home --shell /bin/bash coderus
sudo install -d -o coderus -g coderus /opt/coderus
sudo -u coderus -H git clone <your-coderus-repository-url> /opt/coderus
sudo -u coderus -H bash -lc 'cd /opt/coderus && uv sync --locked --extra dev'
sudo -u coderus -H bash -lc 'cd /opt/coderus && codex login'
sudo -u coderus -H bash -lc 'cd /opt/coderus && uv run coderus init'
```

如果 `uv` 不在服务用户的 `PATH`，请使用系统绝对路径执行上述命令，并将同一路径设置到 `CODERUS_UV`。初始化命令会输出管理员初始密码；应立即保存到密码管理器，并保护 `/opt/coderus/secrets.env`。

可通过环境变量覆盖：

```bash
export CODERUS_ROOT=/srv/coderus
export CODERUS_RUN_USER=coderus
export CODERUS_UV=/usr/local/bin/uv
export CODERUS_PORT=18082
```

运行根目录结构：

```text
/opt/coderus/
├── config.yaml
├── secrets.env
├── current -> releases/<release-id>
├── previous -> releases/<release-id>
├── releases/
├── incoming/
├── validation/
├── backups/
└── data/
```

配置、密钥和 `data/` 是共享运行状态；每个 `releases/<release-id>` 包含独立源码和 `.venv`。

## 首次建立版本布局

完成上述初始化后，由服务用户执行：

```bash
bash scripts/bootstrap-release-layout.sh
bash scripts/container-start.sh
```

启动和停止脚本校验 PID 命令行、配置路径和进程工作目录，避免误操作其他进程。

## 构建候选版本

在开发工作区执行：

```powershell
.\scripts\build-release.ps1
```

构建过程依次执行公开发布扫描、Ruff、完整 pytest，并生成：

```text
dist/releases/coderus-<release-id>.tar.gz
```

发布包只包含源码、测试、通用脚本、README、示例配置和锁文件，不包含配置、密钥、数据库或工作区。

## 安装和隔离预览

将发布包放入运行根目录的 `incoming/`，再执行：

```bash
bash scripts/install-release.sh incoming/coderus-<release-id>.tar.gz
bash scripts/preview-release.sh <release-id>
```

安装阶段为候选版本建立独立 `.venv` 并再次运行质量检查。预览阶段通过 SQLite backup API 创建生产数据库副本，使用独立工作区和端口启动 `preview`；不会启动任务调度、模型代理或飞书连接。只有预览通过后才生成 `VERIFIED`。

## 受控切换

```bash
bash scripts/promote-release.sh <release-id>
```

切换流程：

1. 再次验证候选源码、锁文件和虚拟环境摘要；
2. 获取全局发布锁并创建排空标记；
3. 等待 Issue、PR 检视、仓库同步、Agent 和飞书命令全部空闲；
4. 停止旧服务并再次确认数据库没有在途任务；
5. 使用 SQLite backup API 创建发布前备份；
6. 原子更新 `previous` 和 `current`；
7. 依次验证 `maintenance` 和 `active` 模式。

实际切换阶段共享默认 15 秒硬截止。任一步失败会恢复旧版本；如果已经创建数据库备份，也会恢复发布前数据库。自动回滚失败时写入 `data/ROLLBACK_FAILED` 并保持排空状态，避免继续写入。

## 手工代码回滚

```bash
bash scripts/rollback-release.sh
```

手工回滚只切换到 `previous` 代码版本，不自动恢复历史数据库，因此数据库 Schema 必须保持向后兼容。

## SSH 隧道

控制面板只监听容器或远端主机回环地址时，可在 Windows 运行：

```powershell
.\scripts\start-ssh-tunnel.ps1 `
  -SshHost server.example.com `
  -SshUser coderus `
  -SshPort 22 `
  -LocalPort 18082 `
  -RemotePort 18082
```

默认私钥为 `$HOME/.ssh/coderus_tunnel_ed25519`。脚本启用批处理认证、连接心跳和自动重连，不保存口令。

## HTTPS 反向代理

示例 Caddy 配置：

```caddyfile
coderus.example.com {
    reverse_proxy 127.0.0.1:18082
}
```

只开放 80/443。不要直接暴露 18082、模型代理端口或任何数据文件。
