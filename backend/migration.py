"""The migration engine.

Design notes (the parts that are easy to get wrong):

* Pause is a gate, not a kill. The loop awaits ``asyncio.Event`` **between**
  files, so an in-flight download/upload always finishes cleanly and the run
  resumes at the exact next file.
* Stop is cooperative. It is checked every iteration and releases the pause
  gate, so a paused run stops instantly while a running one finishes the file
  it is on rather than orphaning a half-written Cloudinary asset.
* Every success is written to disk immediately, so a server restart, a crash,
  or a fresh "Sync all" never re-uploads what is already there.
* Temporary downloads are deleted in a ``finally`` block, so peak disk usage
  is one file — not the 20 GB of the whole run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from cloudinary_service import CloudinaryService
from config import Settings
from events import EventHub
from state import MigrationStore
from telegram_service import MediaItem, TelegramService, slugify

log = logging.getLogger(__name__)

IDLE = "idle"
SCANNING = "scanning"
RUNNING = "running"
PAUSED = "paused"
STOPPING = "stopping"

ACTIVE_STATES = {SCANNING, RUNNING, PAUSED, STOPPING}


class MigrationBusyError(RuntimeError):
    pass


class MigrationNotRunningError(RuntimeError):
    pass


class CloudinaryNotConfiguredError(RuntimeError):
    pass


class MigrationManager:
    def __init__(
        self,
        telegram: TelegramService,
        cloudinary_provider: Callable[[], CloudinaryService | None],
        store: MigrationStore,
        hub: EventHub,
        settings: Settings,
    ) -> None:
        self._tg = telegram
        # A callable, not an instance: the operator can change their Cloudinary
        # target between runs and the next run must pick up the new one.
        self._cloudinary_provider = cloudinary_provider
        self._cloud: CloudinaryService | None = None
        self._store = store
        self._hub = hub
        self._settings = settings

        self._task: asyncio.Task | None = None
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._stop_requested = False

        self._state = IDLE
        self._chat_id: int | None = None
        self._chat_title: str | None = None
        self._mode: str = "selection"
        self._total = 0
        self._done = 0
        self._failed = 0
        self._skipped = 0
        self._total_bytes = 0
        self._done_bytes = 0
        self._current: dict[str, Any] | None = None
        self._started_at: float | None = None
        self._finished_reason: str | None = None

    # -------------------------------------------------------------- snapshot --

    @property
    def is_active(self) -> bool:
        return self._state in ACTIVE_STATES

    def snapshot(self) -> dict[str, Any]:
        elapsed = round(time.monotonic() - self._started_at, 1) if self._started_at else 0.0
        return {
            "state": self._state,
            "active": self.is_active,
            "mode": self._mode,
            "chat_id": self._chat_id,
            "chat_title": self._chat_title,
            "total": self._total,
            "done": self._done,
            "failed": self._failed,
            "skipped": self._skipped,
            "total_bytes": self._total_bytes,
            "done_bytes": self._done_bytes,
            "current_file": self._current,
            "elapsed_seconds": elapsed,
            "last_result": self._finished_reason,
        }

    async def _publish(self) -> None:
        await self._hub.progress(self.snapshot())

    # -------------------------------------------------------------- controls --

    async def start(self, chat_id: int, message_ids: list[int] | None) -> dict[str, Any]:
        if self.is_active:
            raise MigrationBusyError(
                f"A migration is already {self._state}. Stop it before starting another."
            )

        cloud = self._cloudinary_provider()
        if cloud is None:
            raise CloudinaryNotConfiguredError(
                "Add your Cloudinary credentials before starting a migration."
            )
        self._cloud = cloud

        self._reset(chat_id=chat_id, mode="selection" if message_ids else "sync_all")
        self._task = asyncio.create_task(
            self._run(chat_id, message_ids), name=f"migration-{chat_id}"
        )
        return self.snapshot()

    async def pause(self) -> dict[str, Any]:
        if self._state not in (RUNNING, SCANNING):
            raise MigrationNotRunningError("Nothing is running to pause.")
        self._pause_gate.clear()
        await self._hub.log("Pause requested — finishing the current file first.", "warning")
        return self.snapshot()

    async def resume(self) -> dict[str, Any]:
        if self._state != PAUSED:
            raise MigrationNotRunningError("The migration is not paused.")
        self._pause_gate.set()
        await self._hub.log("Resumed.", "success")
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        if not self.is_active:
            raise MigrationNotRunningError("Nothing is running to stop.")
        self._stop_requested = True
        self._state = STOPPING
        self._pause_gate.set()  # release a paused loop so it can exit now
        await self._hub.log(
            "Stop requested — the current file will finish, then the run ends.", "warning"
        )
        await self._publish()
        return self.snapshot()

    async def shutdown(self) -> None:
        """Called on server shutdown: cancel hard, clean the temp directory."""
        if self._task and not self._task.done():
            self._stop_requested = True
            self._pause_gate.set()
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.cleanup_downloads()

    # ------------------------------------------------------------------ core --

    def _reset(self, *, chat_id: int, mode: str) -> None:
        self._stop_requested = False
        self._pause_gate.set()
        self._state = SCANNING
        self._chat_id = chat_id
        self._chat_title = None
        self._mode = mode
        self._total = self._done = self._failed = self._skipped = 0
        self._total_bytes = self._done_bytes = 0
        self._current = None
        self._started_at = time.monotonic()
        self._finished_reason = None

    async def _build_worklist(
        self, chat_id: int, message_ids: list[int] | None
    ) -> list[MediaItem]:
        already = await self._store.migrated_ids(chat_id)

        if message_ids is not None:
            items = await self._tg.get_media_items(chat_id, message_ids)
            fresh = [i for i in items if i.message_id not in already]
            self._skipped = len(items) - len(fresh)
            missing = len(message_ids) - len(items)
            if missing > 0:
                await self._hub.log(
                    f"{missing} selected message(s) had no matching media and were ignored.",
                    "warning",
                )
        else:
            await self._hub.log("Scanning the full chat history…", "info")
            fresh = []
            scan_log_state = {"last": 0.0}

            def on_scan(scanned: int, found: int) -> None:
                now = time.monotonic()
                if now - scan_log_state["last"] > 3:
                    scan_log_state["last"] = now
                    log.info("Scanned %s messages, %s media found", scanned, found)

            async for item in self._tg.iter_all_media(chat_id, on_scan=on_scan):
                if self._stop_requested:
                    break
                if item.message_id in already:
                    self._skipped += 1
                    continue
                fresh.append(item)

        fresh.sort(key=lambda i: i.message_id)  # oldest first — chronological
        return fresh

    async def _run(self, chat_id: int, message_ids: list[int] | None) -> None:
        try:
            self._chat_title = await self._tg.chat_title(chat_id)
            await self._hub.log(
                f"Starting {'full sync' if message_ids is None else 'selected files'} "
                f"for “{self._chat_title}”.",
                "info",
            )
            await self._publish()

            worklist = await self._build_worklist(chat_id, message_ids)
            self._total = len(worklist)
            self._total_bytes = sum(i.size for i in worklist)

            if self._skipped:
                await self._hub.log(
                    f"Skipping {self._skipped} file(s) already migrated.", "info"
                )
            if not worklist:
                self._finished_reason = "nothing_to_do"
                await self._hub.log("Nothing left to migrate here.", "success")
                return

            assert self._cloud is not None  # guaranteed by start()
            folder = self._cloud.folder_for(slugify(self._chat_title or str(chat_id)))
            await self._hub.log(
                f"{self._total} file(s) queued → Cloudinary folder “{folder}”.", "info"
            )

            self._state = RUNNING
            await self._publish()

            for item in worklist:
                if self._stop_requested:
                    break

                if not self._pause_gate.is_set():
                    self._state = PAUSED
                    self._current = None
                    await self._hub.log(
                        f"Paused at {self._done + self._failed}/{self._total}. "
                        "Resume picks up at the next file.",
                        "warning",
                    )
                    await self._publish()
                    await self._pause_gate.wait()
                    if self._stop_requested:
                        break
                    self._state = RUNNING
                    await self._publish()

                # Defensive: another run (or a restart) may have covered this.
                if await self._store.is_migrated(chat_id, item.message_id):
                    self._skipped += 1
                    self._total = max(self._total - 1, self._done + self._failed)
                    continue

                await self._migrate_one(chat_id, item, folder)
                await self._tg.sleep_between_files()

            if self._stop_requested:
                self._finished_reason = "stopped"
                await self._hub.log(
                    f"Stopped. {self._done} migrated, {self._failed} failed, "
                    f"{self._total - self._done - self._failed} left for next time.",
                    "warning",
                )
            else:
                self._finished_reason = "completed"
                level = "success" if not self._failed else "warning"
                await self._hub.log(
                    f"Finished “{self._chat_title}” — {self._done} migrated, "
                    f"{self._failed} failed, {self._skipped} skipped.",
                    level,
                )

        except asyncio.CancelledError:
            self._finished_reason = "cancelled"
            log.warning("Migration task cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            self._finished_reason = "error"
            log.exception("Migration failed")
            await self._hub.log(f"Migration aborted: {exc}", "error")
        finally:
            self._state = IDLE
            self._current = None
            self._pause_gate.set()
            self._stop_requested = False
            self.cleanup_downloads()
            with contextlib.suppress(Exception):
                await self._publish()

    async def _migrate_one(self, chat_id: int, item: MediaItem, folder: str) -> None:
        index = self._done + self._failed + 1
        self._current = {
            "message_id": item.message_id,
            "name": item.filename,
            "size": item.size,
            "kind": item.kind,
            "index": index,
        }
        await self._publish()

        # Each file gets its own scratch directory so the local name stays
        # exactly the Telegram filename — Cloudinary's use_filename=True turns
        # that name into the public_id, and a "chatid_msgid_" prefix would end
        # up baked into every asset URL.
        temp_dir = self._settings.download_dir / f"{chat_id}_{item.message_id}"
        temp_path = temp_dir / item.filename
        started = time.monotonic()

        try:
            await self._hub.log(
                f"[{index}/{self._total}] Downloading {item.filename} "
                f"({_human_size(item.size)})",
                "info",
            )
            await self._tg.download(
                chat_id,
                item.message_id,
                temp_path,
                progress=self._download_progress(item),
            )

            await self._hub.log(f"Uploading {item.filename} → Cloudinary…", "info")
            assert self._cloud is not None
            result = await self._cloud.upload(temp_path, ext=item.ext, folder=folder)

            await self._store.mark_migrated(
                chat_id,
                item.message_id,
                filename=item.filename,
                public_id=result.public_id,
                secure_url=result.secure_url,
                resource_type=result.resource_type,
                bytes_uploaded=result.bytes,
            )

            self._done += 1
            self._done_bytes += item.size
            elapsed = time.monotonic() - started
            await self._hub.log(
                f"Migrated {item.filename} as {result.resource_type}/{result.public_id} "
                f"in {elapsed:.1f}s",
                "success",
                message_id=item.message_id,
                url=result.secure_url,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._failed += 1
            self._done_bytes += item.size
            await self._hub.log(
                f"Failed {item.filename}: {type(exc).__name__}: {exc}",
                "error",
                message_id=item.message_id,
            )
        finally:
            _remove_tree(temp_dir)
            self._current = None
            await self._publish()

    def _download_progress(self, item: MediaItem):
        """Throttled byte-level progress for the current file."""
        loop = asyncio.get_running_loop()
        last = {"pct": -10.0, "at": 0.0}

        def callback(received: int, total: int) -> None:
            if not total:
                return
            pct = received * 100 / total
            now = loop.time()
            if now - last["at"] < 0.4:
                return
            if pct - last["pct"] < 5 and pct < 100:
                return
            last["pct"] = pct
            last["at"] = now
            loop.create_task(
                self._hub.file_progress(item.message_id, item.filename, pct, "download")
            )

        return callback

    # ------------------------------------------------------------- housekeeping --

    def cleanup_downloads(self) -> None:
        """Drop every scratch file — run at startup, shutdown and end of run."""
        directory = self._settings.download_dir
        if not directory.exists():
            return
        for leftover in directory.iterdir():
            _remove_tree(leftover)


def _remove_tree(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def unique_ints(values: Iterable[int]) -> list[int]:
    seen: dict[int, None] = {}
    for value in values:
        seen.setdefault(int(value), None)
    return list(seen)
