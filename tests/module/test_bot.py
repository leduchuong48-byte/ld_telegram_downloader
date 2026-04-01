"""test bot"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from module.bot import direct_download


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_direct_download_initial_reply_uses_status_message(self):
        download_bot = mock.Mock()
        download_bot.bot = mock.AsyncMock()
        download_bot.bot.send_message = mock.AsyncMock(
            return_value=SimpleNamespace(id=77)
        )
        download_bot.bot.edit_message_text = mock.AsyncMock()
        download_bot.bot_media_client = mock.Mock()

        message = mock.Mock()
        message.from_user.id = 42
        message.id = 501

        download_message = mock.Mock()
        download_message.id = 601
        download_message.empty = False

        fake_bot = mock.Mock()
        fake_bot.gen_task_id.return_value = 123
        fake_bot.add_download_task = mock.AsyncMock(return_value=True)
        fake_bot.add_task_node = mock.Mock()
        fake_bot.remove_task_node = mock.Mock()

        with mock.patch("module.bot._bot", fake_bot):
            self.loop.run_until_complete(
                direct_download(
                    download_bot,
                    chat_id=42,
                    message=message,
                    download_message=download_message,
                    client=mock.Mock(),
                )
            )

        sent_text = download_bot.bot.send_message.await_args_list[0].args[1]
        self.assertIn("🆔 task id: 123", sent_text)
        self.assertIn("📥", sent_text)
        self.assertNotIn("Direct download queued.", sent_text)

