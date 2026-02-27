# LD Telegram Downloader

多账号 Telegram 媒体下载器，支持 WebUI 管理和 Bot 控制。

## 功能

- 多账号同时运行，每个账号独立配置
- WebUI 全中文界面（暗色主题），支持：
  - 账号登录/管理（无需构建时登录）
  - 链接下载、频道批量下载、转发、监听转发
  - 可视化过滤器构建器
  - 实时下载进度监控
  - 频道管理与历史记录
  - 完整配置编辑
- Bot 功能完整保留（/download, /forward, /get_info 等）
  - 容器启动后 Bot 自动启动并发送欢迎语
  - 支持发送 t.me 链接直接下载
- 支持下载私有群组（已加入）内的媒体
- 下载进度实时日志输出

## 部署

```sh
docker compose build
docker compose up -d
```

访问 `http://<host>:5000` 进入 WebUI，在界面中完成账号登录和配置。

## 配置

所有配置通过 WebUI 或 `config.yaml` / `configs/` 目录管理。

### 关键配置项

| 配置 | 说明 |
|------|------|
| `api_id` / `api_hash` | Telegram API 密钥，从 https://my.telegram.org/apps 获取 |
| `bot_token` | Bot Token，从 @BotFather 获取 |
| `media_types` | 下载的媒体类型：audio, document, photo, video, voice |
| `save_path` | 下载保存路径 |
| `max_download_task` | 最大并行下载任务数 |
| `max_concurrent_transmissions` | Pyrogram 底层并发传输数 |

### 代理配置

```yaml
proxy:
  scheme: socks5
  hostname: 127.0.0.1
  port: 1234
```
