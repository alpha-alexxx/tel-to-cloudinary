"""Dashboard authentication and the Telegram OTP sign-in flow.

Two independent gates, in order:

1. **Passcode** — proves you are allowed to use this deployment at all. Without
   it, anyone who reaches the port could make Telegram send a login code to a
   stranger's phone, which is both an abuse vector and a fast way to get an
   api_id banned. Failed attempts are rate-limited with a lockout.

2. **Telegram sign-in** — phone → code → optional 2FA password, driven from the
   browser. The session string produced at the end is written to SQLite and
   never sent to the client.

An unfinished login holds a live, connected `TelegramClient` in memory (the
code and its `phone_code_hash` are only valid on the connection that requested
them). Those pending clients expire and are disconnected by a sweeper.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest

from config import Settings
from db import Database

log = logging.getLogger(__name__)


class AuthError(Exception):
    """User-facing authentication failure (mapped to 4xx by the API layer)."""

    def __init__(self, message: str, *, status: int = 400, code: str = "auth_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Passcode gate
# --------------------------------------------------------------------------- #


class PasscodeGate:
    """Constant-time passcode check with a simple lockout after N failures."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._failures = 0
        self._locked_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def locked_seconds(self) -> int:
        remaining = self._locked_until - time.monotonic()
        return int(remaining) if remaining > 0 else 0

    async def verify(self, passcode: str) -> None:
        async with self._lock:
            if self.locked_seconds:
                raise AuthError(
                    f"Too many failed attempts. Try again in "
                    f"{self.locked_seconds // 60 + 1} minute(s).",
                    status=429,
                    code="locked_out",
                )

            # Deliberate delay: makes online guessing tedious, costs a real
            # user a third of a second once.
            await asyncio.sleep(0.3)

            if hmac.compare_digest(passcode, self._settings.dashboard_passcode):
                self._failures = 0
                return

            self._failures += 1
            left = self._settings.max_passcode_attempts - self._failures
            if left <= 0:
                self._locked_until = time.monotonic() + self._settings.lockout_minutes * 60
                self._failures = 0
                raise AuthError(
                    f"Too many failed attempts. Locked for "
                    f"{self._settings.lockout_minutes} minutes.",
                    status=429,
                    code="locked_out",
                )
            raise AuthError(
                f"Wrong passcode. {left} attempt(s) left.", status=401, code="bad_passcode"
            )


# --------------------------------------------------------------------------- #
# Telegram OTP login
# --------------------------------------------------------------------------- #


@dataclass
class PendingLogin:
    login_id: str
    phone: str
    client: TelegramClient
    phone_code_hash: str
    created_at: float = field(default_factory=time.monotonic)
    needs_password: bool = False
    hint: str | None = None


class TelegramLoginFlow:
    """Owns half-finished sign-ins: send code → verify code → 2FA password."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pending: dict[str, PendingLogin] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- step one --

    async def send_code(self, phone: str) -> dict[str, Any]:
        phone = phone.strip().replace(" ", "")
        if not phone.startswith("+") or len(phone) < 8:
            raise AuthError(
                "Enter the phone number in international format, e.g. +919876543210.",
                code="bad_phone",
            )

        client = TelegramClient(
            StringSession(),
            self._settings.tg_api_id,
            self._settings.tg_api_hash,
            flood_sleep_threshold=0,  # surface flood waits instead of hanging the request
        )
        await client.connect()

        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            await _close(client)
            raise AuthError("Telegram doesn't recognise that phone number.",
                            code="bad_phone") from exc
        except PhoneNumberBannedError as exc:
            await _close(client)
            raise AuthError("That phone number is banned from Telegram.",
                            status=403, code="banned") from exc
        except FloodWaitError as exc:
            await _close(client)
            raise AuthError(
                f"Telegram is rate-limiting login attempts. Wait {exc.seconds}s.",
                status=429,
                code="flood_wait",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            await _close(client)
            raise AuthError(f"Couldn't send the code: {exc}", status=502,
                            code="send_failed") from exc

        login_id = secrets.token_urlsafe(16)
        async with self._lock:
            self._pending[login_id] = PendingLogin(
                login_id=login_id,
                phone=phone,
                client=client,
                phone_code_hash=sent.phone_code_hash,
            )

        log.info("Login code sent to %s (login_id=%s)", _mask_phone(phone), login_id)
        return {
            "login_id": login_id,
            "phone": _mask_phone(phone),
            "expires_in": self._settings.login_ttl_minutes * 60,
        }

    # ------------------------------------------------------------- step two --

    async def verify_code(self, login_id: str, code: str) -> tuple[PendingLogin, bool]:
        """Returns (login, signed_in). signed_in=False means 2FA is required."""
        pending = await self._get(login_id)
        code = code.strip().replace(" ", "").replace("-", "")
        if not code.isdigit():
            raise AuthError("The login code is digits only.", code="bad_code")

        try:
            await pending.client.sign_in(
                phone=pending.phone, code=code, phone_code_hash=pending.phone_code_hash
            )
        except PhoneCodeInvalidError as exc:
            raise AuthError("That code isn't right. Check and re-enter it.",
                            code="bad_code") from exc
        except PhoneCodeExpiredError as exc:
            await self.discard(login_id)
            raise AuthError("That code expired. Start again to get a new one.",
                            code="code_expired") from exc
        except PhoneNumberUnoccupiedError as exc:
            await self.discard(login_id)
            raise AuthError(
                "No Telegram account exists for that number. Sign up in the "
                "Telegram app first.",
                code="no_account",
            ) from exc
        except SessionPasswordNeededError:
            pending.needs_password = True
            try:
                password_info = await pending.client(GetPasswordRequest())
                pending.hint = password_info.hint
            except Exception:  # noqa: BLE001 - the hint is a nicety, not a requirement
                pending.hint = None
            return pending, False
        except FloodWaitError as exc:
            raise AuthError(
                f"Too many attempts. Telegram wants {exc.seconds}s of silence.",
                status=429,
                code="flood_wait",
            ) from exc

        return pending, True

    # ----------------------------------------------------------- step three --

    async def submit_password(self, login_id: str, password: str) -> PendingLogin:
        pending = await self._get(login_id)
        if not pending.needs_password:
            raise AuthError("This account didn't ask for a 2FA password.",
                            code="no_password_needed")
        try:
            await pending.client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001 - Telethon raises several password errors
            name = type(exc).__name__
            if "Password" in name:
                raise AuthError("That 2FA password isn't right.",
                                code="bad_password") from exc
            raise AuthError(f"Sign-in failed: {exc}", status=502, code="signin_failed") from exc
        return pending

    # ---------------------------------------------------------------- utils --

    async def _get(self, login_id: str) -> PendingLogin:
        async with self._lock:
            pending = self._pending.get(login_id)
        if pending is None:
            raise AuthError("This login expired. Start again.", status=410,
                            code="login_expired")
        if time.monotonic() - pending.created_at > self._settings.login_ttl_minutes * 60:
            await self.discard(login_id)
            raise AuthError("This login expired. Start again.", status=410,
                            code="login_expired")
        return pending

    async def take(self, login_id: str) -> PendingLogin:
        """Remove the pending login and hand over its live client."""
        async with self._lock:
            return self._pending.pop(login_id)

    async def discard(self, login_id: str) -> None:
        async with self._lock:
            pending = self._pending.pop(login_id, None)
        if pending:
            await _close(pending.client)

    async def sweep(self) -> None:
        """Drop and disconnect logins nobody finished."""
        cutoff = self._settings.login_ttl_minutes * 60
        async with self._lock:
            stale = [
                lid
                for lid, p in self._pending.items()
                if time.monotonic() - p.created_at > cutoff
            ]
        for login_id in stale:
            log.info("Discarding stale login %s", login_id)
            await self.discard(login_id)

    async def shutdown(self) -> None:
        async with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            await _close(item.client)


async def _close(client: TelegramClient) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:  # noqa: BLE001
        log.debug("Failed to disconnect a pending login client", exc_info=True)


def _mask_phone(phone: str) -> str:
    return phone[:3] + "•" * max(len(phone) - 6, 0) + phone[-3:] if len(phone) > 6 else phone


# --------------------------------------------------------------------------- #
# Dashboard sessions
# --------------------------------------------------------------------------- #


class SessionService:
    """Issues and validates the browser cookie. Tokens are stored hashed."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def create(self, user_agent: str | None) -> str:
        token = secrets.token_urlsafe(32)
        await self._db.create_session(
            hash_token(token), self._settings.session_ttl_hours, user_agent
        )
        return token

    async def get(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        return await self._db.get_session(hash_token(token))

    async def bind_account(self, token: str, account_id: int | None) -> None:
        await self._db.bind_session_account(hash_token(token), account_id)

    async def destroy(self, token: str | None) -> None:
        if token:
            await self._db.delete_session(hash_token(token))
