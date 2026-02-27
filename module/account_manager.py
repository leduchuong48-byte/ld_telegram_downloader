"""Multi-account manager for ld_telegram_downloader.

Manages multiple Telegram user accounts, each with an optional bot.
Persists account registry to accounts.yaml.
"""

import os
import shutil
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from loguru import logger
from ruamel import yaml

_yaml = yaml.YAML()

ACCOUNTS_FILE = "accounts.yaml"
CONFIGS_DIR = "configs"
SESSIONS_DIR = "sessions"


class AccountStatus(Enum):
    """Account authentication status."""

    Pending = "pending"
    Authenticating = "authenticating"
    WaitingCode = "waiting_code"
    WaitingPassword = "waiting_password"
    Authenticated = "authenticated"
    Failed = "failed"


@dataclass
class AccountConfig:
    """Persistent account configuration."""

    account_id: str
    phone: str = ""
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    session_name: str = ""
    status: str = "pending"

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "phone": self.phone,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "bot_token": self.bot_token,
            "session_name": self.session_name,
            "status": self.status,
        }

    @staticmethod
    def from_dict(data: dict) -> "AccountConfig":
        return AccountConfig(
            account_id=data.get("account_id", ""),
            phone=data.get("phone", ""),
            api_id=int(data.get("api_id", 0)),
            api_hash=data.get("api_hash", ""),
            bot_token=data.get("bot_token", ""),
            session_name=data.get("session_name", ""),
            status=data.get("status", "pending"),
        )


class AccountManager:
    """Manages multiple Telegram accounts.

    Responsibilities:
    - CRUD operations on account registry (accounts.yaml)
    - Per-account config file management (configs/<id>.yaml)
    - Account status tracking
    - Migration from legacy single-account config
    """

    def __init__(self, base_dir: str = "."):
        self.base_dir = os.path.abspath(base_dir)
        self.accounts_file = os.path.join(self.base_dir, ACCOUNTS_FILE)
        self.configs_dir = os.path.join(self.base_dir, CONFIGS_DIR)
        self.sessions_dir = os.path.join(self.base_dir, SESSIONS_DIR)
        self.accounts: Dict[str, AccountConfig] = {}

        os.makedirs(self.configs_dir, exist_ok=True)
        os.makedirs(self.sessions_dir, exist_ok=True)

    # ── persistence ──────────────────────────────────────────────

    def load(self):
        """Load account registry from accounts.yaml."""
        if not os.path.exists(self.accounts_file):
            self.accounts = {}
            return

        with open(self.accounts_file, encoding="utf-8") as f:
            data = _yaml.load(f.read())

        if not data or "accounts" not in data:
            self.accounts = {}
            return

        self.accounts = {}
        for item in data["accounts"]:
            cfg = AccountConfig.from_dict(item)
            self.accounts[cfg.account_id] = cfg

        logger.info("Loaded {} account(s) from registry", len(self.accounts))

    def save(self):
        """Persist account registry to accounts.yaml."""
        data = {"accounts": [acc.to_dict() for acc in self.accounts.values()]}
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            _yaml.dump(data, f)

    # ── CRUD ─────────────────────────────────────────────────────

    def add_account(self, api_id: int, api_hash: str) -> AccountConfig:
        """Create a new account entry. Returns the AccountConfig."""
        account_id = f"acc_{uuid.uuid4().hex[:8]}"
        session_name = account_id

        cfg = AccountConfig(
            account_id=account_id,
            api_id=api_id,
            api_hash=api_hash,
            session_name=session_name,
            status=AccountStatus.Pending.value,
        )
        self.accounts[account_id] = cfg

        # create per-account config from template
        self._create_account_config(cfg)
        self.save()
        logger.info("Added account {}", account_id)
        return cfg

    def remove_account(self, account_id: str) -> bool:
        """Remove an account and its associated files."""
        if account_id not in self.accounts:
            return False

        cfg = self.accounts[account_id]

        # remove session files
        for suffix in ["", "-shm", "-wal", "-journal"]:
            path = os.path.join(
                self.sessions_dir, f"{cfg.session_name}.session{suffix}"
            )
            if os.path.exists(path):
                os.remove(path)
            # bot session
            bot_path = os.path.join(
                self.sessions_dir, f"{cfg.session_name}_bot.session{suffix}"
            )
            if os.path.exists(bot_path):
                os.remove(bot_path)

        # remove per-account config
        config_path = os.path.join(self.configs_dir, f"{account_id}.yaml")
        if os.path.exists(config_path):
            os.remove(config_path)

        del self.accounts[account_id]
        self.save()
        logger.info("Removed account {}", account_id)
        return True

    def get_account(self, account_id: str) -> Optional[AccountConfig]:
        return self.accounts.get(account_id)

    def list_accounts(self) -> List[AccountConfig]:
        return list(self.accounts.values())

    def get_authenticated_accounts(self) -> List[AccountConfig]:
        return [
            acc
            for acc in self.accounts.values()
            if acc.status == AccountStatus.Authenticated.value
        ]

    # ── status ───────────────────────────────────────────────────

    def set_status(self, account_id: str, status: AccountStatus):
        if account_id in self.accounts:
            self.accounts[account_id].status = status.value
            self.save()

    def set_phone(self, account_id: str, phone: str):
        if account_id in self.accounts:
            self.accounts[account_id].phone = phone
            self.save()

    def set_bot_token(self, account_id: str, bot_token: str):
        if account_id in self.accounts:
            self.accounts[account_id].bot_token = bot_token
            self._update_account_config_field(account_id, "bot_token", bot_token)
            self.save()

    # ── per-account config ───────────────────────────────────────

    def get_account_config_path(self, account_id: str) -> str:
        return os.path.join(self.configs_dir, f"{account_id}.yaml")

    def get_account_data_path(self, account_id: str) -> str:
        return os.path.join(self.configs_dir, f"{account_id}_data.yaml")

    def _create_account_config(self, cfg: AccountConfig):
        """Create a per-account config yaml from the global template."""
        config_path = self.get_account_config_path(cfg.account_id)
        if os.path.exists(config_path):
            return

        # read global config as template
        global_config_path = os.path.join(self.base_dir, "config.yaml")
        if os.path.exists(global_config_path):
            with open(global_config_path, encoding="utf-8") as f:
                template = _yaml.load(f.read()) or {}
        else:
            template = {}

        # override auth fields
        template["api_id"] = cfg.api_id
        template["api_hash"] = cfg.api_hash
        template["bot_token"] = cfg.bot_token
        # ensure save_path uses /app/downloads (container path)
        template.setdefault("save_path", "/app/downloads")

        with open(config_path, "w", encoding="utf-8") as f:
            _yaml.dump(template, f)

        # create empty data file
        data_path = self.get_account_data_path(cfg.account_id)
        if not os.path.exists(data_path):
            with open(data_path, "w", encoding="utf-8") as f:
                _yaml.dump({}, f)

    def _update_account_config_field(self, account_id: str, key: str, value):
        """Update a single field in the per-account config yaml."""
        config_path = self.get_account_config_path(account_id)
        if not os.path.exists(config_path):
            return

        with open(config_path, encoding="utf-8") as f:
            data = _yaml.load(f.read()) or {}

        data[key] = value

        with open(config_path, "w", encoding="utf-8") as f:
            _yaml.dump(data, f)

    # ── migration ────────────────────────────────────────────────

    def migrate_legacy_config(self) -> Optional[str]:
        """Migrate legacy single-account config.yaml + session files.

        Returns the new account_id if migration happened, None otherwise.
        """
        if self.accounts:
            return None  # already have accounts, skip

        global_config_path = os.path.join(self.base_dir, "config.yaml")
        if not os.path.exists(global_config_path):
            return None

        with open(global_config_path, encoding="utf-8") as f:
            config = _yaml.load(f.read()) or {}

        api_id = config.get("api_id")
        api_hash = config.get("api_hash")
        if not api_id or not api_hash:
            return None

        # check if legacy session exists
        legacy_session = os.path.join(self.sessions_dir, "ld_tg_downloader.session")
        has_session = os.path.exists(legacy_session)

        # create account
        account_id = "acc_default"
        session_name = account_id
        bot_token = config.get("bot_token", "")

        cfg = AccountConfig(
            account_id=account_id,
            phone="",
            api_id=int(api_id),
            api_hash=str(api_hash),
            bot_token=str(bot_token) if bot_token else "",
            session_name=session_name,
            status=(
                AccountStatus.Authenticated.value
                if has_session
                else AccountStatus.Pending.value
            ),
        )
        self.accounts[account_id] = cfg

        # copy per-account config from global
        config_path = self.get_account_config_path(account_id)
        shutil.copy2(global_config_path, config_path)

        # create data file
        data_path = self.get_account_data_path(account_id)
        global_data_path = os.path.join(self.base_dir, "data.yaml")
        if os.path.exists(global_data_path):
            shutil.copy2(global_data_path, data_path)
        else:
            with open(data_path, "w", encoding="utf-8") as f:
                _yaml.dump({}, f)

        # rename legacy session files to new naming
        if has_session:
            for suffix in ["", "-shm", "-wal", "-journal"]:
                src = os.path.join(
                    self.sessions_dir, f"ld_tg_downloader.session{suffix}"
                )
                dst = os.path.join(self.sessions_dir, f"{session_name}.session{suffix}")
                if os.path.exists(src):
                    shutil.copy2(src, dst)

        # rename legacy bot session
        legacy_bot_session = os.path.join(
            self.sessions_dir, "ld_tg_downloader_bot.session"
        )
        if os.path.exists(legacy_bot_session):
            for suffix in ["", "-shm", "-wal", "-journal"]:
                src = os.path.join(
                    self.sessions_dir,
                    f"ld_tg_downloader_bot.session{suffix}",
                )
                dst = os.path.join(
                    self.sessions_dir,
                    f"{session_name}_bot.session{suffix}",
                )
                if os.path.exists(src):
                    shutil.copy2(src, dst)

        self.save()
        logger.info(
            "Migrated legacy config to account '{}' (session={})",
            account_id,
            has_session,
        )
        return account_id

    def has_session_file(self, account_id: str) -> bool:
        """Check if a session file exists for the given account."""
        cfg = self.accounts.get(account_id)
        if not cfg:
            return False
        path = os.path.join(self.sessions_dir, f"{cfg.session_name}.session")
        return os.path.exists(path)
