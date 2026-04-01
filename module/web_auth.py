"""Web-based Telegram authentication state machine.

Handles the multi-step Pyrogram auth flow via HTTP endpoints:
  phone → send_code → sign_in → (optional 2FA) → done

Each account has its own auth session tracked independently.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional

import pyrogram
from loguru import logger
from pyrogram.errors import (
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    RPCError,
    SessionPasswordNeeded,
)
from pyrogram.types import TermsOfService, User

from module.account_manager import AccountManager, AccountStatus


@dataclass
class AuthSession:
    """Tracks the state of an in-progress authentication."""

    account_id: str
    step: str = "phone"  # phone | code | password | success | error
    phone: str = ""
    phone_code_hash: str = ""
    error: str = ""
    client: Optional[pyrogram.Client] = None
    user_info: Optional[dict] = None


class WebAuthManager:
    """Manages concurrent auth sessions for multiple accounts.

    Usage from Flask routes:
        wam = WebAuthManager(account_manager)
        session = wam.get_or_create(account_id)
        result = await wam.send_code(account_id, phone)
        result = await wam.verify_code(account_id, code)
        result = await wam.verify_password(account_id, password)
    """

    def __init__(self, manager: AccountManager, loop: asyncio.AbstractEventLoop):
        self.manager = manager
        self.loop = loop
        self._sessions: Dict[str, AuthSession] = {}

    def get_session(self, account_id: str) -> Optional[AuthSession]:
        return self._sessions.get(account_id)

    def get_or_create(self, account_id: str) -> AuthSession:
        if account_id not in self._sessions:
            self._sessions[account_id] = AuthSession(account_id=account_id)
        return self._sessions[account_id]

    def remove_session(self, account_id: str):
        session = self._sessions.pop(account_id, None)
        if session and session.client:
            try:
                self.loop.create_task(session.client.disconnect())
            except Exception:
                pass

    def get_status(self, account_id: str) -> dict:
        """Return JSON-serializable status for an auth session."""
        session = self._sessions.get(account_id)
        if not session:
            return {"step": "none", "error": ""}
        return {
            "step": session.step,
            "error": session.error,
            "phone": session.phone,
            "user_info": session.user_info,
        }

    # ── auth flow steps ──────────────────────────────────────────

    async def send_code(self, account_id: str, phone: str) -> dict:
        """Step 1: Send verification code to phone number.

        Returns: {"ok": bool, "step": str, "error": str}
        """
        acc = self.manager.get_account(account_id)
        if not acc:
            return {"ok": False, "step": "error", "error": "Account not found"}

        session = self.get_or_create(account_id)

        # disconnect previous client if any
        if session.client:
            try:
                await session.client.disconnect()
            except Exception:
                pass

        # create a fresh pyrogram client (connect only, no authorize)
        client = pyrogram.Client(
            acc.session_name,
            api_id=acc.api_id,
            api_hash=acc.api_hash,
            workdir=self.manager.sessions_dir,
        )

        try:
            await client.connect()
            sent_code = await client.send_code(phone)

            session.client = client
            session.phone = phone
            session.phone_code_hash = sent_code.phone_code_hash
            session.step = "code"
            session.error = ""

            self.manager.set_phone(account_id, phone)
            self.manager.set_status(account_id, AccountStatus.WaitingCode)

            logger.info("[{}] Verification code sent to {}", account_id, phone)
            return {"ok": True, "step": "code", "error": ""}

        except RPCError as e:
            session.step = "phone"
            session.error = str(e)
            logger.error("[{}] send_code RPC error: {}", account_id, e)
            return {"ok": False, "step": "phone", "error": str(e)}
        except Exception as e:
            session.step = "phone"
            session.error = str(e)
            logger.error("[{}] send_code error: {}", account_id, e)
            if client.is_connected:
                await client.disconnect()
            return {"ok": False, "step": "phone", "error": str(e)}

    async def verify_code(self, account_id: str, code: str) -> dict:
        """Step 2: Verify the SMS/Telegram code.

        Returns: {"ok": bool, "step": str, "error": str}
        """
        session = self._sessions.get(account_id)
        if not session or not session.client:
            return {"ok": False, "step": "error", "error": "No auth session"}

        try:
            signed_in = await session.client.sign_in(
                session.phone, session.phone_code_hash, code
            )

            if isinstance(signed_in, User):
                return await self._auth_success(session, signed_in)

            if isinstance(signed_in, TermsOfService):
                await session.client.accept_terms_of_service(str(signed_in.id))
                user = await session.client.get_me()
                return await self._auth_success(session, user)

            # needs registration (rare)
            session.step = "error"
            session.error = "Account needs registration"
            return {"ok": False, "step": "error", "error": session.error}

        except SessionPasswordNeeded:
            session.step = "password"
            session.error = ""
            self.manager.set_status(account_id, AccountStatus.WaitingPassword)
            logger.info("[{}] 2FA password required", account_id)
            return {"ok": True, "step": "password", "error": ""}

        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            session.step = "code"
            session.error = "Invalid or expired code. Please try again."
            logger.warning("[{}] Code error: {}", account_id, e)
            return {"ok": False, "step": "code", "error": session.error}

        except RPCError as e:
            session.step = "code"
            session.error = str(e)
            logger.error("[{}] verify_code RPC error: {}", account_id, e)
            return {"ok": False, "step": "code", "error": str(e)}

    async def verify_password(self, account_id: str, password: str) -> dict:
        """Step 3: Verify 2FA password.

        Returns: {"ok": bool, "step": str, "error": str}
        """
        session = self._sessions.get(account_id)
        if not session or not session.client:
            return {"ok": False, "step": "error", "error": "No auth session"}

        try:
            await session.client.check_password(password)
            user = await session.client.get_me()
            return await self._auth_success(session, user)

        except PasswordHashInvalid:
            session.step = "password"
            session.error = "Invalid password. Please try again."
            return {"ok": False, "step": "password", "error": session.error}

        except RPCError as e:
            session.step = "password"
            session.error = str(e)
            logger.error("[{}] verify_password RPC error: {}", account_id, e)
            return {"ok": False, "step": "password", "error": str(e)}

    async def _auth_success(self, session: AuthSession, user: User) -> dict:
        """Handle successful authentication."""
        session.step = "success"
        session.error = ""
        session.user_info = {
            "id": user.id,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
            "phone": user.phone_number or session.phone,
        }

        # disconnect the auth client — the real client will be created
        # by AccountInstance.start() using the persisted session file
        try:
            await session.client.disconnect()
        except Exception:
            pass
        session.client = None

        self.manager.set_status(session.account_id, AccountStatus.Authenticated)
        logger.success(
            "[{}] Authentication successful: {} ({})",
            session.account_id,
            user.first_name,
            user.id,
        )
        return {
            "ok": True,
            "step": "success",
            "error": "",
            "user": session.user_info,
        }

    # ── bot token validation ─────────────────────────────────────

    async def validate_bot_token(self, account_id: str, bot_token: str) -> dict:
        """Validate a bot token by connecting and getting bot info.

        Returns: {"ok": bool, "error": str, "bot_info": dict|None}
        """
        acc = self.manager.get_account(account_id)
        if not acc:
            return {"ok": False, "error": "Account not found", "bot_info": None}

        bot_client = pyrogram.Client(
            f"{acc.session_name}_bot",
            api_id=acc.api_id,
            api_hash=acc.api_hash,
            bot_token=bot_token,
            workdir=self.manager.sessions_dir,
        )

        try:
            await bot_client.start()
            me = await bot_client.get_me()
            bot_info = {
                "id": me.id,
                "first_name": me.first_name or "",
                "username": me.username or "",
            }
            await bot_client.stop()

            # persist
            self.manager.set_bot_token(account_id, bot_token)

            logger.success(
                "[{}] Bot validated: @{} ({})",
                account_id,
                me.username,
                me.id,
            )
            return {"ok": True, "error": "", "bot_info": bot_info}

        except RPCError as e:
            logger.error("[{}] Bot token validation RPC error: {}", account_id, e)
            return {"ok": False, "error": str(e), "bot_info": None}
        except Exception as e:
            logger.error("[{}] Bot token validation error: {}", account_id, e)
            return {"ok": False, "error": str(e), "bot_info": None}
        finally:
            try:
                if bot_client.is_connected:
                    await bot_client.stop()
            except Exception:
                pass
