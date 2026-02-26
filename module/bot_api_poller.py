import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Tuple

import pyrogram
from loguru import logger


class _RateLimitError(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"Rate limited for {retry_after}s")
        self.retry_after = retry_after


def _bot_api_call_sync(base_url: str, method: str, payload: dict) -> dict:
    """Synchronous Bot API HTTP call."""
    url = f"{base_url}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code == 429:
            retry_after = 1
            try:
                body = err.read().decode("utf-8")
                d = json.loads(body)
                retry_after = int(d.get("parameters", {}).get("retry_after", 1))
            except Exception:
                pass
            raise _RateLimitError(max(retry_after, 1)) from err
        body = ""
        try:
            body = err.read().decode("utf-8")
        except Exception:
            pass
        logger.warning(f"[BotAPI] {method} HTTP {err.code}: {body}")
        raise


def _serialize_reply_markup(reply_markup) -> Optional[dict]:
    """Convert Pyrogram InlineKeyboardMarkup to Bot API dict."""
    if reply_markup is None:
        return None
    if isinstance(reply_markup, dict):
        return reply_markup
    # pyrogram.types.InlineKeyboardMarkup
    if hasattr(reply_markup, "inline_keyboard"):
        rows = []
        for row in reply_markup.inline_keyboard:
            buttons = []
            for btn in row:
                b = {"text": btn.text}
                if btn.callback_data:
                    b["callback_data"] = btn.callback_data
                if btn.url:
                    b["url"] = btn.url
                buttons.append(b)
            rows.append(buttons)
        return {"inline_keyboard": rows}
    return None


def _parse_mode_str(parse_mode) -> Optional[str]:
    """Convert pyrogram ParseMode enum to Bot API string."""
    if parse_mode is None:
        return None
    if isinstance(parse_mode, str):
        return parse_mode
    # pyrogram.enums.ParseMode
    name = getattr(parse_mode, "name", None) or str(parse_mode)
    name_lower = name.lower().replace("parsemode.", "")
    if "markdown" in name_lower:
        return "Markdown"
    if "html" in name_lower:
        return "HTML"
    return None


class BotApiUser:
    def __init__(self, user: Optional[Dict[str, Any]]):
        self._user = user or {}

    @property
    def id(self) -> int:
        return int(self._user.get("id", 0) or 0)

    @property
    def first_name(self) -> str:
        return self._user.get("first_name") or ""

    @property
    def last_name(self) -> str:
        return self._user.get("last_name") or ""

    @property
    def username(self) -> str:
        return self._user.get("username") or ""


class BotApiMessage:
    def __init__(self, message: Optional[Dict[str, Any]]):
        self._message = message or {}

    @staticmethod
    def _to_object(value: Any) -> Any:
        if isinstance(value, dict):
            return SimpleNamespace(
                **{k: BotApiMessage._to_object(v) for k, v in value.items()}
            )
        if isinstance(value, list):
            return [BotApiMessage._to_object(v) for v in value]
        return value

    @property
    def id(self) -> int:
        return int(self._message.get("message_id", 0) or 0)

    @property
    def message_id(self) -> int:
        return self.id

    @property
    def text(self) -> str:
        return self._message.get("text") or self._message.get("caption") or ""

    @property
    def from_user(self) -> BotApiUser:
        return BotApiUser(self._message.get("from"))

    @property
    def chat(self):
        chat = self._message.get("chat") or {}
        return SimpleNamespace(
            id=int(chat.get("id", 0) or 0), type=chat.get("type") or ""
        )

    @property
    def media(self):
        if "photo" in self._message:
            return pyrogram.enums.MessageMediaType.PHOTO
        if "video" in self._message:
            return pyrogram.enums.MessageMediaType.VIDEO
        if "document" in self._message:
            return pyrogram.enums.MessageMediaType.DOCUMENT
        if "audio" in self._message:
            return pyrogram.enums.MessageMediaType.AUDIO
        return None

    @property
    def photo(self):
        return (
            self._to_object(self._message.get("photo"))
            if "photo" in self._message
            else None
        )

    @property
    def video(self):
        return (
            self._to_object(self._message.get("video"))
            if "video" in self._message
            else None
        )

    @property
    def document(self):
        return (
            self._to_object(self._message.get("document"))
            if "document" in self._message
            else None
        )

    @property
    def audio(self):
        return (
            self._to_object(self._message.get("audio"))
            if "audio" in self._message
            else None
        )

    @property
    def reply_to_message(self):
        reply = self._message.get("reply_to_message")
        return BotApiMessage(reply) if reply else None

    @property
    def media_group_id(self):
        return self._message.get("media_group_id")

    @property
    def caption(self):
        return self._message.get("caption")

    @property
    def empty(self) -> bool:
        return False


class BotApiCallbackQuery:
    def __init__(self, callback_query: Optional[Dict[str, Any]]):
        self._query = callback_query or {}

    @property
    def id(self) -> str:
        return self._query.get("id") or ""

    @property
    def from_user(self) -> BotApiUser:
        return BotApiUser(self._query.get("from"))

    @property
    def data(self) -> str:
        return self._query.get("data") or ""

    @property
    def message(self) -> Optional[BotApiMessage]:
        message = self._query.get("message")
        return BotApiMessage(message) if message else None


class BotApiFacadeClient:
    """Drop-in replacement for pyrogram.Client (bot) using Bot API HTTP.

    Provides send_message, edit_message_text, get_me, set_bot_commands,
    answer_callback_query — the only methods handlers actually call on
    the bot client parameter.
    """

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self._base_url = f"https://api.openai.com/v1"
        self._me = None
        # Provide a .loop attribute for compatibility with code that reads it
        self.loop = None

    async def _call(self, method: str, payload: dict) -> dict:
        return await asyncio.to_thread(
            _bot_api_call_sync, self._base_url, method, payload
        )

    async def get_me(self):
        """Get bot info via Bot API, return SimpleNamespace mimicking pyrogram User."""
        if self._me:
            return self._me
        resp = await self._call("getMe", {})
        if resp.get("ok"):
            u = resp["result"]
            self._me = SimpleNamespace(
                id=u.get("id"),
                first_name=u.get("first_name", ""),
                last_name=u.get("last_name", ""),
                username=u.get("username", ""),
                is_bot=u.get("is_bot", True),
            )
            return self._me
        raise RuntimeError(f"getMe failed: {resp}")

    async def send_message(
        self,
        chat_id,
        text,
        *,
        reply_to_message_id=None,
        parse_mode=None,
        reply_markup=None,
        disable_web_page_preview=None,
        **kwargs,
    ) -> BotApiMessage:
        """Send message via Bot API HTTP. Returns BotApiMessage."""
        logger.info(
            f"[BotApiFacade] send_message chat_id={chat_id}, text_len={len(str(text))}"
        )
        payload: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": str(text),
        }
        pm = _parse_mode_str(parse_mode)
        if pm:
            payload["parse_mode"] = pm
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        rm = _serialize_reply_markup(reply_markup)
        if rm:
            payload["reply_markup"] = rm
        if disable_web_page_preview:
            payload["disable_web_page_preview"] = True

        resp = await self._call("sendMessage", payload)
        if resp.get("ok"):
            return BotApiMessage(resp.get("result"))
        logger.warning(f"[BotApiFacade] sendMessage failed: {resp}")
        # Return a stub so callers don't crash
        return BotApiMessage({"message_id": 0})

    async def edit_message_text(
        self,
        chat_id,
        message_id,
        text,
        *,
        parse_mode=None,
        reply_markup=None,
        disable_web_page_preview=None,
        **kwargs,
    ) -> BotApiMessage:
        """Edit message text via Bot API HTTP."""
        logger.info(
            f"[BotApiFacade] edit_message_text chat_id={chat_id}, msg_id={message_id}"
        )
        payload: Dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": str(text),
        }
        pm = _parse_mode_str(parse_mode)
        if pm:
            payload["parse_mode"] = pm
        rm = _serialize_reply_markup(reply_markup)
        if rm:
            payload["reply_markup"] = rm
        if disable_web_page_preview:
            payload["disable_web_page_preview"] = True

        resp = await self._call("editMessageText", payload)
        if resp.get("ok"):
            return BotApiMessage(resp.get("result"))
        desc = resp.get("description", "")
        # "message is not modified" is not a real error
        if "not modified" in desc.lower():
            return BotApiMessage({"message_id": int(message_id)})
        logger.warning(f"[BotApiFacade] editMessageText failed: {resp}")
        return BotApiMessage({"message_id": int(message_id)})

    async def set_bot_commands(self, commands) -> bool:
        """Set bot commands via Bot API HTTP."""
        cmd_list = []
        for cmd in commands:
            if hasattr(cmd, "command"):
                cmd_list.append(
                    {"command": cmd.command, "description": cmd.description}
                )
            elif isinstance(cmd, dict):
                cmd_list.append(cmd)
        resp = await self._call("setMyCommands", {"commands": cmd_list})
        return bool(resp.get("ok"))

    async def answer_callback_query(
        self, callback_query_id, text=None, show_alert=False, **kwargs
    ) -> bool:
        """Answer callback query via Bot API HTTP."""
        payload: Dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = str(text)
        if show_alert:
            payload["show_alert"] = True
        resp = await self._call("answerCallbackQuery", payload)
        return bool(resp.get("ok"))

    async def start(self):
        """No-op. Facade doesn't need MTProto connection."""
        pass

    async def stop(self):
        """No-op. Facade doesn't need MTProto connection."""
        pass

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        """Stub for send_document — not used by current handlers but prevents AttributeError."""
        logger.warning(
            "[BotApiFacade] send_document called but not implemented via HTTP multipart"
        )
        return BotApiMessage({"message_id": 0})


class BotApiPoller:
    def __init__(
        self,
        bot_token: str,
        allowed_user_ids: list,
        loop: asyncio.AbstractEventLoop,
    ):
        self.bot_token = bot_token or ""
        self.allowed_user_ids = set(allowed_user_ids or [])
        self.loop = loop
        self.bot_client = None

        self._running = True
        self._offset = 0
        self._base_url = f"https://api.openai.com/v1"

        self._command_handlers: Dict[str, Callable] = {}
        self._regex_handlers: List[Tuple[Pattern[str], Callable]] = []
        self._media_handler: Optional[Callable] = None
        self._callback_handler: Optional[Callable] = None

    def stop(self):
        self._running = False

    def set_handlers(
        self,
        command_handlers: dict,
        regex_handlers: list,
        media_handler,
        callback_handler,
    ):
        self._command_handlers = {
            str(name).lower(): handler
            for name, handler in (command_handlers or {}).items()
        }
        self._regex_handlers = [
            (re.compile(pattern), handler)
            for pattern, handler in (regex_handlers or [])
        ]
        self._media_handler = media_handler
        self._callback_handler = callback_handler

    def _is_allowed(self, user_id: int) -> bool:
        if user_id in self.allowed_user_ids:
            return True
        if str(user_id) in self.allowed_user_ids:
            return True
        return False

    def _build_updates_url(self) -> str:
        query = urllib.parse.urlencode(
            {
                "timeout": 30,
                "offset": self._offset,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
        )
        return f"{self._base_url}/getUpdates?{query}"

    def _fetch_updates_sync(self) -> Dict[str, Any]:
        url = self._build_updates_url()
        req = urllib.request.Request(url=url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload)
        except urllib.error.HTTPError as err:
            if err.code == 429:
                retry_after = 1
                try:
                    body = err.read().decode("utf-8")
                    data = json.loads(body)
                    retry_after = int(
                        data.get("parameters", {}).get("retry_after", retry_after)
                    )
                except Exception:
                    pass
                raise _RateLimitError(max(retry_after, 1)) from err
            raise

    async def _dispatch(self, update: Dict[str, Any]):
        try:
            if "message" in update:
                message_raw = update.get("message") or {}
                user_id = int((message_raw.get("from") or {}).get("id", 0) or 0)
                text = (message_raw.get("text") or message_raw.get("caption") or "")[
                    :80
                ]
                logger.info(
                    f"[BotApiPoller] message from user_id={user_id}, text={text!r}"
                )
                logger.info(f"[BotApiPoller] allowed_user_ids={self.allowed_user_ids}")
                if not self._is_allowed(user_id):
                    logger.warning(
                        f"[BotApiPoller] user {user_id} NOT allowed, skipping"
                    )
                    return

                message_obj = BotApiMessage(message_raw)
                text = message_obj.text or ""

                dispatched_command = False
                if text.startswith("/"):
                    first_token = text.split(maxsplit=1)[0]
                    command = first_token[1:].split("@", maxsplit=1)[0].lower()
                    handler = self._command_handlers.get(command)
                    if handler:
                        await handler(self.bot_client, message_obj)
                        dispatched_command = True

                if not dispatched_command:
                    for pattern, handler in self._regex_handlers:
                        if text and pattern.search(text):
                            await handler(self.bot_client, message_obj)
                            dispatched_command = True
                            break

                if not dispatched_command and message_obj.media and self._media_handler:
                    await self._media_handler(self.bot_client, message_obj)

            elif "callback_query" in update and self._callback_handler:
                query_raw = update.get("callback_query") or {}
                user_id = int((query_raw.get("from") or {}).get("id", 0) or 0)
                if not self._is_allowed(user_id):
                    return

                query_obj = BotApiCallbackQuery(query_raw)
                await self._callback_handler(self.bot_client, query_obj)
        except Exception as err:
            logger.exception(f"[BotApiPoller] dispatch failed: {err}")

    async def run(self):
        if not self.bot_token:
            logger.warning("[BotApiPoller] bot_token empty, polling disabled")
            return

        if not self.bot_client:
            logger.warning("[BotApiPoller] bot_client missing, polling disabled")
            return

        backoff = 1
        logger.info("[BotApiPoller] started")

        while self._running:
            try:
                data = await asyncio.to_thread(self._fetch_updates_sync)
                n_results = len(data.get("result", [])) if data else 0
                if n_results > 0:
                    logger.info("[BotApiPoller] received {} update(s)", n_results)
                if data and not data.get("ok", False):
                    error_code = int(data.get("error_code", 0) or 0)
                    if error_code == 429:
                        retry_after = int(
                            data.get("parameters", {}).get("retry_after", 1) or 1
                        )
                        await asyncio.sleep(max(retry_after, 1))
                        continue
                    raise RuntimeError(data.get("description") or "getUpdates failed")

                updates: list = data.get("result", []) if data else []

                for update in updates:
                    update_id = int(update.get("update_id", 0) or 0)
                    if update_id >= self._offset:
                        self._offset = update_id + 1
                    await self._dispatch(update)

                backoff = 1
            except _RateLimitError as err:
                delay = max(err.retry_after, 1)
                logger.warning(f"[BotApiPoller] rate limited, sleeping {delay}s")
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.warning(f"[BotApiPoller] poll error: {err}, retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

        logger.info("[BotApiPoller] stopped")
