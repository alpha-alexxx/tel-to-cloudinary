"""Per-account runtimes.

Each signed-in account gets its own Telegram connection, Cloudinary target,
migration manager and event hub. That isolation is the whole point of the
multi-account design: two accounts can run migrations at the same time without
sharing a log stream, a rate-limit budget, or — critically — a Cloudinary
destination.

Runtimes are started lazily (first time an account is used after a restart)
and stopped when the account signs out or the server shuts down.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cloudinary_service import CloudinaryCredentials, CloudinaryService
from config import Settings
from db import Database
from events import EventHub
from migration import MigrationManager
from state import MigrationStore
from telegram_service import SessionExpiredError, TelegramService

log = logging.getLogger(__name__)


class AccountRuntime:
    def __init__(
        self,
        account: dict[str, Any],
        telegram: TelegramService,
        settings: Settings,
        db: Database,
    ) -> None:
        self.account_id = int(account["id"])
        self.account = account
        self.telegram = telegram
        self.hub = EventHub()
        self.store = MigrationStore(db, self.account_id)
        self.cloudinary: CloudinaryService | None = None
        self.migrator = MigrationManager(
            telegram, lambda: self.cloudinary, self.store, self.hub, settings
        )

    def set_cloudinary(self, credentials: CloudinaryCredentials | None, settings: Settings) -> None:
        self.cloudinary = (
            CloudinaryService(settings, credentials) if credentials else None
        )

    @property
    def cloudinary_configured(self) -> bool:
        return self.cloudinary is not None

    async def stop(self) -> None:
        await self.migrator.shutdown()
        await self.telegram.stop()


class RuntimeRegistry:
    """Account id → live runtime. Serialised so two requests can't race a start."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._runtimes: dict[int, AccountRuntime] = {}
        self._lock = asyncio.Lock()

    def peek(self, account_id: int) -> AccountRuntime | None:
        return self._runtimes.get(account_id)

    async def get(self, account_id: int) -> AccountRuntime:
        async with self._lock:
            existing = self._runtimes.get(account_id)
            if existing:
                return existing

            account = await self._db.get_account(account_id)
            if account is None:
                raise KeyError(f"No account {account_id}")

            service = TelegramService(self._settings, account["session_string"])
            try:
                await service.start()
            except SessionExpiredError:
                # The stored session was revoked elsewhere; drop it so the UI
                # sends the operator back through the OTP flow.
                await service.stop()
                await self._db.delete_account(account_id)
                raise

            runtime = AccountRuntime(account, service, self._settings, self._db)
            await self._load_cloudinary(runtime)
            self._runtimes[account_id] = runtime
            await self._db.touch_account(account_id)
            log.info("Runtime started for account %s (%s)", account_id, account["display_name"])
            return runtime

    async def adopt(self, account: dict[str, Any], telegram: TelegramService) -> AccountRuntime:
        """Register a runtime around a client that just finished signing in."""
        account_id = int(account["id"])
        async with self._lock:
            previous = self._runtimes.pop(account_id, None)
        if previous:
            await previous.stop()

        runtime = AccountRuntime(account, telegram, self._settings, self._db)
        await self._load_cloudinary(runtime)
        async with self._lock:
            self._runtimes[account_id] = runtime
        log.info("Runtime adopted for account %s (%s)", account_id, account["display_name"])
        return runtime

    async def refresh_cloudinary(self, account_id: int) -> None:
        runtime = self._runtimes.get(account_id)
        if runtime:
            await self._load_cloudinary(runtime)

    async def _load_cloudinary(self, runtime: AccountRuntime) -> None:
        row = await self._db.get_cloudinary(runtime.account_id)
        credentials = (
            CloudinaryCredentials(
                cloud_name=row["cloud_name"],
                api_key=row["api_key"],
                api_secret=row["api_secret"],
                folder=row["folder"],
                folder_per_chat=bool(row["folder_per_chat"]),
            )
            if row
            else None
        )
        runtime.set_cloudinary(credentials, self._settings)

    async def drop(self, account_id: int) -> None:
        async with self._lock:
            runtime = self._runtimes.pop(account_id, None)
        if runtime:
            await runtime.stop()
            log.info("Runtime stopped for account %s", account_id)

    async def shutdown(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            try:
                await runtime.stop()
            except Exception:  # noqa: BLE001
                log.warning("Runtime shutdown failed", exc_info=True)

    def active_ids(self) -> list[int]:
        return list(self._runtimes)
