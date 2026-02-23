# LD Telegram Downloader

[English](README_en.md)

LD Telegram Downloader 是一个以 **WebUI 为核心** 的 Telegram 媒体下载与监控工具。它把“发现内容、执行下载、查看进度、暂停/继续、结果归档”放进同一个可视化界面，减少手工命令和脚本维护成本。

## 为什么有用（痛点）

当你需要长期下载频道/群组媒体时，常见问题是：

- 手工下载效率低，任务多了后容易漏
- 新内容持续出现，缺少统一监控入口
- 下载状态和失败重试不透明，排查耗时

这个项目把 Bot 触发与后台批量下载结合起来，并用 WebUI 持续展示状态，让流程从“临时手工操作”变成“可持续运行”。

## 项目做什么（功能概览）

- Telegram 媒体下载：支持 `audio/document/photo/video/voice/video_note` 等类型
- Bot 指令触发：可通过 Telegram Bot 下发下载/转发相关动作
- WebUI 管理：查看实时下载速度、任务列表、已完成记录，支持暂停/继续
- 多任务并发：下载与上传队列可配置
- 文件归档策略：支持按聊天、时间、媒体类型等规则组织目录

## WebUI 特色（代码可验证）

已在 `module/web.py` 中实现并注册以下界面与接口：

- 页面路由：`/login`、`/`
- 状态接口：`/get_download_status`、`/get_download_list`
- 控制接口：`/set_download_state`
- 版本接口：`/get_app_version`

Web 页面 `module/templates/index.html` 提供：

- Downloading / Downloaded 双视图切换
- 下载进度条、实时速度刷新
- 一键暂停/继续下载状态切换

## 如何快速开始（Getting Started）

### 环境要求

- Python 3.8+
- Telegram API `api_id` / `api_hash`
- 可选：Telegram Bot Token（如需 Bot 指令触发）
- Docker / Docker Compose（推荐）

### Docker 运行

```bash
cp config.example.yaml config.yaml
cp data.example.yaml data.yaml
# 按需编辑 config.yaml

docker compose up -d --build
```

默认访问：`http://localhost:5000`

### 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp data.example.yaml data.yaml
python3 media_downloader.py
```

## 配置提示

- `config.yaml`：主配置（API、下载策略、WebUI、并发等）
- `data.yaml`：运行过程状态数据（如重试列表）
- 建议不要将真实 `config.yaml`、`data.yaml`、`sessions/`、`downloads/`、`log/` 上传公开仓库

## 在哪里获得帮助

- Issue：`https://github.com/leduchuong48-byte/ld_telegram_downloader/issues`
- 提问建议附带：复现步骤、脱敏后的配置片段、关键日志

## 维护者与贡献者

- Maintainer: `@leduchuong48-byte`

## 许可证

本项目包含 `LICENSE` 文件（MIT License）。

## 免责声明

使用本项目即表示你已阅读并同意 [免责声明](DISCLAIMER.md)。
