"""SQLite persistence.

One file, four tables, WAL mode. `sqlite3` is synchronous, so every call is
wrapped in ``asyncio.to_thread`` and serialised behind a lock — the same
treatment the Cloudinary SDK gets, and for the same reason: a blocked event
loop makes Pause look broken.

The database holds live credentials (Telegram session strings, Cloudinary API
secrets). It is created with 0600 permissions and gitignored. Treat the file
the way you would treat a password manager export.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id      INTEGER NOT NULL UNIQUE,
    phone           TEXT,
    display_name    TEXT NOT NULL,
    username        TEXT,
    session_string  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT
);

CREATE TABLE IF NOT EXISTS cloudinary_configs (
    account_id      INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    cloud_name      TEXT NOT NULL,
    api_key         TEXT NOT NULL,
    api_secret      TEXT NOT NULL,
    folder          TEXT NOT NULL DEFAULT 'telegram_migration',
    folder_per_chat INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migrated_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    filename      TEXT,
    public_id     TEXT,
    secure_url    TEXT,
    resource_type TEXT,
    bytes         INTEGER,
    migrated_at   TEXT NOT NULL,
    UNIQUE (account_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_migrated_chat
    ON migrated_files (account_id, chat_id);

CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash  TEXT PRIMARY KEY,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    user_agent  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ lifecycle --

    def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        first_time = not self._path.exists()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        if first_time:
            log.info("Created database at %s", self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover - Windows / odd filesystems
            log.warning("Could not tighten permissions on %s", self._path)

    def close(self) -> None:
        if self._conn:
            with self._lock:
                self._conn.close()
            self._conn = None

    # -------------------------------------------------------------- plumbing --

    def _sync_write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        assert self._conn is not None, "Database.connect() was never called"
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def _sync_rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        assert self._conn is not None, "Database.connect() was never called"
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _sync_row(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self._sync_rows(sql, params)
        return rows[0] if rows else None

    async def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return await asyncio.to_thread(self._sync_write, sql, params)

    async def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._sync_rows, sql, params)

    async def _row(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._sync_row, sql, params)

    # -------------------------------------------------------------- accounts --

    async def upsert_account(
        self,
        *,
        tg_user_id: int,
        phone: str | None,
        display_name: str,
        username: str | None,
        session_string: str,
    ) -> dict[str, Any]:
        """Insert a freshly signed-in account, or refresh an existing one."""
        now = utcnow()
        await self._write(
            """
            INSERT INTO accounts
                (tg_user_id, phone, display_name, username, session_string,
                 created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                phone          = excluded.phone,
                display_name   = excluded.display_name,
                username       = excluded.username,
                session_string = excluded.session_string,
                last_used_at   = excluded.last_used_at
            """,
            (tg_user_id, phone, display_name, username, session_string, now, now),
        )
        row = await self._row("SELECT * FROM accounts WHERE tg_user_id = ?", (tg_user_id,))
        assert row is not None
        return dict(row)

    async def list_accounts(self) -> list[dict[str, Any]]:
        rows = await self._rows(
            """
            SELECT a.id, a.tg_user_id, a.phone, a.display_name, a.username,
                   a.created_at, a.last_used_at,
                   (c.account_id IS NOT NULL)            AS cloudinary_configured,
                   c.cloud_name                          AS cloud_name,
                   (SELECT COUNT(*) FROM migrated_files m
                     WHERE m.account_id = a.id)          AS migrated_total
              FROM accounts a
              LEFT JOIN cloudinary_configs c ON c.account_id = a.id
             ORDER BY a.last_used_at DESC, a.id DESC
            """
        )
        return [dict(r) for r in rows]

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        row = await self._row("SELECT * FROM accounts WHERE id = ?", (account_id,))
        return dict(row) if row else None

    async def touch_account(self, account_id: int) -> None:
        await self._write(
            "UPDATE accounts SET last_used_at = ? WHERE id = ?", (utcnow(), account_id)
        )

    async def update_session_string(self, account_id: int, session_string: str) -> None:
        await self._write(
            "UPDATE accounts SET session_string = ? WHERE id = ?",
            (session_string, account_id),
        )

    async def delete_account(self, account_id: int) -> None:
        # Cascades to cloudinary_configs and migrated_files.
        await self._write("DELETE FROM accounts WHERE id = ?", (account_id,))

    # ------------------------------------------------------------ cloudinary --

    async def set_cloudinary(
        self,
        account_id: int,
        *,
        cloud_name: str,
        api_key: str,
        api_secret: str,
        folder: str,
        folder_per_chat: bool,
    ) -> None:
        await self._write(
            """
            INSERT INTO cloudinary_configs
                (account_id, cloud_name, api_key, api_secret, folder,
                 folder_per_chat, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                cloud_name      = excluded.cloud_name,
                api_key         = excluded.api_key,
                api_secret      = excluded.api_secret,
                folder          = excluded.folder,
                folder_per_chat = excluded.folder_per_chat,
                updated_at      = excluded.updated_at
            """,
            (
                account_id,
                cloud_name,
                api_key,
                api_secret,
                folder,
                1 if folder_per_chat else 0,
                utcnow(),
            ),
        )

    async def get_cloudinary(self, account_id: int) -> dict[str, Any] | None:
        row = await self._row(
            "SELECT * FROM cloudinary_configs WHERE account_id = ?", (account_id,)
        )
        return dict(row) if row else None

    async def clear_cloudinary(self, account_id: int) -> None:
        await self._write("DELETE FROM cloudinary_configs WHERE account_id = ?", (account_id,))

    # -------------------------------------------------------- migrated files --

    async def mark_migrated(
        self,
        account_id: int,
        chat_id: int,
        message_id: int,
        *,
        filename: str,
        public_id: str,
        secure_url: str,
        resource_type: str,
        bytes_uploaded: int,
    ) -> None:
        await self._write(
            """
            INSERT INTO migrated_files
                (account_id, chat_id, message_id, filename, public_id,
                 secure_url, resource_type, bytes, migrated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, chat_id, message_id) DO UPDATE SET
                filename      = excluded.filename,
                public_id     = excluded.public_id,
                secure_url    = excluded.secure_url,
                resource_type = excluded.resource_type,
                bytes         = excluded.bytes,
                migrated_at   = excluded.migrated_at
            """,
            (
                account_id,
                chat_id,
                message_id,
                filename,
                public_id,
                secure_url,
                resource_type,
                bytes_uploaded,
                utcnow(),
            ),
        )

    async def is_migrated(self, account_id: int, chat_id: int, message_id: int) -> bool:
        row = await self._row(
            "SELECT 1 FROM migrated_files "
            "WHERE account_id = ? AND chat_id = ? AND message_id = ?",
            (account_id, chat_id, message_id),
        )
        return row is not None

    async def migrated_ids(self, account_id: int, chat_id: int) -> set[int]:
        rows = await self._rows(
            "SELECT message_id FROM migrated_files WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
        )
        return {int(r["message_id"]) for r in rows}

    async def chat_counts(self, account_id: int) -> dict[int, int]:
        rows = await self._rows(
            "SELECT chat_id, COUNT(*) AS n FROM migrated_files "
            "WHERE account_id = ? GROUP BY chat_id",
            (account_id,),
        )
        return {int(r["chat_id"]): int(r["n"]) for r in rows}

    async def total_count(self, account_id: int) -> int:
        row = await self._row(
            "SELECT COUNT(*) AS n FROM migrated_files WHERE account_id = ?", (account_id,)
        )
        return int(row["n"]) if row else 0

    async def forget_chat(self, account_id: int, chat_id: int) -> int:
        cursor = await self._write(
            "DELETE FROM migrated_files WHERE account_id = ? AND chat_id = ?",
            (account_id, chat_id),
        )
        return cursor.rowcount or 0

    # ----------------------------------------------------- dashboard sessions --

    async def create_session(
        self, token_hash: str, ttl_hours: int, user_agent: str | None
    ) -> None:
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        await self._write(
            "INSERT INTO dashboard_sessions "
            "(token_hash, account_id, created_at, expires_at, user_agent) "
            "VALUES (?, NULL, ?, ?, ?)",
            (token_hash, utcnow(), expires.isoformat(timespec="seconds"), user_agent),
        )

    async def get_session(self, token_hash: str) -> dict[str, Any] | None:
        row = await self._row(
            "SELECT * FROM dashboard_sessions WHERE token_hash = ?", (token_hash,)
        )
        if not row:
            return None
        expires = _parse(row["expires_at"])
        if expires and expires < datetime.now(timezone.utc):
            await self.delete_session(token_hash)
            return None
        return dict(row)

    async def bind_session_account(self, token_hash: str, account_id: int | None) -> None:
        await self._write(
            "UPDATE dashboard_sessions SET account_id = ? WHERE token_hash = ?",
            (account_id, token_hash),
        )

    async def delete_session(self, token_hash: str) -> None:
        await self._write("DELETE FROM dashboard_sessions WHERE token_hash = ?", (token_hash,))

    async def purge_expired_sessions(self) -> int:
        cursor = await self._write(
            "DELETE FROM dashboard_sessions WHERE expires_at < ?", (utcnow(),)
        )
        return cursor.rowcount or 0
