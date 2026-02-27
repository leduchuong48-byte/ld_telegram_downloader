"""Downloads media from telegram."""

import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import (
    Application,
    ChatDownloadConfig,
    DownloadStatus,
    TaskNode,
    TaskType,
)
from module.bot import start_download_bot, stop_download_bot
from module.download_stat import update_download_status
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_download_status,
    report_bot_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.web import init_web
from utils.file_management import cleanup_dir_by_freeing, get_dir_size
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "ld_telegram_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

queue: asyncio.Queue = asyncio.Queue()
upload_queue: asyncio.Queue = asyncio.Queue()
RETRY_TIME_OUT = 3
FLOOD_PREMIUM_WAIT = getattr(
    pyrogram.errors.exceptions.flood_420, "FloodPremiumWait", None
)
if FLOOD_PREMIUM_WAIT:
    FLOOD_WAIT_EXCEPTIONS = (
        pyrogram.errors.exceptions.flood_420.FloodWait,
        FLOOD_PREMIUM_WAIT,
    )
else:
    FLOOD_WAIT_EXCEPTIONS = (pyrogram.errors.exceptions.flood_420.FloodWait,)

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish

    Parameters
    ----------
    media_size: int
        The size of the downloaded resource
    download_path: str
        Resource download hold path
    ui_file_name: str
        Really show file name

    """
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        raise pyrogram.errors.exceptions.bad_request_400.BadRequest()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path

    Parameters
    ----------
    temp_download_path: str
        Temporary download path

    download_path: str
        Download path

    """

    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _safe_remove_file(file_path: Optional[str]):
    """Remove file if exists."""
    if not file_path:
        return
    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass


def _check_timeout(retry: int, _: int):
    """Check if message download timeout, then add message id into failed_ids

    Parameters
    ----------
    retry: int
        Retry download message times

    message_id: int
        Try to download message 's id

    """
    if retry == 2:
        return True
    return False


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """
    Check if the given file format can be downloaded.

    Parameters
    ----------
    _type: str
        Type of media object.
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types
    file_format: str
        Format of the current file to be downloaded.

    Returns
    -------
    bool
        True if the file format can be downloaded else False.
    """
    if _type in ["audio", "document", "video"]:
        allowed_formats: list = file_formats[_type]
        if not file_format in allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """
    Check if a file exists and it is not a directory.

    Parameters
    ----------
    file_path: str
        Absolute path of the file to be checked.

    Returns
    -------
    bool
        True if the file exists else False.
    """
    return not os.path.isdir(file_path) and os.path.exists(file_path)


# pylint: disable = R0912


def _get_task_temp_path(node: TaskNode) -> str:
    """Use download temp path for forward tasks."""
    if node.task_type in (TaskType.Forward, TaskType.ListenForward):
        return app.forward_temp_path
    return app.temp_save_path


def _get_task_save_base_path(node: TaskNode) -> str:
    """Get base download path for task."""
    if node.task_type in (TaskType.Forward, TaskType.ListenForward):
        return app.forward_save_path
    return app.save_path


def _ensure_forward_dirs():
    os.makedirs(app.forward_temp_path, exist_ok=True)
    os.makedirs(app.forward_save_path, exist_ok=True)


def _maybe_cleanup_forward_dir():
    if app.forward_max_size <= 0:
        return
    total_size = get_dir_size(app.forward_save_path)
    if total_size < app.forward_max_size:
        return
    target_free = int(app.forward_max_size * 0.3)
    if target_free <= 0:
        return
    freed = cleanup_dir_by_freeing(app.forward_save_path, target_free)
    if freed > 0:
        logger.info(
            "forward dir cleanup: freed %s bytes (target %s bytes)",
            freed,
            target_free,
        )


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
    temp_base_dir: Optional[str] = None,
    save_base_dir: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object.

    Parameters
    ----------
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice]
        Media object to be extracted.
    _type: str
        Type of media object.

    Returns
    -------
    Tuple[str, str, Optional[str]]
        file_name, file_format
    """
    if _type in ["audio", "document", "video"]:
        # pylint: disable = C0301
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]  # type: ignore
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    temp_base_dir = temp_base_dir or app.temp_save_path
    if _type in ["voice", "video_note"]:
        # pylint: disable = C0209
        file_format = media_obj.mime_type.split("/")[-1]  # type: ignore
        file_save_path = app.get_file_save_path(
            _type, dirname, datetime_dir_name, base_dir=save_base_dir
        )
        file_name = "{} - {}_{}.{}".format(
            message.id,
            _type,
            media_obj.date.isoformat(),  # type: ignore
            file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(temp_base_dir, dirname, file_name)

        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            # file_name = file_name.split(".")[0]
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )

        file_save_path = app.get_file_save_path(
            _type, dirname, datetime_dir_name, base_dir=save_base_dir
        )

        temp_file_name = os.path.join(temp_base_dir, dirname, gen_file_name)

        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(
    message: pyrogram.types.Message,
    node: TaskNode,
):
    """Add Download task"""
    if message.empty:
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    if queue.full():
        logger.debug(
            "Download queue is full (size=%s), waiting to enqueue message %s",
            queue.qsize(),
            message.id,
        )
    await queue.put((message, node))
    node.total_task += 1
    return True


async def add_upload_task(
    message: pyrogram.types.Message,
    node: TaskNode,
    download_status: DownloadStatus,
    file_name: Optional[str],
):
    """Add Upload task"""
    if upload_queue.full():
        logger.debug(
            "Upload queue is full (size=%s), waiting to enqueue message %s",
            upload_queue.qsize(),
            message.id,
        )
    await upload_queue.put((message, node, download_status, file_name))


async def save_msg_to_file(
    app, chat_id: Union[int, str], message: pyrogram.types.Message
):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"

    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path,
        file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )

    os.makedirs(os.path.dirname(file_name), exist_ok=True)

    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None

    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")

    return DownloadStatus.SuccessDownload, file_name


async def download_task(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    node: TaskNode,
    app_override: "Application" = None,
):
    """Download and Forward media"""

    _app = app_override or app

    download_status, file_name = await download_media(
        client, message, _app.media_types, _app.file_formats, node
    )

    if _app.enable_download_txt and message.text and not message.media:
        download_status, file_name = await save_msg_to_file(_app, node.chat_id, message)

    if not node.bot:
        _app.set_download_id(node, message.id, download_status)

    node.download_status[message.id] = download_status

    file_size = os.path.getsize(file_name) if file_name else 0

    await report_bot_download_status(
        node.bot,
        node,
        download_status,
        file_size,
    )
    if download_status is DownloadStatus.SkipDownload and node.bot:
        await report_bot_status(node.bot, node, immediate_reply=True)

    if node.upload_telegram_chat_id or _app.cloud_drive_config.enable_upload_file:
        await add_upload_task(message, node, download_status, file_name)


async def upload_task(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    node: TaskNode,
    download_status: DownloadStatus,
    file_name: Optional[str],
):
    """Upload downloaded media to Telegram or cloud drive."""
    await upload_telegram_chat(
        client,
        node.upload_user if node.upload_user else client,
        app,
        node,
        message,
        download_status,
        file_name,
    )

    # rclone upload
    if (
        not node.upload_telegram_chat_id
        and download_status is DownloadStatus.SuccessDownload
        and file_name
    ):
        ui_file_name = file_name
        if app.hide_file_name:
            ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
        if await app.upload_file(
            file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)
        ):
            node.upload_success_count += 1


# pylint: disable = R0915,R0914


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """
    Download media from Telegram.

    Each of the files to download are retried 3 times with a
    delay of 5 seconds each.

    Parameters
    ----------
    client: pyrogram.client.Client
        Client to interact with Telegram APIs.
    message: pyrogram.types.Message
        Message object retrieved from telegram.
    media_types: list
        List of strings of media types to be downloaded.
        Ex : `["audio", "photo"]`
        Supported formats:
            * audio
            * document
            * photo
            * video
            * voice
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types.

    Returns
    -------
    int
        Current message id.
    """

    # pylint: disable = R0912

    file_name: str = ""
    temp_file_name: Optional[str] = None
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    message = await fetch_message(client, message)
    if node.task_type in (TaskType.Forward, TaskType.ListenForward):
        _ensure_forward_dirs()
        _maybe_cleanup_forward_dir()
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id,
                message,
                _media,
                _type,
                temp_base_dir=_get_task_temp_path(node),
                save_base_dir=_get_task_save_base_path(node),
            )
            media_size = getattr(_media, "file_size", 0)

            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(_type, file_formats, file_format):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    if file_size or file_size == media_size:
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}.\n"
                        )

                        return DownloadStatus.SkipDownload, None
            else:
                return DownloadStatus.SkipDownload, None

            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.FailedDownload, None
    if _media is None:
        return DownloadStatus.SkipDownload, None

    message_id = message.id

    last_temp_download_path: Optional[str] = None
    for retry in range(3):
        try:
            temp_download_path = await asyncio.wait_for(
                client.download_media(
                    message,
                    file_name=temp_file_name,
                    progress=update_download_status,
                    progress_args=(
                        message_id,
                        ui_file_name,
                        task_start_time,
                        node,
                        client,
                    ),
                ),
                timeout=app.download_timeout,
            )

            if temp_download_path and isinstance(temp_download_path, str):
                last_temp_download_path = temp_download_path
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                _move_to_download_path(temp_download_path, file_name)
                if node.task_type in (TaskType.Forward, TaskType.ListenForward):
                    _maybe_cleanup_forward_dir()
                # TODO: if not exist file size or media
                return DownloadStatus.SuccessDownload, file_name
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            message = await fetch_message(client, message)
            if _check_timeout(retry, message.id):
                # pylint: disable = C0301
                logger.error(
                    f"Message[{message.id}]: "
                    f"{_t('file reference expired for 3 retries, download skipped.')}"
                )
        except asyncio.TimeoutError:
            client.stop_transmission()
            logger.error(
                f"Message[{message.id}]: {_t('download timeout')}, "
                f"{_t('skipping after')} {app.download_timeout}s"
            )
        except FLOOD_WAIT_EXCEPTIONS as wait_err:
            wait_seconds = wait_err.value + app.flood_wait_extra
            await asyncio.sleep(wait_seconds)
            logger.warning("Message[{}]: FlowWait {}", message.id, wait_err.value)
            _check_timeout(retry, message.id)
        except TypeError:
            # pylint: disable = C0301
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                )
        except Exception as e:
            # pylint: disable = C0301
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            break

    if node.task_type in (TaskType.Forward, TaskType.ListenForward):
        _safe_remove_file(temp_file_name)
        _safe_remove_file(last_temp_download_path)

    return DownloadStatus.FailedDownload, None


def _load_config():
    """Load config"""
    app.load_config()


def _init_queue():
    """Init global queue by config"""
    global queue  # pylint: disable=W0603
    maxsize = app.queue_max_size or app.max_download_task * 20
    queue = asyncio.Queue(maxsize=maxsize)


def _init_upload_queue():
    """Init upload queue by config"""
    global upload_queue  # pylint: disable=W0603
    maxsize = app.upload_queue_max_size or app.max_upload_task * 20
    upload_queue = asyncio.Queue(maxsize=maxsize)


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()
        logger.add(
            os.path.join(app.log_file_path, "ld_telegram_downloader.log"),
            rotation="10 MB",
            retention="10 days",
            level=app.log_level,
        )
        _init_queue()
        _init_upload_queue()
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False

    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]

            if node.is_stop_transmission:
                continue

            if node.client:
                await download_task(node.client, message, node)
            else:
                await download_task(client, message, node)
        except Exception as e:
            logger.exception(f"{e}")


async def upload_worker(client: pyrogram.client.Client):
    """Work for upload task"""
    while app.is_running:
        try:
            item = await upload_queue.get()
            message, node, download_status, file_name = item

            if node.is_stop_transmission:
                continue

            if node.client:
                await upload_task(
                    node.client, message, node, download_status, file_name
                )
            else:
                await upload_task(client, message, node, download_status, file_name)
        except Exception as e:
            logger.exception(f"{e}")


async def download_chat_task(
    client: pyrogram.Client,
    chat_download_config: ChatDownloadConfig,
    node: TaskNode,
    use_queue: bool = True,
):
    """Download all task"""
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
        logger.info(f"{_t('Downloading files failed during last run')}...")
        skipped_messages: list = await client.get_messages(  # type: ignore
            chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
        )

        for message in skipped_messages:
            if use_queue:
                await add_download_task(message, node)
            else:
                node.total_task += 1
                await download_task(client, message, node)

    async for message in messages_iter:  # type: ignore
        meta_data = MetaData()

        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                node.chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)

        if app.need_skip_message(chat_download_config, message.id):
            continue

        if app.exec_filter(chat_download_config, meta_data):
            if use_queue:
                await add_download_task(message, node)
            else:
                node.total_task += 1
                await download_task(client, message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id and (
                node.upload_telegram_chat_id
                or app.cloud_drive_config.enable_upload_file
            ):
                await add_upload_task(message, node, DownloadStatus.SkipDownload, None)

    chat_download_config.need_check = True
    chat_download_config.total_task = node.total_task
    node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
            value.need_check = True


async def run_until_all_task_finish():
    """Normal download"""
    while True:
        finish: bool = True
        for _, value in app.chat_download_config.items():
            if not value.need_check or value.total_task != value.finish_task:
                finish = False

        if (not app.bot_token and finish) or app.restart_program:
            break

        await asyncio.sleep(1)


def _exec_loop():
    """Exec loop"""

    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """
    Start the server using the provided client.
    """
    await client.start()


async def stop_server(client: pyrogram.Client):
    """
    Stop the server using the provided client.
    """
    await client.stop()


def main():
    """Main function of the downloader."""
    # Ensure app has an event loop (no longer created in Application.__init__)
    if app.loop is None:
        app.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(app.loop)

    tasks = []
    client = HookClient(
        "ld_telegram_downloader",
        api_id=app.api_id,
        api_hash=app.api_hash,
        proxy=app.proxy,
        workdir=app.session_file_path,
        start_timeout=app.start_timeout,
    )
    try:
        app.pre_run()
        init_web(app)

        set_max_concurrent_transmissions(
            client,
            app.max_download_concurrent_transmissions,
            app.max_upload_concurrent_transmissions,
        )

        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))

        app.loop.create_task(download_all_chat(client))
        for _ in range(app.max_download_task):
            task = app.loop.create_task(worker(client))
            tasks.append(task)
        for _ in range(app.max_upload_task):
            task = app.loop.create_task(upload_worker(client))
            tasks.append(task)

        if app.bot_token:
            app.loop.run_until_complete(
                start_download_bot(app, client, add_download_task, download_chat_task)
            )
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        if app.bot_token:
            app.loop.run_until_complete(stop_download_bot())
        app.loop.run_until_complete(stop_server(client))
        for task in tasks:
            task.cancel()
        logger.info(_t("Stopped!"))
        # check_for_updates(app.proxy)
        logger.info(f"{_t('update config')}......")
        app.update_config()
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} "
            f"{app.cloud_drive_config.total_upload_success_file_count}"
        )


def _load_global_config() -> dict:
    """Load global config.yaml for web settings."""
    try:
        from ruamel import yaml as _ryaml

        _y = _ryaml.YAML()
        with open(CONFIG_NAME, encoding="utf-8") as f:
            return _y.load(f.read()) or {}
    except Exception:
        return {}


def main_multi():
    """Multi-account entry point.

    Replaces the legacy single-account main() with:
    1. AccountManager — loads/migrates accounts
    2. WebAuthManager — handles runtime Telegram auth via WebUI
    3. AccountInstance per account — independent client + bot + workers
    4. Flask WebUI — dashboard + auth wizard
    """
    from module.account_manager import AccountManager, AccountStatus
    from module.account_instance import AccountInstance
    from module.web_auth import WebAuthManager
    from module.web import init_web_multi

    global_config = _load_global_config()

    # Initialize the global app so download_task/download_media have
    # valid media_types, file_formats, save_path etc.
    _load_config()

    # CRITICAL: When this file runs as __main__, the module-level `app` belongs
    # to __main__.  But account_instance.py does `from media_downloader import
    # download_task`, which causes Python to import this file AGAIN as the
    # `media_downloader` module — creating a SECOND `app` object.  We must
    # configure that second copy too, otherwise download_task sees empty
    # media_types/file_formats.
    import importlib
    import sys

    _md = sys.modules.get("media_downloader")
    if _md is None:
        _md = importlib.import_module("media_downloader")
    if _md is not None and hasattr(_md, "app") and _md.app is not app:
        _md.app.load_config()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Assign the single event loop to the module-level app (and its _md copy)
    app.loop = loop
    if _md is not None and hasattr(_md, 'app') and _md.app is not app:
        _md.app.loop = loop

    # ── account manager ──────────────────────────────────────────
    manager = AccountManager(base_dir=".")
    manager.load()

    # migrate legacy config if no accounts exist yet
    migrated_id = manager.migrate_legacy_config()
    if migrated_id:
        logger.info("Migrated legacy config to account '{}'", migrated_id)

    # ── web auth manager ─────────────────────────────────────────
    web_auth = WebAuthManager(manager, loop)

    # ── running account instances ────────────────────────────────
    instances: dict = {}  # account_id -> AccountInstance

    def get_instance(account_id: str):
        """Get a running AccountInstance by ID (used by web routes)."""
        return instances.get(account_id)

    async def start_account(account_id: str) -> bool:
        """Start a single account instance (called from WebUI)."""
        if account_id in instances:
            logger.warning("Account {} already running", account_id)
            return True

        acc = manager.get_account(account_id)
        if not acc or acc.status != AccountStatus.Authenticated.value:
            logger.warning(
                "Cannot start account {} (status={})",
                account_id,
                acc.status if acc else "not found",
            )
            return False

        instance = AccountInstance(acc, manager)
        result = await instance.start(loop)
        if result:
            instances[account_id] = instance
        return result

    async def stop_account(account_id: str):
        """Stop a single account instance (called from WebUI)."""
        instance = instances.pop(account_id, None)
        if instance:
            await instance.stop()

    # ── start web server ─────────────────────────────────────────
    web_host = global_config.get("web_host", "0.0.0.0")
    web_port = int(global_config.get("web_port", 5000))
    web_login_secret = str(global_config.get("web_login_secret", ""))

    init_web_multi(
        account_manager=manager,
        web_auth_manager=web_auth,
        loop=loop,
        web_host=web_host,
        web_port=web_port,
        web_login_secret=web_login_secret,
        start_account_cb=start_account,
        stop_account_cb=stop_account,
        get_instance_cb=get_instance,
    )

    logger.success("Web UI started at http://{}:{}", web_host, web_port)

    # ── auto-start authenticated accounts ────────────────────────
    async def auto_start():
        for acc in manager.get_authenticated_accounts():
            if manager.has_session_file(acc.account_id):
                try:
                    await start_account(acc.account_id)
                except Exception as e:
                    logger.error(
                        "Failed to auto-start account {}: {}",
                        acc.account_id,
                        e,
                    )

    loop.run_until_complete(auto_start())

    if instances:
        logger.success("Started {} account(s)", len(instances))
    else:
        logger.info("No authenticated accounts. Open the Web UI to add accounts.")

    # ── run forever ──────────────────────────────────────────────
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
    finally:
        # graceful shutdown
        async def shutdown():
            for aid, inst in list(instances.items()):
                try:
                    await inst.stop()
                except Exception as e:
                    logger.warning("Error stopping {}: {}", aid, e)

        loop.run_until_complete(shutdown())
        loop.close()
        logger.info("Stopped!")


if __name__ == "__main__":
    # Multi-account mode is the new default
    main_multi()
