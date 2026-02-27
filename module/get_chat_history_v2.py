"""Rewrite pyrogram.get_chat_history with timeout + retry + resume.

Based on the original get_chat_history_v2 implementation with added resilience:
- Each GetHistory chunk is wrapped in asyncio.wait_for for timeout protection
- Retry logic with exponential backoff on transient failures
- Cached peer resolution to avoid redundant resolve_peer calls
- Fallback to standard get_chat_history when reverse-mode GetHistory returns empty
"""

import asyncio
from datetime import datetime
from typing import AsyncGenerator, Optional, Union

import pyrogram
from loguru import logger

# pylint: disable = W0611
from pyrogram import raw, types, utils

# ── constants ────────────────────────────────────────────────────
GET_HISTORY_TIMEOUT = 30          # seconds per GetHistory call
GET_HISTORY_MAX_RETRIES = 5       # retries per chunk before giving up
GET_HISTORY_RETRY_BACKOFF = 3     # base seconds between retries


async def get_chunk_v2(
    *,
    client: pyrogram.Client,
    chat_id: Union[int, str],
    peer=None,
    limit: int = 0,
    offset: int = 0,
    max_id: int = 0,
    from_message_id: int = 0,
    from_date: datetime = utils.zero_datetime(),
    reverse: bool = False
):
    """Get a single chunk of chat history with timeout protection."""
    from_message_id = from_message_id or (1 if reverse else 0)

    if peer is None:
        peer = await asyncio.wait_for(
            client.resolve_peer(chat_id),
            timeout=GET_HISTORY_TIMEOUT,
        )

    result = await asyncio.wait_for(
        client.invoke(
            raw.functions.messages.GetHistory(
                peer=peer,
                offset_id=from_message_id,
                offset_date=utils.datetime_to_timestamp(from_date),
                add_offset=offset * (-1 if reverse else 1) - (limit if reverse else 0),
                limit=limit,
                max_id=max_id,
                min_id=0,
                hash=0,
            ),
            sleep_threshold=60,
        ),
        timeout=GET_HISTORY_TIMEOUT,
    )

    messages = await utils.parse_messages(client, result, replies=0)

    if reverse:
        messages.reverse()

    return messages, peer


# pylint: disable = C0301
async def get_chat_history_v2(
    self: pyrogram.Client,
    chat_id: Union[int, str],
    limit: int = 0,
    max_id: int = 0,
    offset: int = 0,
    offset_id: int = 0,
    offset_date: datetime = utils.zero_datetime(),
    reverse: bool = False,
) -> Optional[AsyncGenerator["types.Message", None]]:
    """Get messages from a chat history.

    Resilient version with timeout, retry, and fallback logic.
    When reverse-mode GetHistory returns empty (known MTProto quirk),
    falls back to standard get_chat_history iteration.
    """
    current = 0
    total = limit or (1 << 31) - 1
    limit = min(100, total)

    cached_peer = None  # resolve once, reuse across chunks

    while True:
        # ── fetch one chunk with retry ───────────────────────────
        messages = None
        logger.info('get_chat_history_v2: fetching chunk offset_id={} limit={} max_id={} reverse={}',
                     offset_id, limit, max_id, reverse)

        for attempt in range(1, GET_HISTORY_MAX_RETRIES + 1):
            try:
                chunk_messages, cached_peer = await get_chunk_v2(
                    client=self,
                    chat_id=chat_id,
                    peer=cached_peer,
                    limit=limit,
                    offset=offset,
                    max_id=max_id + 1 if max_id else 0,
                    from_message_id=offset_id,
                    from_date=offset_date,
                    reverse=reverse,
                )
                messages = chunk_messages
                break
            except asyncio.TimeoutError:
                logger.warning(
                    "GetHistory timeout (attempt {}/{}) offset_id={}",
                    attempt, GET_HISTORY_MAX_RETRIES, offset_id,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, ConnectionError) as e:
                logger.warning(
                    "GetHistory connection error (attempt {}/{}): {}",
                    attempt, GET_HISTORY_MAX_RETRIES, e,
                )
                cached_peer = None
            except Exception as e:
                err_name = type(e).__name__
                if "FloodWait" in err_name or "FloodPremiumWait" in err_name:
                    raise
                logger.warning(
                    "GetHistory unexpected error (attempt {}/{}): {} {}",
                    attempt, GET_HISTORY_MAX_RETRIES, err_name, e,
                )

            if attempt < GET_HISTORY_MAX_RETRIES:
                wait = GET_HISTORY_RETRY_BACKOFF * attempt
                logger.info("Retrying GetHistory in {}s (offset_id={})", wait, offset_id)
                await asyncio.sleep(wait)

        if messages is None:
            logger.error(
                "GetHistory failed after {} retries, offset_id={}. Stopping iteration.",
                GET_HISTORY_MAX_RETRIES, offset_id,
            )
            return

        # ── fallback when reverse-mode returns empty ─────────────
        # MTProto's GetHistory with reverse add_offset can return empty
        # even when more messages exist. Fall back to standard iteration.
        if not messages:
            logger.info(
                "get_chat_history_v2: chunk empty at offset_id={}, trying fallback via get_chat_history",
                offset_id,
            )
            break_count = offset_id - 1
            fallback_messages = []
            try:
                async for message in self.get_chat_history(chat_id, max_id=max_id + 1 if max_id else 0):
                    if break_count > 0:
                        break_count -= 1
                        continue
                    if len(fallback_messages) >= limit + 1:
                        break
                    fallback_messages.append(message)
            except Exception as fb_err:
                logger.warning("Fallback get_chat_history failed: {}", fb_err)

            if not fallback_messages:
                logger.info("get_chat_history_v2: fallback also empty, iteration done")
                return
            messages = fallback_messages
            logger.info("get_chat_history_v2: fallback returned {} messages", len(messages))

        offset_id = messages[-1].id + (1 if reverse else 0)
        logger.info("get_chat_history_v2: chunk has {} messages, next offset_id={}",
                     len(messages), offset_id)

        for message in messages:
            yield message

            current += 1

            if current >= total:
                return
