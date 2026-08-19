"""Telegram → Cloudinary migration dashboard — FastAPI application.

Run it:  uvicorn main:app --host 0.0.0.0 --port 8000
Open it: the forwarded port 8000 in Codespaces (or http://localhost:8000).

Request flow: passcode → session cookie → Telegram OTP sign-in → account
runtime → migration. Everything under /api (except the unlock and session
probe) requires an unlocked session; everything under /api/channels and
/api/migrate additionally requires a signed-in Telegram account.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import AuthError, PasscodeGate, SessionService, TelegramLoginFlow
from cloudinary_service import CloudinaryCredentials, CloudinaryService
from config import FRONTEND_DIR, ConfigError, Settings, load_settings
from db import Database
from migration import (
    CloudinaryNotConfiguredError,
    MigrationBusyError,
    MigrationNotRunningError,
    unique_ints,
)
from runtime import AccountRuntime, RuntimeRegistry
from telegram_service import SessionExpiredError, TelegramService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrator")

TypeFilter = Literal["all", "pdf", "video", "html", "image"]
SWEEP_INTERVAL_SECONDS = 300


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


class AppContext:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.settings.db_path)
        self.db.connect()

        self.sessions = SessionService(self.db, self.settings)
        self.gate = PasscodeGate(self.settings)
        self.login = TelegramLoginFlow(self.settings)
        self.runtimes = RuntimeRegistry(self.db, self.settings)


async def _housekeeping(ctx: AppContext) -> None:
    """Expire stale logins and dead cookies in the background."""
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await ctx.login.sweep()
            purged = await ctx.db.purge_expired_sessions()
            if purged:
                log.info("Purged %d expired dashboard session(s).", purged)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Housekeeping pass failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ctx = AppContext()
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc

    app.state.ctx = ctx
    _sweep_downloads(ctx)  # clear anything a previous crash left behind
    sweeper = asyncio.create_task(_housekeeping(ctx), name="housekeeping")
    log.info("Dashboard ready on http://0.0.0.0:8000 — unlock with your passcode.")
    try:
        yield
    finally:
        sweeper.cancel()
        await ctx.login.shutdown()
        await ctx.runtimes.shutdown()
        _sweep_downloads(ctx)
        ctx.db.close()


def _sweep_downloads(ctx: AppContext) -> None:
    import shutil

    directory = ctx.settings.download_dir
    if not directory.exists():
        return
    for leftover in directory.iterdir():
        try:
            shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
        except OSError:
            pass


app = FastAPI(
    title="Telegram → Cloudinary Migrator",
    description="Multi-account tool for bulk-moving Telegram media into Cloudinary.",
    version="2.0.0",
    lifespan=lifespan,
)


def ctx_of(request: Request | WebSocket) -> AppContext:
    return request.app.state.ctx


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


class Caller:
    """Who is asking: their session token, row, and (maybe) active runtime."""

    def __init__(self, ctx: AppContext, token: str, session: dict[str, Any]) -> None:
        self.ctx = ctx
        self.token = token
        self.session = session
        self.account_id: int | None = session.get("account_id")


async def require_unlocked(
    request: Request,
    migrator_session: Annotated[str | None, Cookie(alias="migrator_session")] = None,
) -> Caller:
    ctx = ctx_of(request)
    session = await ctx.sessions.get(migrator_session)
    if session is None:
        raise HTTPException(status_code=401, detail="Locked. Enter the dashboard passcode.")
    return Caller(ctx, migrator_session or "", session)


async def require_account(
    caller: Annotated[Caller, Depends(require_unlocked)],
) -> tuple[Caller, AccountRuntime]:
    if caller.account_id is None:
        raise HTTPException(status_code=428, detail="Sign in to a Telegram account first.")
    try:
        runtime = await caller.ctx.runtimes.get(caller.account_id)
    except SessionExpiredError as exc:
        await caller.ctx.sessions.bind_account(caller.token, None)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except KeyError as exc:
        await caller.ctx.sessions.bind_account(caller.token, None)
        raise HTTPException(status_code=428, detail="That account was removed.") from exc
    return caller, runtime


CallerDep = Annotated[Caller, Depends(require_unlocked)]
AccountDep = Annotated[tuple[Caller, AccountRuntime], Depends(require_account)]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class UnlockRequest(BaseModel):
    passcode: str = Field(..., min_length=1, max_length=256)


class PhoneRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=32)


class CodeRequest(BaseModel):
    login_id: str
    code: str = Field(..., min_length=3, max_length=16)


class PasswordRequest(BaseModel):
    login_id: str
    password: str = Field(..., min_length=1, max_length=256)


class CloudinaryRequest(BaseModel):
    cloud_name: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field(..., min_length=1, max_length=128)
    api_secret: str = Field(..., min_length=1, max_length=256)
    folder: str = Field(default="telegram_migration", max_length=200)
    folder_per_chat: bool = True


class StartRequest(BaseModel):
    chat_id: int
    message_ids: list[int] | None = None


# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #


@app.exception_handler(AuthError)
async def _auth_handler(_: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), "code": exc.code})


@app.exception_handler(MigrationBusyError)
async def _busy_handler(_: Request, exc: MigrationBusyError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(MigrationNotRunningError)
async def _not_running_handler(_: Request, exc: MigrationNotRunningError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(CloudinaryNotConfiguredError)
async def _no_cloud_handler(_: Request, exc: CloudinaryNotConfiguredError) -> JSONResponse:
    return JSONResponse(status_code=428, content={"detail": str(exc), "code": "no_cloudinary"})


# --------------------------------------------------------------------------- #
# Auth: passcode gate
# --------------------------------------------------------------------------- #


@app.get("/api/auth/session")
async def auth_session(
    request: Request,
    migrator_session: Annotated[str | None, Cookie(alias="migrator_session")] = None,
) -> dict:
    """Unauthenticated probe the frontend calls on load to decide what to show."""
    ctx = ctx_of(request)
    session = await ctx.sessions.get(migrator_session)
    if session is None:
        return {"unlocked": False, "locked_seconds": ctx.gate.locked_seconds}

    account_id = session.get("account_id")
    account = await ctx.db.get_account(account_id) if account_id else None
    payload: dict[str, Any] = {
        "unlocked": True,
        "account": None,
        "accounts": await ctx.db.list_accounts(),
        "cloudinary_defaults": {
            "cloud_name": ctx.settings.default_cloud_name,
            "api_key": ctx.settings.default_api_key,
            "folder": ctx.settings.default_folder,
            "folder_per_chat": ctx.settings.folder_per_chat,
        },
    }
    if account:
        cloud = await ctx.db.get_cloudinary(account["id"])
        payload["account"] = {
            "id": account["id"],
            "display_name": account["display_name"],
            "username": account["username"],
            "tg_user_id": account["tg_user_id"],
            "migrated_total": await ctx.db.total_count(account["id"]),
            "cloudinary": _mask_cloudinary(cloud),
        }
    return payload


@app.post("/api/auth/unlock")
async def auth_unlock(request: Request, body: UnlockRequest, response: Response) -> dict:
    ctx = ctx_of(request)
    await ctx.gate.verify(body.passcode)
    token = await ctx.sessions.create(request.headers.get("user-agent"))
    response.set_cookie(
        ctx.settings.cookie_name,
        token,
        max_age=ctx.settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    log.info("Dashboard unlocked.")
    return {"unlocked": True, "accounts": await ctx.db.list_accounts()}


@app.post("/api/auth/lock")
async def auth_lock(request: Request, caller: CallerDep, response: Response) -> dict:
    await caller.ctx.sessions.destroy(caller.token)
    response.delete_cookie(caller.ctx.settings.cookie_name, path="/")
    return {"unlocked": False}


# --------------------------------------------------------------------------- #
# Auth: Telegram OTP
# --------------------------------------------------------------------------- #


@app.post("/api/auth/telegram/send-code")
async def telegram_send_code(caller: CallerDep, body: PhoneRequest) -> dict:
    return await caller.ctx.login.send_code(body.phone)


@app.post("/api/auth/telegram/verify-code")
async def telegram_verify_code(caller: CallerDep, body: CodeRequest) -> dict:
    ctx = caller.ctx
    pending, signed_in = await ctx.login.verify_code(body.login_id, body.code)
    if not signed_in:
        return {
            "signed_in": False,
            "needs_password": True,
            "hint": pending.hint,
            "login_id": pending.login_id,
        }
    return await _complete_login(caller, body.login_id)


@app.post("/api/auth/telegram/password")
async def telegram_password(caller: CallerDep, body: PasswordRequest) -> dict:
    await caller.ctx.login.submit_password(body.login_id, body.password)
    return await _complete_login(caller, body.login_id)


async def _complete_login(caller: Caller, login_id: str) -> dict:
    """Persist the new session string and start the account's runtime."""
    ctx = caller.ctx
    pending = await ctx.login.take(login_id)
    service = TelegramService.adopt(ctx.settings, pending.client)

    try:
        await service.start()
        me = await service.whoami()
    except Exception as exc:  # noqa: BLE001
        await service.stop()
        raise AuthError(f"Signed in, but the connection failed: {exc}", status=502) from exc

    account = await ctx.db.upsert_account(
        tg_user_id=me["tg_user_id"],
        phone=me["phone"] or pending.phone,
        display_name=me["display_name"],
        username=me["username"],
        session_string=service.session_string(),
    )
    runtime = await ctx.runtimes.adopt(account, service)
    await ctx.sessions.bind_account(caller.token, runtime.account_id)

    cloud = await ctx.db.get_cloudinary(runtime.account_id)
    log.info("Account %s signed in.", account["display_name"])
    return {
        "signed_in": True,
        "account": {
            "id": account["id"],
            "display_name": account["display_name"],
            "username": account["username"],
            "tg_user_id": account["tg_user_id"],
            "migrated_total": await ctx.db.total_count(account["id"]),
            "cloudinary": _mask_cloudinary(cloud),
        },
    }


@app.post("/api/auth/telegram/cancel")
async def telegram_cancel(caller: CallerDep, body: dict) -> dict:
    await caller.ctx.login.discard(str(body.get("login_id", "")))
    return {"cancelled": True}


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


@app.get("/api/accounts")
async def list_accounts(caller: CallerDep) -> dict:
    return {
        "accounts": await caller.ctx.db.list_accounts(),
        "active_account_id": caller.account_id,
    }


@app.post("/api/accounts/{account_id}/activate")
async def activate_account(caller: CallerDep, account_id: int) -> dict:
    ctx = caller.ctx
    if await ctx.db.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="No such account.")
    try:
        runtime = await ctx.runtimes.get(account_id)
    except SessionExpiredError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    await ctx.sessions.bind_account(caller.token, account_id)
    cloud = await ctx.db.get_cloudinary(account_id)
    return {
        "account": {
            "id": runtime.account_id,
            "display_name": runtime.account["display_name"],
            "username": runtime.account["username"],
            "tg_user_id": runtime.account["tg_user_id"],
            "migrated_total": await ctx.db.total_count(account_id),
            "cloudinary": _mask_cloudinary(cloud),
        }
    }


@app.delete("/api/accounts/{account_id}")
async def remove_account(
    caller: CallerDep,
    account_id: int,
    revoke: Annotated[bool, Query(description="Also revoke the session on Telegram")] = True,
) -> dict:
    """Sign an account out. Its migration history goes with it (cascade)."""
    ctx = caller.ctx
    runtime = ctx.runtimes.peek(account_id)
    if runtime and runtime.migrator.is_active:
        raise HTTPException(status_code=409, detail="Stop the running migration first.")

    revoked = False
    if runtime and revoke:
        revoked = await runtime.telegram.sign_out()
    await ctx.runtimes.drop(account_id)
    await ctx.db.delete_account(account_id)
    if caller.account_id == account_id:
        await ctx.sessions.bind_account(caller.token, None)
    return {"removed": True, "revoked_on_telegram": revoked}


# --------------------------------------------------------------------------- #
# Cloudinary settings (per account)
# --------------------------------------------------------------------------- #


def _mask_cloudinary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "cloud_name": row["cloud_name"],
        "api_key": row["api_key"],
        "api_secret_set": bool(row["api_secret"]),
        "folder": row["folder"],
        "folder_per_chat": bool(row["folder_per_chat"]),
        "updated_at": row["updated_at"],
    }


@app.get("/api/account/cloudinary")
async def get_cloudinary(account: AccountDep) -> dict:
    caller, runtime = account
    row = await caller.ctx.db.get_cloudinary(runtime.account_id)
    return {"cloudinary": _mask_cloudinary(row)}


@app.put("/api/account/cloudinary")
async def put_cloudinary(account: AccountDep, body: CloudinaryRequest) -> dict:
    caller, runtime = account
    if runtime.migrator.is_active:
        raise HTTPException(
            status_code=409, detail="Stop the running migration before changing the target."
        )

    credentials = CloudinaryCredentials(
        cloud_name=body.cloud_name.strip(),
        api_key=body.api_key.strip(),
        api_secret=body.api_secret.strip(),
        folder=body.folder.strip() or "telegram_migration",
        folder_per_chat=body.folder_per_chat,
    )

    # Verify before saving: a typo here would otherwise surface as a failure on
    # every single file, hundreds of files into a run.
    try:
        await CloudinaryService(caller.ctx.settings, credentials).verify()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"Cloudinary rejected those credentials: {exc}"
        ) from exc

    await caller.ctx.db.set_cloudinary(
        runtime.account_id,
        cloud_name=credentials.cloud_name,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        folder=credentials.folder,
        folder_per_chat=credentials.folder_per_chat,
    )
    await caller.ctx.runtimes.refresh_cloudinary(runtime.account_id)
    row = await caller.ctx.db.get_cloudinary(runtime.account_id)
    await runtime.hub.log(f"Cloudinary target set to “{credentials.cloud_name}”.", "success")
    return {"cloudinary": _mask_cloudinary(row)}


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


@app.get("/api/health")
async def health(account: AccountDep) -> dict:
    caller, runtime = account
    return {
        "telegram_connected": runtime.telegram.connected,
        "account": runtime.account["display_name"],
        "account_id": runtime.account_id,
        "cloudinary_cloud": runtime.cloudinary.cloud_name if runtime.cloudinary else None,
        "cloudinary_configured": runtime.cloudinary_configured,
        "migrated_total": await runtime.store.total_count(),
        "log_clients": runtime.hub.client_count,
    }


# --------------------------------------------------------------------------- #
# Channels and files
# --------------------------------------------------------------------------- #


@app.get("/api/channels")
async def list_channels(
    account: AccountDep,
    include_dms: Annotated[bool | None, Query()] = None,
) -> dict:
    caller, runtime = account
    try:
        dialogs = await runtime.telegram.list_dialogs(include_dms)
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to list dialogs")
        raise HTTPException(status_code=502, detail=f"Telegram error: {exc}") from exc

    counts = await runtime.store.chat_counts()
    payload = []
    for dialog in dialogs:
        item = dialog.to_dict()
        item["migrated_count"] = counts.get(dialog.id, 0)
        payload.append(item)
    return {"channels": payload, "count": len(payload)}


@app.get("/api/channels/{chat_id}/files")
async def list_files(
    account: AccountDep,
    chat_id: int,
    offset_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
    type_filter: Annotated[TypeFilter, Query()] = "all",
) -> dict:
    caller, runtime = account
    try:
        page = await runtime.telegram.list_media_page(
            chat_id, offset_id=offset_id, limit=limit, type_filter=type_filter
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown chat {chat_id}.") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to list files for %s", chat_id)
        raise HTTPException(status_code=502, detail=f"Telegram error: {exc}") from exc

    migrated = await runtime.store.migrated_ids(chat_id)
    for item in page["items"]:
        item["migrated"] = item["message_id"] in migrated

    page["chat_id"] = chat_id
    page["type_filter"] = type_filter
    return page


@app.delete("/api/channels/{chat_id}/migrated")
async def forget_chat(account: AccountDep, chat_id: int) -> dict:
    caller, runtime = account
    if runtime.migrator.is_active:
        raise HTTPException(status_code=409, detail="Stop the running migration first.")
    removed = await runtime.store.forget_chat(chat_id)
    await runtime.hub.log(f"Cleared {removed} migrated record(s) for chat {chat_id}.", "warning")
    return {"chat_id": chat_id, "removed": removed}


# --------------------------------------------------------------------------- #
# Migration controls
# --------------------------------------------------------------------------- #


@app.post("/api/migrate/start")
async def migrate_start(account: AccountDep, body: StartRequest) -> dict:
    caller, runtime = account
    ids = unique_ints(body.message_ids) if body.message_ids is not None else None
    if ids is not None and not ids:
        raise HTTPException(status_code=400, detail="Select at least one file, or use Sync all.")
    return await runtime.migrator.start(body.chat_id, ids)


@app.post("/api/migrate/pause")
async def migrate_pause(account: AccountDep) -> dict:
    return await account[1].migrator.pause()


@app.post("/api/migrate/resume")
async def migrate_resume(account: AccountDep) -> dict:
    return await account[1].migrator.resume()


@app.post("/api/migrate/stop")
async def migrate_stop(account: AccountDep) -> dict:
    return await account[1].migrator.stop()


@app.get("/api/migrate/status")
async def migrate_status(account: AccountDep) -> dict:
    return account[1].migrator.snapshot()


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    """Attaches to the caller's *active account* hub, so accounts never see
    each other's logs."""
    ctx: AppContext = websocket.app.state.ctx
    token = websocket.cookies.get(ctx.settings.cookie_name)
    session = await ctx.sessions.get(token)

    if session is None or not session.get("account_id"):
        await websocket.close(code=4401)  # policy violation: not authenticated
        return

    try:
        runtime = await ctx.runtimes.get(int(session["account_id"]))
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return

    await runtime.hub.connect(websocket)
    try:
        await websocket.send_json({"type": "history", "events": runtime.hub.history()})
        await websocket.send_json({"type": "progress", **runtime.migrator.snapshot()})
        while True:
            await websocket.receive_text()  # keepalives only
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("WebSocket closed unexpectedly", exc_info=True)
    finally:
        await runtime.hub.disconnect(websocket)


# --------------------------------------------------------------------------- #
# Static frontend (mounted last so /api and /ws win)
# --------------------------------------------------------------------------- #

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:  # pragma: no cover - only when the repo layout is broken
    log.warning("Frontend directory %s not found — serving the API only.", FRONTEND_DIR)

    @app.get("/")
    async def _no_frontend() -> dict:
        return {"detail": f"Frontend not found at {FRONTEND_DIR}. API is available under /api."}
