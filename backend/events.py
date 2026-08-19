"""Fan-out hub for live log / progress events over WebSocket.

Keeps a small ring buffer so a client that connects (or reconnects) mid-run
immediately sees recent history instead of an empty console.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import WebSocket

log = logging.getLogger(__name__)

Level = Literal["info", "success", "warning", "error"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventHub:
    def __init__(self, history_size: int = 300) -> None:
        self._clients: set[WebSocket] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle --

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        log.debug("WS client connected (%d total)", len(self._clients))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        log.debug("WS client disconnected (%d left)", len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    # ----------------------------------------------------------- publishing --

    async def broadcast(self, payload: dict[str, Any], *, remember: bool = False) -> None:
        payload.setdefault("ts", _now())
        if remember:
            self._history.append(payload)

        async with self._lock:
            targets = list(self._clients)

        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # client vanished mid-send
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def log(self, message: str, level: Level = "info", **extra: Any) -> None:
        log.info("[%s] %s", level, message)
        await self.broadcast(
            {"type": "log", "level": level, "message": message, **extra}, remember=True
        )

    async def progress(self, snapshot: dict[str, Any]) -> None:
        await self.broadcast({"type": "progress", **snapshot})

    async def file_progress(self, message_id: int, name: str, percent: float, phase: str) -> None:
        await self.broadcast(
            {
                "type": "file_progress",
                "message_id": message_id,
                "name": name,
                "percent": round(percent, 1),
                "phase": phase,
            }
        )
