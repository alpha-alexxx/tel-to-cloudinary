"""Account-scoped view of the migrated-files table.

`MigrationManager` shouldn't know about account ids or SQL, so this thin
adapter binds one account to the database and exposes the same handful of
questions the migration loop actually asks: has this been done, what's been
done in this chat, record this success.
"""

from __future__ import annotations

import logging

from db import Database

log = logging.getLogger(__name__)


class MigrationStore:
    def __init__(self, db: Database, account_id: int) -> None:
        self._db = db
        self._account_id = account_id

    @property
    def account_id(self) -> int:
        return self._account_id

    async def is_migrated(self, chat_id: int, message_id: int) -> bool:
        return await self._db.is_migrated(self._account_id, chat_id, message_id)

    async def migrated_ids(self, chat_id: int) -> set[int]:
        return await self._db.migrated_ids(self._account_id, chat_id)

    async def chat_counts(self) -> dict[int, int]:
        return await self._db.chat_counts(self._account_id)

    async def total_count(self) -> int:
        return await self._db.total_count(self._account_id)

    async def mark_migrated(
        self,
        chat_id: int,
        message_id: int,
        *,
        filename: str,
        public_id: str,
        secure_url: str,
        resource_type: str,
        bytes_uploaded: int,
    ) -> None:
        await self._db.mark_migrated(
            self._account_id,
            chat_id,
            message_id,
            filename=filename,
            public_id=public_id,
            secure_url=secure_url,
            resource_type=resource_type,
            bytes_uploaded=bytes_uploaded,
        )

    async def forget_chat(self, chat_id: int) -> int:
        return await self._db.forget_chat(self._account_id, chat_id)
