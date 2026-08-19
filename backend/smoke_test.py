"""Offline smoke test — no Telegram, no Cloudinary, no real credentials.

    cd backend && pip install httpx && python smoke_test.py

Stubs the Telegram client and the Cloudinary SDK, then drives the real
FastAPI app end to end: passcode lockout, OTP sign-in with 2FA, per-account
Cloudinary setup, migration, resume-skipping, pause → resume → stop, account
switching, and WebSocket authorisation.

Run it after touching auth or the migration loop. It takes ~10 seconds and is
a far cheaper way to find a broken pause gate than 20 GB of real transfers.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="migrator_smoke_"))
PASSCODE = "correct-horse-battery"

os.environ.update(
    TG_API_ID="1",
    TG_API_HASH="hash",
    DASHBOARD_PASSCODE=PASSCODE,
    DB_PATH=str(TMP / "migrator.db"),
    DOWNLOAD_DIR=str(TMP / "downloads"),
    MAX_PASSCODE_ATTEMPTS="3",
    LOCKOUT_MINUTES="1",
)

from telethon.errors import SessionPasswordNeededError  # noqa: E402

import auth  # noqa: E402
import cloudinary_service  # noqa: E402
import runtime as runtime_mod  # noqa: E402
from telegram_service import DialogInfo, MediaItem  # noqa: E402

CHAT_ID = -1001234567890
FILES = [
    MediaItem(
        message_id=100 + i,
        filename=f"file_{i}.pdf" if i % 2 else f"clip_{i}.mp4",
        size=1024 * (i + 1),
        ext=".pdf" if i % 2 else ".mp4",
        kind="pdf" if i % 2 else "video",
        mime_type="application/pdf" if i % 2 else "video/mp4",
        date=f"2026-01-0{i + 1}T10:00:00+00:00",
    )
    for i in range(6)
]

ACCOUNTS = {
    "+911234567890": {"id": 555001, "name": "Primary Operator", "username": "primary",
                      "twofa": False},
    "+919999900000": {"id": 555002, "name": "Second Account", "username": "second",
                      "twofa": True},
}
GOOD_CODE = "12345"
GOOD_PASSWORD = "hunter2-but-longer"
DOWNLOAD_DELAY = 0.25


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #


class FakePasswordInfo:
    hint = "the usual one"


class FakeTelegramClient:
    """Stands in for Telethon during the OTP flow."""

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.phone = None
        self._connected = False
        self._authorized = False
        self.logged_out = False

    async def connect(self):
        self._connected = True

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self._connected = False

    async def send_code_request(self, phone):
        if phone not in ACCOUNTS:
            raise Exception("unknown test phone")
        self.phone = phone
        return type("Sent", (), {"phone_code_hash": f"hash-for-{phone}"})()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != GOOD_PASSWORD:
                raise Exception("PasswordHashInvalidError")
            self._authorized = True
            return True
        if code != GOOD_CODE:
            from telethon.errors import PhoneCodeInvalidError

            raise PhoneCodeInvalidError(request=None)
        if ACCOUNTS[self.phone]["twofa"]:
            raise SessionPasswordNeededError(request=None)
        self._authorized = True
        return True

    async def __call__(self, request):
        return FakePasswordInfo()


class StubTelegramService:
    """Stands in for TelegramService once an account is live."""

    def __init__(self, settings, session_string):
        self._settings = settings
        self.phone = session_string.replace("session-for-", "")
        self.connected = True

    @classmethod
    def adopt(cls, settings, client):
        return cls(settings, f"session-for-{client.phone}")

    @property
    def account_name(self):
        return ACCOUNTS[self.phone]["name"]

    def session_string(self):
        return f"session-for-{self.phone}"

    async def start(self):
        self.connected = True

    async def stop(self):
        self.connected = False

    async def sign_out(self):
        return True

    async def whoami(self):
        meta = ACCOUNTS[self.phone]
        return {
            "tg_user_id": meta["id"],
            "display_name": meta["name"],
            "username": meta["username"],
            "phone": self.phone,
        }

    async def list_dialogs(self, include_dms=None):
        return [DialogInfo(id=CHAT_ID, title="Archive Channel", kind="channel",
                           username="archive", unread=0)]

    async def chat_title(self, chat_id):
        return "Archive Channel"

    async def list_media_page(self, chat_id, *, offset_id=0, limit=60, type_filter="all"):
        items = [f for f in FILES if type_filter in ("all", f.kind)]
        return {
            "items": [i.to_dict() for i in items],
            "next_offset_id": items[-1].message_id if items else offset_id,
            "has_more": False,
            "scanned": len(FILES),
        }

    async def get_media_items(self, chat_id, message_ids):
        wanted = set(message_ids)
        return [f for f in FILES if f.message_id in wanted]

    async def iter_all_media(self, chat_id, *, on_scan=None):
        for item in FILES:
            yield item

    async def download(self, chat_id, message_id, dest, *, progress=None):
        await asyncio.sleep(DOWNLOAD_DELAY)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 64)
        if progress:
            progress(64, 64)
        return dest

    async def sleep_between_files(self):
        await asyncio.sleep(0)


class StubCloudinaryService:
    def __init__(self, settings, credentials):
        self._settings = settings
        self._credentials = credentials
        self.uploaded: list[str] = []

    @property
    def credentials(self):
        return self._credentials

    @property
    def cloud_name(self):
        return self._credentials.cloud_name

    async def verify(self):
        if self._credentials.api_secret == "wrong-secret":
            raise RuntimeError("Invalid Signature")
        return {"status": "ok"}

    def folder_for(self, slug):
        base = self._credentials.folder
        return f"{base}/{slug}" if self._credentials.folder_per_chat and slug else base

    async def upload(self, path, *, ext, folder):
        await asyncio.sleep(0.05)
        assert path.exists(), "the file must exist when the upload starts"
        self.uploaded.append(path.name)
        return cloudinary_service.UploadResult(
            public_id=f"{folder}/{path.stem}",
            secure_url=f"https://res.cloudinary.com/demo/{path.name}",
            resource_type="raw" if ext == ".pdf" else "video",
            bytes=path.stat().st_size,
            existing=False,
        )


auth.TelegramClient = FakeTelegramClient  # type: ignore[assignment]
runtime_mod.TelegramService = StubTelegramService  # type: ignore[assignment]
runtime_mod.CloudinaryService = StubCloudinaryService  # type: ignore[assignment]

import main  # noqa: E402

main.TelegramService = StubTelegramService  # type: ignore[assignment]
main.CloudinaryService = StubCloudinaryService  # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        sys.exit(1)
    PASSED += 1


def wait_for(client, predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    snapshot = {}
    while time.monotonic() < deadline:
        snapshot = client.get("/api/migrate/status").json()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting; last status = {snapshot}")


def sign_in(client, phone, *, code=GOOD_CODE, password=GOOD_PASSWORD):
    sent = client.post("/api/auth/telegram/send-code", json={"phone": phone}).json()
    verified = client.post(
        "/api/auth/telegram/verify-code", json={"login_id": sent["login_id"], "code": code}
    ).json()
    if verified.get("needs_password"):
        verified = client.post(
            "/api/auth/telegram/password",
            json={"login_id": sent["login_id"], "password": password},
        ).json()
    return sent, verified


# --------------------------------------------------------------------------- #
# Test run
# --------------------------------------------------------------------------- #


def run() -> None:
    with TestClient(main.app) as client:
        print("\n1. Everything is locked before the passcode")
        check("session probe says locked", client.get("/api/auth/session").json()["unlocked"] is False)
        check("channels blocked with 401", client.get("/api/channels").status_code == 401)
        check("migrate blocked with 401",
              client.post("/api/migrate/start",
                          json={"chat_id": CHAT_ID, "message_ids": [1]}).status_code == 401)

        print("\n2. Passcode gate")
        bad = client.post("/api/auth/unlock", json={"passcode": "nope"})
        check("wrong passcode → 401", bad.status_code == 401, bad.json()["detail"])
        client.post("/api/auth/unlock", json={"passcode": "nope"})
        locked = client.post("/api/auth/unlock", json={"passcode": "nope"})
        check("third failure locks out → 429", locked.status_code == 429, locked.json()["code"])
        blocked = client.post("/api/auth/unlock", json={"passcode": PASSCODE})
        check("correct passcode also refused while locked", blocked.status_code == 429)

        main.app.state.ctx.gate._locked_until = 0  # fast-forward the lockout
        unlocked = client.post("/api/auth/unlock", json={"passcode": PASSCODE})
        check("correct passcode unlocks", unlocked.status_code == 200)
        check("cookie issued", "migrator_session" in client.cookies)

        print("\n3. Telegram OTP sign-in")
        check("unlocked but signed out → 428", client.get("/api/channels").status_code == 428)

        sent = client.post("/api/auth/telegram/send-code",
                           json={"phone": "+911234567890"}).json()
        check("code sent, phone masked in the response",
              "login_id" in sent and "•" in sent["phone"], sent["phone"])

        wrong = client.post("/api/auth/telegram/verify-code",
                            json={"login_id": sent["login_id"], "code": "00000"})
        check("wrong OTP → 400", wrong.status_code == 400, wrong.json()["code"])

        ok = client.post("/api/auth/telegram/verify-code",
                         json={"login_id": sent["login_id"], "code": GOOD_CODE}).json()
        check("correct OTP signs in", ok["signed_in"] is True, ok["account"]["display_name"])

        session = client.get("/api/auth/session").json()
        check("session now carries the account", session["account"]["id"] == 1)
        check("session never leaks a session string",
              "session_string" not in str(session))

        print("\n4. Cloudinary is required before migrating")
        blocked = client.post("/api/migrate/start",
                              json={"chat_id": CHAT_ID, "message_ids": [100]})
        check("no Cloudinary → 428", blocked.status_code == 428, blocked.json()["code"])

        rejected = client.put("/api/account/cloudinary", json={
            "cloud_name": "demo", "api_key": "k", "api_secret": "wrong-secret",
            "folder": "telegram_migration", "folder_per_chat": True})
        check("bad credentials rejected before saving", rejected.status_code == 400)

        saved = client.put("/api/account/cloudinary", json={
            "cloud_name": "demo-cloud", "api_key": "key123", "api_secret": "secret123",
            "folder": "telegram_migration", "folder_per_chat": True})
        check("good credentials accepted", saved.status_code == 200)
        check("api_secret never returned",
              saved.json()["cloudinary"]["api_secret_set"] is True
              and "secret123" not in saved.text)

        print("\n5. Listing and selected-file migration")
        files = client.get(f"/api/channels/{CHAT_ID}/files").json()
        check("six files listed", len(files["items"]) == 6)
        check("type filter narrows the list",
              len(client.get(f"/api/channels/{CHAT_ID}/files?type_filter=pdf").json()["items"]) == 3)

        ids = [f.message_id for f in FILES[:3]]
        started = client.post("/api/migrate/start",
                              json={"chat_id": CHAT_ID, "message_ids": ids})
        check("start accepted", started.status_code == 200)
        check("second start → 409",
              client.post("/api/migrate/start",
                          json={"chat_id": CHAT_ID, "message_ids": ids}).status_code == 409)

        final = wait_for(client, lambda s: s["state"] == "idle")
        check("three files migrated", final["done"] == 3 and final["failed"] == 0)
        check("temp downloads cleaned up",
              not list(Path(os.environ["DOWNLOAD_DIR"]).glob("*")))
        check("badges reflected in listing",
              sum(i["migrated"] for i in
                  client.get(f"/api/channels/{CHAT_ID}/files").json()["items"]) == 3)

        print("\n6. Re-running skips what is done")
        client.post("/api/migrate/start", json={"chat_id": CHAT_ID, "message_ids": ids})
        repeat = wait_for(client, lambda s: s["state"] == "idle")
        check("nothing re-uploaded", repeat["done"] == 0 and repeat["skipped"] == 3)

        print("\n7. Sync all with pause → resume → stop")
        client.post("/api/migrate/start", json={"chat_id": CHAT_ID, "message_ids": None})
        wait_for(client, lambda s: s["state"] == "running")
        check("pause accepted", client.post("/api/migrate/pause").status_code == 200)
        paused = wait_for(client, lambda s: s["state"] == "paused")
        check("paused between files, not mid-file", paused["current_file"] is None)
        before = paused["done"]
        time.sleep(0.4)
        check("nothing progresses while paused",
              client.get("/api/migrate/status").json()["done"] == before)
        client.post("/api/migrate/resume")
        wait_for(client, lambda s: s["state"] in ("running", "idle"))
        client.post("/api/migrate/stop")
        stopped = wait_for(client, lambda s: s["state"] == "idle")
        check("run ended after stop", stopped["last_result"] in ("stopped", "completed"))
        check("progress preserved", stopped["done"] >= before)

        print("\n8. WebSocket is account-scoped")
        with client.websocket_connect("/ws/logs") as ws:
            first = ws.receive_json()
            second = ws.receive_json()
            check("history replayed on connect", first["type"] == "history")
            check("status snapshot on connect", second["type"] == "progress")
            check("history holds earlier log lines", len(first["events"]) > 0,
                  f"{len(first['events'])} events")

        print("\n9. Second account with 2FA, isolated state")
        sent2, verified2 = sign_in(client, "+919999900000")
        check("2FA account signs in after password", verified2["signed_in"] is True)
        check("switched to the new account",
              client.get("/api/auth/session").json()["account"]["id"] == 2)
        check("new account has its own empty history",
              sum(i["migrated"] for i in
                  client.get(f"/api/channels/{CHAT_ID}/files").json()["items"]) == 0)
        check("new account needs its own Cloudinary",
              client.post("/api/migrate/start",
                          json={"chat_id": CHAT_ID, "message_ids": [100]}).status_code == 428)

        accounts = client.get("/api/accounts").json()
        check("both accounts listed", len(accounts["accounts"]) == 2)

        client.post("/api/accounts/1/activate")
        check("switching back restores history",
              sum(i["migrated"] for i in
                  client.get(f"/api/channels/{CHAT_ID}/files").json()["items"]) >= 3)

        print("\n10. Sign-out and locking")
        removed = client.delete("/api/accounts/2")
        check("account removed and revoked on Telegram",
              removed.json()["removed"] and removed.json()["revoked_on_telegram"])
        check("one account left", len(client.get("/api/accounts").json()["accounts"]) == 1)

        client.post("/api/auth/lock")
        check("locking clears the session", client.get("/api/channels").status_code == 401)
        try:
            with client.websocket_connect("/ws/logs") as ws:
                ws.receive_json()
            rejected_ws = False
        except Exception:
            rejected_ws = True
        check("WebSocket refuses a locked session", rejected_ws)

    print(f"\nAll {PASSED} checks passed. Scratch dir: {TMP}\n")


if __name__ == "__main__":
    run()
