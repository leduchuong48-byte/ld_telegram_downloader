"""test pyrogram extension"""

import asyncio
import unittest
from unittest import mock

from module.app import DownloadStatus, ForwardStatus, TaskNode
from module.pyrogram_extension import upload_telegram_chat


class PyrogramExtensionTestCase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_upload_telegram_chat_no_delete_on_failure(self):
        node = TaskNode(chat_id=1, upload_telegram_chat_id=123)
        message = mock.Mock()
        message.media = True
        message.media_group_id = None

        app = mock.Mock()
        app.after_upload_telegram_delete = True

        async def _raise_upload(*_args, **_kwargs):
            raise Exception("upload failed")

        with mock.patch(
            "module.pyrogram_extension.upload_telegram_chat_message",
            new=_raise_upload,
        ), mock.patch("module.pyrogram_extension.os.remove") as mock_remove:
            self.loop.run_until_complete(
                upload_telegram_chat(
                    client=mock.Mock(),
                    upload_user=mock.Mock(),
                    app=app,
                    node=node,
                    message=message,
                    download_status=DownloadStatus.SuccessDownload,
                    file_name="file.txt",
                )
            )

        mock_remove.assert_not_called()

    def test_upload_telegram_chat_delete_on_success(self):
        node = TaskNode(chat_id=1, upload_telegram_chat_id=123)
        message = mock.Mock()
        message.media = True
        message.media_group_id = None

        app = mock.Mock()
        app.after_upload_telegram_delete = True

        async def _success_upload(*_args, **_kwargs):
            return ForwardStatus.SuccessForward

        with mock.patch(
            "module.pyrogram_extension.upload_telegram_chat_message",
            new=_success_upload,
        ), mock.patch("module.pyrogram_extension.os.remove") as mock_remove:
            self.loop.run_until_complete(
                upload_telegram_chat(
                    client=mock.Mock(),
                    upload_user=mock.Mock(),
                    app=app,
                    node=node,
                    message=message,
                    download_status=DownloadStatus.SuccessDownload,
                    file_name="file.txt",
                )
            )

        mock_remove.assert_called_once_with("file.txt")

