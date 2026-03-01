# ld_tg_downloader

> 面向 NAS/服务器的 Telegram 媒体下载与转发工具，核心是“全流程 WebUI + Bot 双入口”。

[English](./README_en.md)

## v3.2 更新

- 修复了部分场景下媒体下载失败的问题，提升 Bot 触发下载的稳定性。

## For Portainer/Synology Users

Copy this into Portainer stacks and hit Deploy. Done.

## Docker Compose

```yaml
services:
  ld_tg_downloader:
    image: leduchuong/ld_tg_downloader:latest
    container_name: ld_tg_downloader
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./downloads:/app/downloads
      - ./config.yaml:/app/config.yaml
      - ./accounts.yaml:/app/accounts.yaml
      - ./data.yaml:/app/data.yaml
      - ./configs:/app/configs
      - ./sessions:/app/sessions
      - ./temp:/app/temp
      - ./log:/app/log
```

## 项目最大特点

- 全流程 WebUI：登录后可在页面里完成账号添加、手机验证码登录、2FA 密码验证、Bot token 校验、任务创建、任务状态查看、配置修改。
- Bot 操作入口：支持通过 Bot 指令触发下载、转发、监听转发、停止任务与清理策略。
- 下载与转发一体化：既可以按频道历史区间下载，也可以做频道到频道转发和监听转发。
- 多账号架构：每个账号独立配置和会话文件，便于隔离管理。
- 可选云盘上传：支持通过 `upload_drive` 配置对接上传流程。

## 快速开始

1. 准备目录与文件。

```bash
mkdir -p downloads configs sessions temp log
touch config.yaml accounts.yaml data.yaml
```

2. 参考下面最小配置，先设置 `api_id`、`api_hash`，并设置 WebUI 密码 `web_login_secret`。
3. 启动容器：

```bash
docker compose up -d
```

4. 访问 `http://<你的主机IP>:5000` 进入 WebUI。

## WebUI 初次登录密码如何设置

WebUI 登录密码来自 `config.yaml` 的 `web_login_secret` 字段：

- 为空：不启用登录保护（不建议）。
- 非空：使用该密码登录 WebUI。

修改后执行 `docker compose up -d --force-recreate` 使配置生效。

## 最小配置示例（config.yaml）

```yaml
api_id: 123456
api_hash: "your_api_hash"
bot_token: ""
chat: []

media_types:
  - photo
  - video
  - document
  - audio
  - voice

file_formats:
  audio: [all]
  document: [all]
  video: [all]

save_path: /app/downloads
file_path_prefix:
  - chat_title
  - media_datetime

web_host: "0.0.0.0"
web_port: 5000
web_login_secret: "change_me"
language: ZH
```

## Bot 常用指令

- `/help` 查看帮助。
- `/download <chat_link> <start_id> <end_id> [filter]` 下载频道消息区间。
- `/forward <src_chat> <dst_chat> <start_id> <end_id> [filter]` 批量转发。
- `/listen_forward <src_chat> <dst_chat> [filter]` 监听源频道并转发。
- `/stop` 停止当前下载/转发任务。
- `/cleanup on|off` 开关“转发/上传后自动清理”。
- `/forward-clean` 立即清理 forward 目录。
- `/forward-limit 20GB` 设置 forward 目录容量上限。

## UI 界面展示（高清）

### 登录页

![WebUI Login](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_login_hd.png)

### 多账号仪表盘

![WebUI Dashboard](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_dashboard_hd.png)

### 任务管理（下载/转发/监听）

![WebUI Tasks](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_tasks_hd.png)

### 下载监控

![WebUI Downloads](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_downloads_hd.png)

### 配置编辑

![WebUI Config](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_config_hd.png)

### 频道管理

![WebUI Chats](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_chats_hd.png)

### Bot 操作示例

![Bot Flow](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/bot_workflow_hd.png)

## 数据与隐私建议

- `sessions/`、`temp/`、`log/`、`downloads/` 都属于运行态目录，不建议提交到代码仓库。
- `config.yaml`、`accounts.yaml` 内请只保留示例值，不要写入真实 API 密钥、Bot Token、手机号。
- 分享镜像或仓库前，先检查并清理会话文件（如 `*.session*`）与日志文件。

## License

MIT
