# LD Telegram Downloader

[中文](README.md)

LD Telegram Downloader is a **WebUI-first** Telegram media downloading and monitoring tool. It combines discovery, download execution, progress visibility, pause/resume control, and archive output into one interface.

## Why This Project Is Useful (Pain Points)

For long-running channel/group media collection, common issues are:

- Manual downloads are slow and error-prone
- New content appears continuously, but tracking is fragmented
- Download status and retry visibility are weak, making troubleshooting expensive

This project combines bot-triggered actions with scheduled/batch downloading and keeps runtime status visible in a single WebUI.

## What the Project Does (Features)

- Telegram media download for `audio/document/photo/video/voice/video_note`
- Bot-triggered operations (download/forward workflow)
- WebUI for real-time speed, task list, finished records, pause/resume
- Configurable concurrent download/upload workers
- Structured output paths by chat/time/media type rules

## WebUI Highlights (Code-Verified)

Implemented in `module/web.py`:

- Page routes: `/login`, `/`
- Status APIs: `/get_download_status`, `/get_download_list`
- Control API: `/set_download_state`
- Version API: `/get_app_version`

`module/templates/index.html` provides:

- Downloading/Downloaded tabs
- Real-time progress and speed refresh
- One-click pause/resume control

## Getting Started

### Prerequisites

- Python 3.8+
- Telegram API `api_id` / `api_hash`
- Optional: Telegram Bot Token (for bot-triggered workflow)
- Docker / Docker Compose (recommended)

### Run with Docker

```bash
cp config.example.yaml config.yaml
cp data.example.yaml data.yaml
# edit config.yaml

docker compose up -d --build
```

Open: `http://localhost:5000`

### Run Locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp data.example.yaml data.yaml
python3 media_downloader.py
```

## Configuration Notes

- `config.yaml`: main configuration (API, download strategy, WebUI, concurrency)
- `data.yaml`: runtime state (for example retry-related data)
- Do not publish real `config.yaml`, `data.yaml`, `sessions/`, `downloads/`, or `log/` to public repositories

## Where to Get Help

- Issues: `https://github.com/leduchuong48-byte/ld_telegram_downloader/issues`
- Please include sanitized config snippets, logs, and reproducible steps

## Maintainers and Contributors

- Maintainer: `@leduchuong48-byte`

## License

See `LICENSE` (MIT License).

## Disclaimer

By using this project, you acknowledge and agree to the [Disclaimer](DISCLAIMER.md).
