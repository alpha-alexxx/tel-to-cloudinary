"""Telethon wrapper: dialog listing, media discovery, downloads.

Telegram speaks MTProto over a raw, persistent TCP socket — which is exactly
why this process has to be a long-lived server (Codespaces / any Linux VM)
rather than a fetch-only edge function.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    Channel,
    Chat,
    MessageMediaWebPage,
    User,
)

from config import ALLOWED_EXTENSIONS, KIND_EXTENSIONS, Settings, kind_for_extension

log = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\- ]+")


def safe_filename(name: str, fallback: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", name).strip(" .")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    return cleaned[:120] or fallback


def slugify(value: str, fallback: str = "chat") -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug[:60] or fallback


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One migratable file, flattened for the API and the migration loop."""

    message_id: int
    filename: str
    size: int
    ext: str
    kind: str
    mime_type: str | None
    date: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DialogInfo:
    id: int
    title: str
    kind: str            # channel | group | dm
    username: str | None
    unread: int
    migrated_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class SessionExpiredError(RuntimeError):
    """The stored session string no longer authorises this account."""


class TelegramService:
    """One authenticated Telegram connection, belonging to one account."""

    def __init__(self, settings: Settings, session_string: str) -> None:
        self._settings = settings
        self._client = TelegramClient(
            StringSession(session_string),
            settings.tg_api_id,
            settings.tg_api_hash,
            flood_sleep_threshold=settings.flood_sleep_threshold,
            connection_retries=5,
            retry_delay=2,
        )
        self._entity_cache: dict[int, object] = {}
        self._me_name: str | None = None

    @classmethod
    def adopt(cls, settings: Settings, client: TelegramClient) -> "TelegramService":
        """Take over a client that just completed the OTP flow.

        The login client is already connected and authorised — reconnecting
        with its session string would work but costs an extra round trip and a
        second auth key handshake for no reason.
        """
        service = cls.__new__(cls)
        service._settings = settings
        service._client = client
        service._entity_cache = {}
        service._me_name = None
        return service

    # ------------------------------------------------------------ lifecycle --

    async def start(self) -> None:
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise SessionExpiredError(
                "This Telegram session is no longer valid — it was probably "
                "revoked from another device. Sign in again."
            )
        me = await self._client.get_me()
        self._me_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username
        log.info("Telegram connected as %s (id=%s)", self._me_name, me.id)

    def session_string(self) -> str:
        return self._client.session.save()

    async def sign_out(self) -> bool:
        """Revoke this session on Telegram's side, not just locally."""
        try:
            return bool(await self._client.log_out())
        except Exception:  # noqa: BLE001
            log.warning("Remote sign-out failed; dropping the local session anyway.")
            return False

    async def whoami(self) -> dict:
        me = await self._client.get_me()
        return {
            "tg_user_id": me.id,
            "display_name": f"{me.first_name or ''} {me.last_name or ''}".strip()
            or (me.username or str(me.id)),
            "username": me.username,
            "phone": me.phone,
        }

    async def stop(self) -> None:
        if self._client.is_connected():
            await self._client.disconnect()
            log.info("Telegram disconnected.")

    @property
    def account_name(self) -> str | None:
        return self._me_name

    @property
    def connected(self) -> bool:
        return self._client.is_connected()

    # -------------------------------------------------------------- dialogs --

    async def list_dialogs(self, include_dms: bool | None = None) -> list[DialogInfo]:
        include_dms = self._settings.include_dms if include_dms is None else include_dms
        dialogs: list[DialogInfo] = []

        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, User):
                if not include_dms or entity.bot or entity.is_self:
                    continue
                kind = "dm"
            elif isinstance(entity, Channel):
                kind = "group" if getattr(entity, "megagroup", False) else "channel"
            elif isinstance(entity, Chat):
                kind = "group"
            else:
                continue

            self._entity_cache[dialog.id] = entity
            dialogs.append(
                DialogInfo(
                    id=dialog.id,
                    title=(dialog.name or "Untitled").strip(),
                    kind=kind,
                    username=getattr(entity, "username", None),
                    unread=dialog.unread_count or 0,
                )
            )
        return dialogs

    async def resolve_entity(self, chat_id: int):
        if chat_id in self._entity_cache:
            return self._entity_cache[chat_id]
        try:
            entity = await self._client.get_entity(chat_id)
        except ValueError:
            # Entity not in the local session cache yet — priming from dialogs
            # is the documented Telethon fix.
            await self.list_dialogs()
            if chat_id in self._entity_cache:
                return self._entity_cache[chat_id]
            raise
        self._entity_cache[chat_id] = entity
        return entity

    async def chat_title(self, chat_id: int) -> str:
        entity = await self.resolve_entity(chat_id)
        title = getattr(entity, "title", None)
        if not title:
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            title = f"{first} {last}".strip() or getattr(entity, "username", None) or str(chat_id)
        return title

    # ---------------------------------------------------------------- media --

    def _to_media_item(self, msg: Message) -> MediaItem | None:
        """Map a Telegram message to a MediaItem, or None if not migratable."""
        if not msg.media or isinstance(msg.media, MessageMediaWebPage):
            return None
        if self._settings.skip_stickers and (msg.sticker or msg.voice or msg.video_note):
            return None

        media_file = msg.file
        if media_file is None:
            return None

        raw_name = media_file.name or ""
        ext = (Path(raw_name).suffix or media_file.ext or "").lower()
        if ext not in ALLOWED_EXTENSIONS:
            return None

        filename = safe_filename(raw_name, fallback=f"{msg.id}{ext}")
        if not Path(filename).suffix:
            filename = f"{filename}{ext}"

        return MediaItem(
            message_id=msg.id,
            filename=filename,
            size=int(media_file.size or 0),
            ext=ext,
            kind=kind_for_extension(ext),
            mime_type=media_file.mime_type,
            date=msg.date.isoformat() if msg.date else None,
        )

    @staticmethod
    def _matches_filter(item: MediaItem, type_filter: str) -> bool:
        if type_filter in ("", "all"):
            return True
        allowed = KIND_EXTENSIONS.get(type_filter)
        return bool(allowed and item.ext in allowed)

    async def list_media_page(
        self,
        chat_id: int,
        *,
        offset_id: int = 0,
        limit: int | None = None,
        type_filter: str = "all",
    ) -> dict:
        """One page of media, newest first.

        Scans at most ``scan_batch`` raw messages so a chat with 200k text
        messages can't stall the request; ``next_offset_id`` walks backwards
        through history exactly the way Telethon's ``offset_id`` expects.
        """
        limit = limit or self._settings.page_size
        entity = await self.resolve_entity(chat_id)

        items: list[MediaItem] = []
        scanned = 0
        last_id = offset_id
        exhausted = True

        async for msg in self._client.iter_messages(
            entity, offset_id=offset_id or 0, limit=self._settings.scan_batch
        ):
            scanned += 1
            last_id = msg.id
            item = self._to_media_item(msg)
            if item and self._matches_filter(item, type_filter):
                items.append(item)
            if len(items) >= limit:
                exhausted = False
                break
        else:
            # Loop finished without break: more history remains only if we hit
            # the scan cap rather than the start of the chat.
            exhausted = scanned < self._settings.scan_batch

        return {
            "items": [i.to_dict() for i in items],
            "next_offset_id": last_id,
            "has_more": not exhausted,
            "scanned": scanned,
        }

    async def iter_all_media(
        self,
        chat_id: int,
        *,
        on_scan: Callable[[int, int], None] | None = None,
    ) -> AsyncIterator[MediaItem]:
        """Walk the entire history of a chat, yielding every migratable file."""
        entity = await self.resolve_entity(chat_id)
        scanned = 0
        found = 0
        async for msg in self._client.iter_messages(entity):
            scanned += 1
            item = self._to_media_item(msg)
            if item:
                found += 1
                yield item
            if on_scan and scanned % 500 == 0:
                on_scan(scanned, found)

    async def get_media_items(self, chat_id: int, message_ids: Iterable[int]) -> list[MediaItem]:
        """Fetch specific messages (batched, Telegram caps ids at 100/request)."""
        entity = await self.resolve_entity(chat_id)
        ids = list(message_ids)
        out: list[MediaItem] = []
        for start in range(0, len(ids), 100):
            batch = ids[start : start + 100]
            messages = await self._client.get_messages(entity, ids=batch)
            for msg in messages:
                if msg is None:
                    continue
                item = self._to_media_item(msg)
                if item:
                    out.append(item)
        return out

    # ------------------------------------------------------------- download --

    async def download(
        self,
        chat_id: int,
        message_id: int,
        dest: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        entity = await self.resolve_entity(chat_id)
        messages = await self._client.get_messages(entity, ids=[message_id])
        msg = messages[0] if messages else None
        if msg is None or not msg.media:
            raise FileNotFoundError(f"Message {message_id} no longer has media (deleted?).")

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            path = await self._client.download_media(
                msg, file=str(dest), progress_callback=progress
            )
        except FloodWaitError as exc:
            # Telethon auto-sleeps under flood_sleep_threshold; anything longer
            # is re-raised so the operator sees it in the live log.
            raise RuntimeError(
                f"Telegram rate limit: wait {exc.seconds}s before retrying."
            ) from exc

        if not path:
            raise RuntimeError("Download returned no file.")
        return Path(path)

    async def sleep_between_files(self) -> None:
        """Small courtesy delay; keeps long bulk runs well under flood limits."""
        await asyncio.sleep(0.35)
