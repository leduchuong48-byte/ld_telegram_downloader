# ld_tg_downloader

> Telegram media downloader/forwarder for NAS and self-hosted servers, focused on a full WebUI + Bot workflow.

[中文](./README.md)

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

## Highlights

- End-to-end WebUI flow: account onboarding, phone code/2FA auth, bot token validation, task creation, monitoring and config editing.
- Bot operations: download, forward, listen-forward, stop and cleanup commands.
- Unified workflow for download + forward jobs.
- Multi-account architecture with isolated config/session files.
- Optional cloud-drive upload via `upload_drive` settings.

## First WebUI Password Setup

Set `web_login_secret` in `config.yaml`:

- Empty value: login disabled (not recommended).
- Non-empty value: required password for WebUI login.

After editing, run:

```bash
docker compose up -d --force-recreate
```

## Bot Commands

- `/help`
- `/download <chat_link> <start_id> <end_id> [filter]`
- `/forward <src_chat> <dst_chat> <start_id> <end_id> [filter]`
- `/listen_forward <src_chat> <dst_chat> [filter]`
- `/stop`
- `/cleanup on|off`
- `/forward-clean`
- `/forward-limit 20GB`

## UI Preview

![WebUI Login](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_login_hd.png)
![WebUI Dashboard](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_dashboard_hd.png)
![WebUI Tasks](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_tasks_hd.png)
![WebUI Downloads](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_downloads_hd.png)
![WebUI Config](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_config_hd.png)
![WebUI Chats](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/webui_chats_hd.png)
![Bot Flow](https://raw.githubusercontent.com/leduchuong48-byte/ld_telegram_downloader/main/screenshot/bot_workflow_hd.png)

## License

MIT
