"""Tests for account manager legacy config synchronization."""

from pathlib import Path

from module.account_manager import AccountManager


def test_sync_global_config_bot_token_uses_single_account_token(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "api_id: 123",
                "api_hash: hash",
                "bot_token: old-token",
                "media_types:",
                "  - document",
                "file_formats:",
                "  document:",
                "    - all",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text(
        "\n".join(
            [
                "accounts:",
                "  - account_id: acc_default",
                "    api_id: 123",
                "    api_hash: hash",
                "    bot_token: current-token",
                "    session_name: acc_default",
                "    status: authenticated",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manager = AccountManager(base_dir=str(tmp_path))
    manager.load()

    changed = manager.sync_global_config_bot_token()

    assert changed is True
    assert "bot_token: current-token" in config_path.read_text(encoding="utf-8")


def test_set_bot_token_keeps_global_config_in_sync_for_single_account(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "bot_token: old-token\n",
        encoding="utf-8",
    )

    accounts_path = tmp_path / "accounts.yaml"
    accounts_path.write_text(
        "\n".join(
            [
                "accounts:",
                "  - account_id: acc_default",
                "    api_id: 123",
                "    api_hash: hash",
                "    bot_token: current-token",
                "    session_name: acc_default",
                "    status: authenticated",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "acc_default.yaml").write_text(
        "bot_token: current-token\n",
        encoding="utf-8",
    )

    manager = AccountManager(base_dir=str(tmp_path))
    manager.load()

    manager.set_bot_token("acc_default", "new-token")

    assert "bot_token: new-token" in config_path.read_text(encoding="utf-8")
