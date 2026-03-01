"""Single account runtime instance.

Wraps one Telegram user client + optional bot client + Application
into a cohesive unit that can be started/stopped independently.
"""

import asyncio
import os
import time
from typing import Callable, Dict, List, Optional, Union

import pyrogram
import utils
from loguru import logger
from ruamel import yaml as _ryaml

from module.account_manager import AccountConfig, AccountManager, AccountStatus
from module.app import (
    Application,
    ChatDownloadConfig,
    DownloadStatus,
    TaskNode,
    TaskType,
)
from module.bot import DownloadBot
from module.download_stat import (
    DownloadState,
    get_download_result,
    get_download_state,
    set_download_state,
    update_download_status,
)
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    record_download_status,
    report_bot_download_status,
    report_bot_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from utils.file_management import (
    cleanup_dir_by_freeing,
    clear_dir_contents,
    get_dir_size,
)
from utils.format import format_byte, parse_size_to_bytes, validate_title
from utils.meta_data import MetaData


class AccountInstance:
    """Runtime instance for a single Telegram account.

    Lifecycle:
        1. __init__(account_cfg, manager)
        2. start(loop) — connect client + bot, launch workers
        3. stop() — graceful shutdown
    """

    def __init__(self, account_cfg: AccountConfig, manager: AccountManager):
        self.account_cfg = account_cfg
        self.manager = manager
        self.account_id = account_cfg.account_id

        # paths
        config_path = manager.get_account_config_path(self.account_id)
        data_path = manager.get_account_data_path(self.account_id)

        # Application instance (per-account config)
        self.app = Application(
            config_file=config_path,
            app_data_file=data_path,
            application_name=account_cfg.session_name,
        )

        self.client: Optional[HookClient] = None
        self.bot_instance: Optional[DownloadBot] = None
        self.tasks: List[asyncio.Task] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.upload_queue: asyncio.Queue = asyncio.Queue()
        self.is_running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def display_name(self) -> str:
        phone = self.account_cfg.phone
        if phone:
            return f"{self.account_id} ({phone})"
        return self.account_id

    def load_config(self) -> bool:
        """Load per-account config. Returns True on success."""
        try:
            self.app.load_config()
            # override api credentials from account registry (source of truth)
            self.app.api_id = self.account_cfg.api_id
            self.app.api_hash = self.account_cfg.api_hash
            self.app.bot_token = self.account_cfg.bot_token
            return True
        except Exception as e:
            logger.error("[{}] Failed to load config: {}", self.account_id, e)
            return False

    async def start(self, loop: asyncio.AbstractEventLoop):
        """Start the account: connect client, start bot, launch workers."""
        self._loop = loop
        self.is_running = True

        if not self.load_config():
            return False

        # Ensure app.loop points to the running event loop (needed by bot.py)
        self.app.loop = loop

        self.app.pre_run()

        # init queues
        maxsize = self.app.queue_max_size or self.app.max_download_task * 20
        self.queue = asyncio.Queue(maxsize=maxsize)
        upload_maxsize = self.app.upload_queue_max_size or self.app.max_upload_task * 20
        self.upload_queue = asyncio.Queue(maxsize=upload_maxsize)

        # create user client
        self.client = HookClient(
            self.account_cfg.session_name,
            api_id=self.app.api_id,
            api_hash=self.app.api_hash,
            proxy=self.app.proxy,
            workdir=self.manager.sessions_dir,
            start_timeout=self.app.start_timeout,
        )

        set_max_concurrent_transmissions(
            self.client,
            self.app.max_download_concurrent_transmissions,
            self.app.max_upload_concurrent_transmissions,
        )

        try:
            await self.client.start()
            logger.success("[{}] User client started", self.display_name)
        except Exception as e:
            logger.error(
                "[{}] Failed to start user client: {}",
                self.display_name,
                e,
            )
            self.is_running = False
            return False

        # start download workers
        for _ in range(self.app.max_download_task):
            task = loop.create_task(self._worker())
            self.tasks.append(task)

        # start upload workers
        for _ in range(self.app.max_upload_task):
            task = loop.create_task(self._upload_worker())
            self.tasks.append(task)

        # start bot if configured
        if self.account_cfg.bot_token:
            try:
                self.bot_instance = DownloadBot()
                await asyncio.wait_for(
                    self.bot_instance.start(
                        self.app,
                        self.client,
                        self._add_download_task,
                        self._download_chat_task,
                    ),
                    timeout=self.app.start_timeout or 60,
                )
                logger.success("[{}] Bot started", self.display_name)
                # Send welcome message to admin
                try:
                    me = await self.client.get_me()
                    bot_me = self.bot_instance.bot_info
                    bot_name = (
                        f"@{bot_me.username}" if bot_me and bot_me.username else "Bot"
                    )
                    user_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
                    welcome = (
                        f"🟢 **TG Media Downloader 已上线**\n\n"
                        f"📦 版本：`{utils.__version__}`\n"
                        f"📡 账号：`{user_name}`\n"
                        f"🤖 机器人：{bot_name}\n"
                        f"🌐 WebUI：`http://<host>:{self.app.web_port}`\n\n"
                        f"🔗 仓库：https://github.com/leduchuong48-byte/ld_telegram_downloader\n\n"
                        f"发送 /help 查看所有可用命令。"
                    )
                    await self.bot_instance.bot.send_message(me.id, welcome)
                except Exception as exc:
                    logger.debug(
                        "[{}] Welcome message failed: {}", self.display_name, exc
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "[{}] Bot start timed out after {}s, continuing without bot",
                    self.display_name,
                    self.app.start_timeout or 60,
                )
                self.bot_instance = None
            except Exception as e:
                logger.warning(
                    "[{}] Failed to start bot: {}",
                    self.display_name,
                    e,
                )
                self.bot_instance = None

        # start downloading configured chats
        loop.create_task(self._download_all_chat())

        logger.success("[{}] Account instance fully started", self.display_name)
        return True

    async def stop(self):
        """Graceful shutdown."""
        self.is_running = False
        self.app.is_running = False

        if self.bot_instance:
            try:
                self.bot_instance.update_config()
                self.bot_instance.is_running = False
                if self.bot_instance.reply_task:
                    self.bot_instance.reply_task.cancel()
                self.bot_instance.stop_task("all")
                if getattr(self.bot_instance, "poller", None):
                    self.bot_instance.poller.stop()
                if getattr(self.bot_instance, "_poller_task", None):
                    self.bot_instance._poller_task.cancel()
                if getattr(self.bot_instance, "bot_media_client", None):
                    await self.bot_instance.bot_media_client.stop()
                if self.bot_instance.bot:
                    await self.bot_instance.bot.stop()
            except Exception as e:
                logger.warning("[{}] Error stopping bot: {}", self.account_id, e)

        if self.client:
            try:
                await self.client.stop()
            except Exception as e:
                logger.warning("[{}] Error stopping client: {}", self.account_id, e)

        for task in self.tasks:
            task.cancel()
        self.tasks.clear()

        # persist config
        try:
            self.app.update_config()
        except Exception as e:
            logger.warning("[{}] Error saving config: {}", self.account_id, e)

        logger.info("[{}] Account instance stopped", self.account_id)

    # ── internal workers (mirrors media_downloader.py logic) ─────

    async def _add_download_task(
        self,
        message: pyrogram.types.Message,
        node: TaskNode,
    ):
        """Add download task to this account's queue."""
        if message.empty:
            logger.warning(
                "[{}] _add_download_task: message {} is empty, skipping",
                self.account_id,
                message.id,
            )
            return False
        node.download_status[message.id] = DownloadStatus.Downloading
        logger.info(
            "[{}] _add_download_task: putting msg_id={} into queue (qsize={})",
            self.account_id,
            message.id,
            self.queue.qsize(),
        )
        await self.queue.put((message, node))
        node.total_task += 1
        return True

    async def _download_chat_task(
        self,
        client: pyrogram.Client,
        chat_download_config: ChatDownloadConfig,
        node: TaskNode,
        use_queue: bool = True,
    ):
        """Download all messages for a chat config."""
        from media_downloader import download_task

        try:
            logger.info(
                "[{}] _download_chat_task started chat_id={} limit={} offset_id={}",
                self.account_id,
                node.chat_id,
                node.limit,
                chat_download_config.last_read_message_id,
            )

            messages_iter = get_chat_history_v2(
                client,
                node.chat_id,
                limit=node.limit,
                max_id=node.end_offset_id,
                offset_id=chat_download_config.last_read_message_id,
                reverse=True,
            )

            chat_download_config.node = node

            if chat_download_config.ids_to_retry:
                skipped_messages = await client.get_messages(
                    chat_id=node.chat_id,
                    message_ids=chat_download_config.ids_to_retry,
                )
                for message in skipped_messages:
                    if use_queue:
                        await self._add_download_task(message, node)
                    else:
                        node.total_task += 1
                        await download_task(
                            client, message, node, app_override=self.app
                        )

            async for message in messages_iter:
                meta_data = MetaData()
                caption = message.caption
                if caption:
                    caption = validate_title(caption)
                    self.app.set_caption_name(
                        node.chat_id, message.media_group_id, caption
                    )
                    self.app.set_caption_entities(
                        node.chat_id,
                        message.media_group_id,
                        message.caption_entities,
                    )
                else:
                    caption = self.app.get_caption_name(
                        node.chat_id, message.media_group_id
                    )
                set_meta_data(meta_data, message, caption)

                if self.app.need_skip_message(chat_download_config, message.id):
                    continue

                if self.app.exec_filter(chat_download_config, meta_data):
                    if use_queue:
                        await self._add_download_task(message, node)
                    else:
                        node.total_task += 1
                        await download_task(
                            client, message, node, app_override=self.app
                        )
                else:
                    node.download_status[message.id] = DownloadStatus.SkipDownload

            chat_download_config.need_check = True
            chat_download_config.total_task = node.total_task
            node.is_running = True
            logger.info(
                "[{}] _download_chat_task finished chat_id={} total={}",
                self.account_id,
                node.chat_id,
                node.total_task,
            )
        except Exception as e:
            logger.exception(
                "[{}] _download_chat_task FAILED chat_id={}: {}",
                self.account_id,
                node.chat_id,
                e,
            )

    async def _worker(self):
        """Download worker for this account."""
        from media_downloader import download_task

        logger.info(
            "[{}] _worker started, queue_size={}", self.account_id, self.queue.qsize()
        )
        while self.is_running:
            try:
                item = await self.queue.get()
                message = item[0]
                node: TaskNode = item[1]
                logger.info(
                    "[{}] _worker dequeued msg_id={} chat_id={}",
                    self.account_id,
                    message.id,
                    node.chat_id,
                )

                if node.is_stop_transmission:
                    continue

                if node.client:
                    await download_task(
                        node.client, message, node, app_override=self.app
                    )
                else:
                    await download_task(
                        self.client, message, node, app_override=self.app
                    )
            except Exception as e:
                logger.exception("[{}] Worker error: {}", self.account_id, e)

    async def _upload_worker(self):
        """Upload worker for this account."""
        from media_downloader import upload_task

        while self.is_running:
            try:
                item = await self.upload_queue.get()
                message, node, download_status, file_name = item

                if node.is_stop_transmission:
                    continue

                if node.client:
                    await upload_task(
                        node.client, message, node, download_status, file_name
                    )
                else:
                    await upload_task(
                        self.client, message, node, download_status, file_name
                    )
            except Exception as e:
                logger.exception("[{}] Upload worker error: {}", self.account_id, e)

    async def _download_all_chat(self):
        """Download all configured chats for this account."""
        for key, value in self.app.chat_download_config.items():
            value.node = TaskNode(chat_id=key)
            try:
                await self._download_chat_task(self.client, value, value.node)
            except Exception as e:
                logger.warning(
                    "[{}] Download {} error: {}",
                    self.account_id,
                    key,
                    e,
                )
            finally:
                value.need_check = True

    # ══════════════════════════════════════════════════════════════
    # WebUI-callable methods
    # ══════════════════════════════════════════════════════════════

    _task_counter: int = 0
    _active_tasks: Dict[int, TaskNode] = {}

    def _gen_task_id(self) -> int:
        self._task_counter += 1
        return self._task_counter

    # ── download from WebUI ──────────────────────────────────────

    async def web_download(
        self,
        chat_id_or_link: str,
        start_id: int = 0,
        end_id: int = 0,
        download_filter: str = "",
    ) -> dict:
        """Start a download task from WebUI (equivalent to Bot /download)."""
        if not self.client:
            return {"ok": False, "error": "Client not connected"}

        from module.pyrogram_extension import parse_link

        resolved_chat_id, _, _ = await parse_link(self.client, chat_id_or_link)
        if not resolved_chat_id:
            try:
                resolved_chat_id = int(chat_id_or_link)
            except ValueError:
                return {"ok": False, "error": f"Cannot resolve: {chat_id_or_link}"}

        try:
            entity = await self.client.get_chat(resolved_chat_id)
        except Exception as e:
            return {"ok": False, "error": f"Cannot get chat: {e}"}

        limit = 0
        if end_id and end_id > start_id:
            limit = end_id - start_id + 1

        chat_download_config = ChatDownloadConfig()
        chat_download_config.last_read_message_id = start_id
        if download_filter:
            chat_download_config.download_filter = download_filter

        task_id = self._gen_task_id()
        node = TaskNode(
            chat_id=entity.id,
            limit=limit,
            start_offset_id=start_id,
            end_offset_id=end_id,
            download_filter=download_filter or None,
            task_id=task_id,
        )
        self._active_tasks[task_id] = node

        self._loop.create_task(
            self._download_chat_task(self.client, chat_download_config, node)
        )

        chat_title = getattr(entity, "title", None) or str(entity.id)
        return {
            "ok": True,
            "task_id": task_id,
            "chat_title": chat_title,
            "range": f"{start_id} - {end_id or 'latest'}",
        }

    # ── forward from WebUI ───────────────────────────────────────

    async def web_forward(
        self,
        from_chat_link: str,
        to_chat_link: str,
        start_id: int = 0,
        end_id: int = 0,
        download_filter: str = "",
    ) -> dict:
        """Start a forward task from WebUI (equivalent to Bot /forward)."""
        if not self.client:
            return {"ok": False, "error": "Client not connected"}

        from module.pyrogram_extension import parse_link

        src_chat_id, _, _ = await parse_link(self.client, from_chat_link)
        dst_chat_id, _, topic_id = await parse_link(self.client, to_chat_link)

        if not src_chat_id or not dst_chat_id:
            return {"ok": False, "error": "Cannot resolve source or destination chat"}

        limit = 0
        if end_id and end_id > start_id:
            limit = end_id - start_id + 1

        try:
            src_entity = await self.client.get_chat(src_chat_id)
            dst_entity = await self.client.get_chat(dst_chat_id)
        except Exception as e:
            return {"ok": False, "error": f"Cannot get chat info: {e}"}

        task_id = self._gen_task_id()
        node = TaskNode(
            chat_id=src_entity.id,
            upload_telegram_chat_id=dst_entity.id,
            limit=limit,
            start_offset_id=start_id,
            end_offset_id=end_id,
            download_filter=download_filter or None,
            task_type=TaskType.Forward,
            task_id=task_id,
            topic_id=topic_id or 0,
        )
        node.upload_user = self.client
        self._active_tasks[task_id] = node

        chat_download_config = ChatDownloadConfig()
        chat_download_config.last_read_message_id = start_id
        if download_filter:
            chat_download_config.download_filter = download_filter

        self._loop.create_task(
            self._download_chat_task(self.client, chat_download_config, node)
        )

        src_title = getattr(src_entity, "title", None) or str(src_entity.id)
        dst_title = getattr(dst_entity, "title", None) or str(dst_entity.id)
        return {
            "ok": True,
            "task_id": task_id,
            "from": src_title,
            "to": dst_title,
            "range": f"{start_id} - {end_id or 'latest'}",
        }

    # ── listen forward from WebUI ────────────────────────────────

    _listen_tasks: Dict[str, TaskNode] = {}

    async def web_listen_forward(
        self,
        from_chat_link: str,
        to_chat_link: str,
        download_filter: str = "",
    ) -> dict:
        """Start a listen-forward task (equivalent to Bot /listen_forward)."""
        if not self.client:
            return {"ok": False, "error": "Client not connected"}

        from module.pyrogram_extension import parse_link

        src_chat_id, _, _ = await parse_link(self.client, from_chat_link)
        dst_chat_id, _, topic_id = await parse_link(self.client, to_chat_link)

        if not src_chat_id or not dst_chat_id:
            return {"ok": False, "error": "Cannot resolve source or destination chat"}

        key = f"{src_chat_id}_{dst_chat_id}"
        if key in self._listen_tasks:
            return {"ok": False, "error": "Already listening on this pair"}

        task_id = self._gen_task_id()
        node = TaskNode(
            chat_id=src_chat_id,
            upload_telegram_chat_id=dst_chat_id,
            download_filter=download_filter or None,
            task_type=TaskType.ListenForward,
            task_id=task_id,
            topic_id=topic_id or 0,
        )
        node.upload_user = self.client
        node.is_running = True
        self._listen_tasks[key] = node
        self._active_tasks[task_id] = node

        return {"ok": True, "task_id": task_id, "key": key}

    async def web_stop_listen(self, key: str) -> dict:
        node = self._listen_tasks.pop(key, None)
        if node:
            node.stop_transmission()
            self._active_tasks.pop(node.task_id, None)
            return {"ok": True}
        return {"ok": False, "error": "Not found"}

    # ── download single link ─────────────────────────────────────

    async def web_download_link(self, link: str) -> dict:
        """Download a single message by t.me link."""
        if not self.client:
            return {"ok": False, "error": "Client not connected"}

        from module.pyrogram_extension import parse_link

        chat_id, message_id, _ = await parse_link(self.client, link)
        if not chat_id or not message_id:
            return {"ok": False, "error": "Invalid link"}

        try:
            msg = await self.client.get_messages(
                chat_id=chat_id, message_ids=message_id
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

        if not msg or msg.empty:
            return {"ok": False, "error": "Message not found or empty"}

        task_id = self._gen_task_id()
        node = TaskNode(chat_id=chat_id, task_id=task_id)
        node.is_running = True
        self._active_tasks[task_id] = node

        await self._add_download_task(msg, node)
        return {"ok": True, "task_id": task_id, "message_id": message_id}

    # ── task management ──────────────────────────────────────────

    def web_get_tasks(self) -> list:
        result = []
        # Pre-fetch download snapshot for speed/progress aggregation
        download_result = get_download_result()
        for tid, node in self._active_tasks.items():
            # Aggregate per-file stats for this task
            task_downloaded_bytes = 0
            task_total_bytes = 0
            task_speed_bps = 0
            file_count = 0

            chat_ids = list(download_result.keys())
            for chat_id in chat_ids:
                messages = download_result.get(chat_id)
                if not messages:
                    continue
                msg_ids = list(messages.keys())
                for msg_id in msg_ids:
                    entry = messages.get(msg_id)
                    if not entry:
                        continue
                    if entry.get("task_id") != tid:
                        continue
                    task_downloaded_bytes += entry.get("down_byte", 0)
                    task_total_bytes += entry.get("total_size", 0)
                    task_speed_bps += entry.get("download_speed", 0)
                    file_count += 1

            eta = (
                round((task_total_bytes - task_downloaded_bytes) / task_speed_bps)
                if task_speed_bps > 0 and task_downloaded_bytes < task_total_bytes
                else 0
            )

            result.append(
                {
                    "task_id": tid,
                    "chat_id": str(node.chat_id),
                    "type": node.task_type.name
                    if hasattr(node.task_type, "name")
                    else str(node.task_type),
                    "total": node.total_task,
                    "downloaded": node.success_download_task,
                    "failed": node.failed_download_task,
                    "skipped": node.skip_download_task,
                    "forwarded": node.success_forward_task,
                    "running": node.is_running,
                    "stopped": node.is_stop_transmission,
                    "downloaded_bytes": task_downloaded_bytes,
                    "total_bytes": task_total_bytes,
                    "speed_bps": int(task_speed_bps),
                    "speed_display": format_byte(int(task_speed_bps)) + "/s",
                    "eta_seconds": eta,
                    "active_files": file_count,
                }
            )
        return result

    def web_stop_task(self, task_id: int) -> dict:
        node = self._active_tasks.get(task_id)
        if not node:
            return {"ok": False, "error": "Task not found"}
        node.stop_transmission()
        return {"ok": True}

    # ── get chat info ────────────────────────────────────────────

    async def web_get_chat_info(self, chat_id_or_link: str) -> dict:
        if not self.client:
            return {"ok": False, "error": "Client not connected"}

        from module.pyrogram_extension import parse_link

        resolved_id, _, _ = await parse_link(self.client, chat_id_or_link)
        if not resolved_id:
            try:
                resolved_id = int(chat_id_or_link)
            except ValueError:
                return {"ok": False, "error": f"Cannot resolve: {chat_id_or_link}"}

        try:
            chat = await self.client.get_chat(resolved_id)
            return {
                "ok": True,
                "chat": {
                    "id": chat.id,
                    "title": getattr(chat, "title", None) or "",
                    "username": getattr(chat, "username", None) or "",
                    "type": str(chat.type),
                    "members_count": getattr(chat, "members_count", None),
                },
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── config management (read/write per-account yaml) ──────────

    def web_get_config(self) -> dict:
        _y = _ryaml.YAML()
        config_path = self.manager.get_account_config_path(self.account_id)
        if not os.path.exists(config_path):
            return {}
        with open(config_path, encoding="utf-8") as f:
            return dict(_y.load(f.read()) or {})

    def web_save_config(self, data: dict) -> dict:
        _y = _ryaml.YAML()
        config_path = self.manager.get_account_config_path(self.account_id)

        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                existing = _y.load(f.read()) or {}
        else:
            existing = {}

        existing.update(data)
        with open(config_path, "w", encoding="utf-8") as f:
            _y.dump(existing, f)

        # Sync bot_token to accounts.yaml if changed
        if "bot_token" in data:
            self.manager.set_bot_token(self.account_id, data["bot_token"])
            self.account_cfg = self.manager.get_account(self.account_id)

        if self.is_running:
            self.load_config()

        return {"ok": True}

    # ── chat (channel) management ────────────────────────────────

    def web_get_chats(self) -> list:
        config = self.web_get_config()
        chats = config.get("chat", [])
        return chats if isinstance(chats, list) else []

    def web_add_chat(self, chat_id: str, download_filter: str = "") -> dict:
        _y = _ryaml.YAML()
        config_path = self.manager.get_account_config_path(self.account_id)
        with open(config_path, encoding="utf-8") as f:
            config = _y.load(f.read()) or {}

        if "chat" not in config:
            config["chat"] = []

        for c in config["chat"]:
            if str(c.get("chat_id")) == str(chat_id):
                return {"ok": False, "error": "Chat already exists"}

        entry = {"chat_id": chat_id, "last_read_message_id": 0}
        if download_filter:
            entry["download_filter"] = download_filter
        config["chat"].append(entry)

        with open(config_path, "w", encoding="utf-8") as f:
            _y.dump(config, f)

        return {"ok": True}

    def web_remove_chat(self, chat_id: str) -> dict:
        _y = _ryaml.YAML()
        config_path = self.manager.get_account_config_path(self.account_id)
        with open(config_path, encoding="utf-8") as f:
            config = _y.load(f.read()) or {}

        chats = config.get("chat", [])
        config["chat"] = [c for c in chats if str(c.get("chat_id")) != str(chat_id)]

        with open(config_path, "w", encoding="utf-8") as f:
            _y.dump(config, f)

        return {"ok": True}

    def web_get_listen_forwards(self) -> list:
        result = []
        for key, node in self._listen_tasks.items():
            result.append(
                {
                    "key": key,
                    "from_chat": str(node.chat_id),
                    "to_chat": str(node.upload_telegram_chat_id),
                    "running": node.is_running,
                    "forwarded": node.success_forward_task,
                }
            )
        return result

    # ── forward to comments (equivalent to Bot /forward_to_comments) ─

    async def web_forward_to_comments(
        self,
        from_chat_link: str,
        to_chat_link: str,
        start_id: int = 0,
        end_id: int = 0,
        download_filter: str = "",
    ) -> dict:
        """Forward messages to a discussion/comment section.

        Same as web_forward but resolves the discussion message
        from the target link's message_id so replies land in comments.
        """
        if not self.client:
            return {"ok": False, "error": "Client not connected"}

        from module.pyrogram_extension import parse_link

        src_chat_id, _, _ = await parse_link(self.client, from_chat_link)
        dst_chat_id, target_msg_id, topic_id = await parse_link(
            self.client, to_chat_link
        )

        if not src_chat_id or not dst_chat_id:
            return {"ok": False, "error": "Cannot resolve source or destination chat"}

        limit = 0
        if end_id and end_id > start_id:
            limit = end_id - start_id + 1

        try:
            src_entity = await self.client.get_chat(src_chat_id)
            await self.client.get_chat(dst_chat_id)
        except Exception as e:
            return {"ok": False, "error": f"Cannot get chat info: {e}"}

        task_id = self._gen_task_id()
        node = TaskNode(
            chat_id=src_entity.id,
            upload_telegram_chat_id=dst_chat_id,
            limit=limit,
            start_offset_id=start_id,
            end_offset_id=end_id,
            download_filter=download_filter or None,
            task_type=TaskType.Forward,
            task_id=task_id,
            topic_id=topic_id or 0,
        )
        node.upload_user = self.client

        # Resolve discussion message so forwards land in comments
        if target_msg_id:
            try:
                node.reply_to_message = await self.client.get_discussion_message(
                    dst_chat_id, target_msg_id
                )
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Cannot get discussion message: {e}",
                }

        self._active_tasks[task_id] = node

        chat_download_config = ChatDownloadConfig()
        chat_download_config.last_read_message_id = start_id
        if download_filter:
            chat_download_config.download_filter = download_filter

        self._loop.create_task(
            self._download_chat_task(self.client, chat_download_config, node)
        )

        src_title = getattr(src_entity, "title", None) or str(src_entity.id)
        return {
            "ok": True,
            "task_id": task_id,
            "from": src_title,
            "range": f"{start_id} - {end_id or 'latest'}",
        }

    # ── add runtime download filter ──────────────────────────────

    def web_add_filter(self, filter_str: str) -> dict:
        """Add/replace the runtime download filter (equivalent to Bot /add_filter)."""
        from datetime import datetime

        from module.filter import Filter
        from utils.format import replace_date_time

        resolved = replace_date_time(filter_str)
        f = Filter()
        # Set dummy meta_data so check_filter can validate the expression
        dummy_meta = MetaData(datetime(2022, 8, 5, 14, 35, 12), 0, "", 0, 0, 0, "", 0)
        f.set_meta_data(dummy_meta)
        ok, err = f.check_filter(resolved)
        if not ok:
            return {"ok": False, "error": err or "Invalid filter expression"}

        # Store on the bot instance if running, otherwise just validate
        if self.bot_instance:
            self.bot_instance.download_filter = [filter_str]
        return {"ok": True, "filter": filter_str}

    # ── toggle cleanup after upload ──────────────────────────────

    def web_toggle_cleanup(self, enabled: bool) -> dict:
        """Toggle delete-after-forward/upload (equivalent to Bot /cleanup on|off)."""
        self.app.after_upload_telegram_delete = enabled
        self.app.config["after_upload_telegram_delete"] = enabled
        return {
            "ok": True,
            "cleanup_enabled": enabled,
        }

    def web_get_cleanup_status(self) -> dict:
        """Get current cleanup toggle state."""
        return {
            "ok": True,
            "cleanup_enabled": self.app.after_upload_telegram_delete,
        }

    # ── forward folder cleanup ───────────────────────────────────

    def web_forward_clean(self) -> dict:
        """Clean forward folders (equivalent to Bot /forward-clean)."""
        forward_root = self.app.forward_save_path
        forward_temp = self.app.forward_temp_path
        os.makedirs(forward_root, exist_ok=True)
        os.makedirs(forward_temp, exist_ok=True)
        freed = clear_dir_contents(forward_root)
        return {
            "ok": True,
            "freed_bytes": freed,
            "freed_display": format_byte(freed),
        }

    # ── forward folder size limit ────────────────────────────────

    def web_forward_limit(self, size_str: str) -> dict:
        """Set forward folder size limit (equivalent to Bot /forward-limit).

        size_str: e.g. "20GB", "500MB", "off"
        """
        value = size_str.strip().lower()
        if value in ("off", "0", "disable", "none"):
            self.app.forward_max_size = 0
            self.app.config["forward_max_size"] = 0
            return {"ok": True, "limit": 0, "limit_display": "off"}

        size_bytes = parse_size_to_bytes(size_str)
        if not size_bytes or size_bytes <= 0:
            return {"ok": False, "error": f"Invalid size: {size_str}"}

        self.app.forward_max_size = size_bytes
        self.app.config["forward_max_size"] = size_bytes

        # Immediate cleanup if over limit
        os.makedirs(self.app.forward_save_path, exist_ok=True)
        total_size = get_dir_size(self.app.forward_save_path)
        freed = 0
        if total_size >= size_bytes:
            freed = cleanup_dir_by_freeing(
                self.app.forward_save_path, int(size_bytes * 0.3)
            )

        return {
            "ok": True,
            "limit": size_bytes,
            "limit_display": format_byte(size_bytes),
            "current_size": format_byte(total_size),
            "freed": format_byte(freed),
        }

    def web_get_forward_limit(self) -> dict:
        """Get current forward folder limit and usage."""
        forward_root = self.app.forward_save_path
        os.makedirs(forward_root, exist_ok=True)
        total_size = get_dir_size(forward_root)
        limit = self.app.forward_max_size
        return {
            "ok": True,
            "limit": limit,
            "limit_display": format_byte(limit) if limit else "off",
            "current_size": total_size,
            "current_size_display": format_byte(total_size),
        }

    # ══════════════════════════════════════════════════════════════
    # Download progress API (account-scoped)
    # ══════════════════════════════════════════════════════════════

    def _get_account_task_ids(self) -> set:
        """Get set of task_ids belonging to this account."""
        return set(self._active_tasks.keys())

    def get_downloads_snapshot(self) -> dict:
        """Read global download_stat, filter by this account's task_ids.

        Returns {"active": [...], "completed": [...]} with per-file info.
        """
        task_ids = self._get_account_task_ids()
        if not task_ids:
            return {"active": [], "completed": []}

        download_result = get_download_result()
        active = []
        completed = []

        # Shallow-copy the outer dict keys to avoid mutation during iteration
        chat_ids = list(download_result.keys())
        for chat_id in chat_ids:
            messages = download_result.get(chat_id)
            if not messages:
                continue
            msg_ids = list(messages.keys())
            for msg_id in msg_ids:
                entry = messages.get(msg_id)
                if not entry:
                    continue
                if entry.get("task_id") not in task_ids:
                    continue

                total_bytes = entry.get("total_size", 0)
                down_bytes = entry.get("down_byte", 0)
                speed = entry.get("download_speed", 0)
                progress = (
                    round(down_bytes / total_bytes * 100, 1) if total_bytes > 0 else 0
                )
                eta = (
                    round((total_bytes - down_bytes) / speed)
                    if speed > 0 and down_bytes < total_bytes
                    else 0
                )

                item = {
                    "message_id": msg_id,
                    "chat_id": str(chat_id),
                    "file_name": os.path.basename(entry.get("file_name", "")),
                    "file_path": entry.get("file_name", ""),
                    "total_bytes": total_bytes,
                    "downloaded_bytes": down_bytes,
                    "speed_bps": int(speed),
                    "progress_pct": progress,
                    "eta_seconds": eta,
                    "task_id": entry.get("task_id", 0),
                }

                if down_bytes >= total_bytes and total_bytes > 0:
                    completed.append(item)
                else:
                    active.append(item)

        return {"active": active, "completed": completed}

    def web_get_downloads_status(self) -> dict:
        """Summary status for the download monitor header."""
        snapshot = self.get_downloads_snapshot()
        active_items = snapshot["active"]
        total_speed = sum(item["speed_bps"] for item in active_items)

        state = "idle"
        if active_items:
            if get_download_state() == DownloadState.StopDownload:
                state = "paused"
            else:
                state = "running"

        return {
            "ok": True,
            "state": state,
            "active_count": len(active_items),
            "completed_count": len(snapshot["completed"]),
            "total_speed_bps": total_speed,
            "total_speed_display": format_byte(total_speed) + "/s",
        }

    def web_get_downloads_list(self) -> dict:
        """Full download list for the download monitor page."""
        snapshot = self.get_downloads_snapshot()
        return {
            "ok": True,
            "active": snapshot["active"],
            "completed": snapshot["completed"],
        }

    def web_pause_downloads(self) -> dict:
        """Pause all downloads (global state)."""
        if get_download_state() == DownloadState.Downloading:
            set_download_state(DownloadState.StopDownload)
        return {"ok": True, "state": "paused"}

    def web_continue_downloads(self) -> dict:
        """Resume all downloads (global state)."""
        if get_download_state() == DownloadState.StopDownload:
            set_download_state(DownloadState.Downloading)
        return {"ok": True, "state": "running"}
