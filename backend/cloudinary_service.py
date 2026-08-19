"""Cloudinary uploads, scoped to one account.

Two things drive this file's shape:

* The SDK is synchronous, so every call goes through ``asyncio.to_thread`` —
  otherwise a multi-GB ``upload_large`` blocks the event loop and freezes the
  WebSocket log, the pause gate, and every other request.
* ``cloudinary.config()`` is **process-global**. With one Cloudinary target per
  Telegram account, calling it would mean account B's upload could land in
  account A's cloud depending on timing. Credentials are therefore passed
  explicitly on every call and never written to global config.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import cloudinary.api
import cloudinary.uploader

from config import Settings, resource_type_for_extension

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CloudinaryCredentials:
    cloud_name: str
    api_key: str
    api_secret: str
    folder: str = "telegram_migration"
    folder_per_chat: bool = True

    def as_options(self) -> dict[str, str]:
        return {
            "cloud_name": self.cloud_name,
            "api_key": self.api_key,
            "api_secret": self.api_secret,
        }


@dataclass(frozen=True, slots=True)
class UploadResult:
    public_id: str
    secure_url: str
    resource_type: str
    bytes: int
    existing: bool


class CloudinaryService:
    def __init__(self, settings: Settings, credentials: CloudinaryCredentials) -> None:
        self._settings = settings
        self._credentials = credentials

    @property
    def credentials(self) -> CloudinaryCredentials:
        return self._credentials

    @property
    def cloud_name(self) -> str:
        return self._credentials.cloud_name

    def folder_for(self, chat_slug: str | None) -> str:
        base = self._credentials.folder.strip("/") or "telegram_migration"
        if self._credentials.folder_per_chat and chat_slug:
            return f"{base}/{chat_slug}"
        return base

    # ---------------------------------------------------------------- verify --

    async def verify(self) -> dict:
        """Cheap credential check, used before the UI accepts a config."""
        return await asyncio.to_thread(self._ping_sync)

    def _ping_sync(self) -> dict:
        result = cloudinary.api.ping(**self._credentials.as_options())
        return {"status": result.get("status", "ok")}

    # ---------------------------------------------------------------- upload --

    async def upload(self, path: Path, *, ext: str, folder: str) -> UploadResult:
        resource_type = resource_type_for_extension(ext)
        result = await asyncio.to_thread(self._upload_sync, path, resource_type, folder)
        return UploadResult(
            public_id=result.get("public_id", ""),
            secure_url=result.get("secure_url") or result.get("url", ""),
            resource_type=result.get("resource_type", resource_type),
            bytes=int(result.get("bytes") or path.stat().st_size),
            existing=bool(result.get("existing", False)),
        )

    def _upload_sync(self, path: Path, resource_type: str, folder: str) -> dict:
        # upload_large chunks the transfer, so a 4 GB video is never held in
        # memory or pushed as a single request.
        return cloudinary.uploader.upload_large(
            str(path),
            resource_type=resource_type,
            folder=folder,
            use_filename=True,
            unique_filename=False,
            overwrite=False,
            chunk_size=self._settings.chunk_size,
            timeout=600,
            **self._credentials.as_options(),
        )
