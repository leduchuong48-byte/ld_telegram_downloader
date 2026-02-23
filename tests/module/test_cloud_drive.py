"""test cloud drive"""

import asyncio
import unittest
from unittest import mock

from module.cloud_drive import CloudDrive, CloudDriveConfig


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._iter = iter(self._lines)

    def __aiter__(self):
        self._iter = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeProc:
    def __init__(self, stdout_lines=(), return_code=0):
        self.stdout = _FakeStdout(stdout_lines)
        self._return_code = return_code

    async def wait(self):
        return self._return_code

    def kill(self):
        return None


class CloudDriveTestCase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_rclone_upload_file_rc0_success(self):
        drive_config = CloudDriveConfig(
            enable_upload_file=True,
            upload_adapter="rclone",
            rclone_path="rclone",
            remote_dir="remote",
        )
        drive_config.before_upload_file_zip = True
        drive_config.after_upload_file_delete = True

        async def _fake_create_subprocess_shell(*_args, **_kwargs):
            return _FakeProc(stdout_lines=[b"not a progress line\n"], return_code=0)

        with mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_shell",
            new=_fake_create_subprocess_shell,
        ), mock.patch("module.cloud_drive.CloudDrive.rclone_mkdir"), mock.patch(
            "module.cloud_drive.CloudDrive.zip_file",
            return_value="save/dir/file.zip",
        ), mock.patch("module.cloud_drive.os.remove") as mock_remove:
            ret = self.loop.run_until_complete(
                CloudDrive.rclone_upload_file(
                    drive_config,
                    save_path="save",
                    local_file_path="save/dir/file.txt",
                    timeout=1,
                )
            )

        self.assertTrue(ret)
        self.assertEqual(drive_config.total_upload_success_file_count, 1)
        mock_remove.assert_any_call("save/dir/file.txt")
        mock_remove.assert_any_call("save/dir/file.zip")

    def test_rclone_upload_file_rc_nonzero_failure(self):
        drive_config = CloudDriveConfig(
            enable_upload_file=True,
            upload_adapter="rclone",
            rclone_path="rclone",
            remote_dir="remote",
        )
        drive_config.before_upload_file_zip = True
        drive_config.after_upload_file_delete = True

        async def _fake_create_subprocess_shell(*_args, **_kwargs):
            return _FakeProc(stdout_lines=[b"some output\n"], return_code=1)

        with mock.patch(
            "module.cloud_drive.asyncio.create_subprocess_shell",
            new=_fake_create_subprocess_shell,
        ), mock.patch("module.cloud_drive.CloudDrive.rclone_mkdir"), mock.patch(
            "module.cloud_drive.CloudDrive.zip_file",
            return_value="save/dir/file.zip",
        ), mock.patch("module.cloud_drive.os.remove") as mock_remove:
            ret = self.loop.run_until_complete(
                CloudDrive.rclone_upload_file(
                    drive_config,
                    save_path="save",
                    local_file_path="save/dir/file.txt",
                    timeout=1,
                )
            )

        self.assertFalse(ret)
        self.assertEqual(drive_config.total_upload_success_file_count, 0)
        mock_remove.assert_called_once_with("save/dir/file.zip")

