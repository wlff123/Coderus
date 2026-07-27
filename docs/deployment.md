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
sudo -u coderus -H bash -lc 'cd /opt/coderus && uv run coderus init'
```

签名归档由稳定目录中的独立 bootstrap 验证。系统 Python 必须预装 `cryptography`；bootstrap 不导入当前版本或候选版本的 Coderus 代码。首次建立版本布局会自动安装它。已经运行旧版布局的机器，在第一次签名升级前执行一次：

```bash
sudo install -d -o coderus -g coderus -m 0755 /opt/coderus/bootstrap
sudo install -o coderus -g coderus -m 0444 \
  coderus/release_bootstrap.py \
  /opt/coderus/bootstrap/release-bootstrap.py
sudo -u coderus /usr/bin/python3 -c 'import cryptography'
```

如系统 Python 不是 `/usr/bin/python3`，通过 `CODERUS_BOOTSTRAP_PYTHON` 指定已审核且安装了 `cryptography` 的解释器。bootstrap 文件只在人工审核升级安装链路时更新，普通候选包不能覆盖它。

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

首次构建前生成一对 Ed25519 发布密钥。私钥只保存在受控开发机或 CI Secret 中，部署机只保存公钥：

```powershell
uv run python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K; from cryptography.hazmat.primitives.serialization import Encoding as E, PrivateFormat as PF, PublicFormat as UF, NoEncryption as N; k=K.generate(); open('release-signing-key.pem','wb').write(k.private_bytes(E.PEM,PF.PKCS8,N())); open('release-public-key.pem','wb').write(k.public_key().public_bytes(E.PEM,UF.SubjectPublicKeyInfo))"
.\scripts\build-release.ps1 -SigningKey .\release-signing-key.pem
```

`release-signing-key.pem` 和 `release-public-key.pem` 都不能提交到代码仓。通过独立安全通道将公钥安装为 `/opt/coderus/release-public-key.pem`；可用 `CODERUS_RELEASE_PUBLIC_KEY` 覆盖该路径。

构建过程依次执行公开发布扫描、Ruff、完整 pytest，并生成：

```text
dist/releases/coderus-<release-id>.tar.gz
```

发布包只包含源码、测试、通用脚本、README、示例配置和锁文件，不包含配置、密钥、数据库或工作区。`release.json` 使用 Ed25519 签名，安装、预览和切换时均由部署机固定公钥验证来源。

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
6. 在服务停止窗口内更新 `previous` 和 `current` 链接；
7. 依次验证 `maintenance` 和 `active` 模式。

实际切换阶段共享默认 15 秒硬截止。任一步失败会恢复旧版本；如果已经创建数据库备份，也会恢复发布前数据库。自动回滚失败时写入 `data/ROLLBACK_FAILED` 并保持排空状态，避免继续写入。

## 手工代码回滚

```bash
bash scripts/rollback-release.sh
```

手工回滚只切换到 `previous` 代码版本，不自动恢复历史数据库。回滚脚本会先读取目标版本的 Schema 兼容范围；不兼容时保持当前服务和数据库不变并拒绝回滚。需要破坏性迁移的版本必须同时提供经过验证的数据库恢复方案。

发布成功记录通过临时文件、`fsync` 和原子替换写入 `data/release-history/`，默认保留最近 20 条；默认另外保留最近 5 个版本目录和 20 个数据库备份，`current`、`previous` 永不被清理。可分别使用 `CODERUS_RELEASE_HISTORY_RETAIN`、`CODERUS_RELEASE_RETAIN` 和 `CODERUS_BACKUP_RETAIN` 调整。历史写入完成前排空闸门不会解除；非关键的过期文件清理失败只记录警告，不回滚已经健康运行的新版本。

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
