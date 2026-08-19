# Telegram → Cloudinary Migrator

A dashboard for bulk-moving media out of Telegram and into Cloudinary. Sign in from the
browser with a Telegram login code, browse your chats, pick files (or sync a whole
channel), and watch every download and upload stream past in a live console. Multiple
accounts, each with its own Cloudinary target and its own migration history.

```
┌──────────────┬────────────────────────────────────┬──────────────────┐
│  Chats       │  Files + toolbar + progress        │  Live log        │
│  (search,    │  (type tabs, select, Sync all,     │  (WebSocket,     │
│   accounts)  │   Start / Pause / Resume / Stop)   │   colour-coded)  │
└──────────────┴────────────────────────────────────┴──────────────────┘
```

**Stack:** FastAPI + Telethon + the official Cloudinary SDK + SQLite on the backend;
plain HTML/CSS/JS on the frontend. No `npm install`, no build step.

---

## Sign-in, in three gates

```
   passcode            phone → code → 2FA           cloud name / key / secret
  ───────────►  unlock  ──────────────────►  account  ───────────────────►  ready
   (from .env)          (Telegram OTP)               (verified, then saved)
```

1. **Dashboard passcode** — from `.env`. Without it, anyone who reaches the port could
   make Telegram send a login code to a stranger's phone. Five wrong tries triggers a
   15-minute lockout.
2. **Telegram OTP** — phone number → the code Telegram sends → two-step verification
   password if the account has one. The session string this produces is written to
   SQLite and **never** sent to the browser.
3. **Cloudinary target** — entered per account and verified against Cloudinary's API
   before it is saved, so a typo fails at setup rather than on file 300 of a run.

Sessions persist, so a restart doesn't mean logging in again. Switch accounts, add
another, or sign out entirely from the ⋯ menu beside the account name.

---

## Setup

Only three environment variables now, and none of them is a user credential:

```bash
git clone <your-repo> && cd telegram-cloudinary-migrator
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

| Variable | Where it comes from |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | <https://my.telegram.org> → API development tools. Identifies the *app*, shared by every account. |
| `DASHBOARD_PASSCODE` | You choose it. Minimum 8 characters — `openssl rand -base64 24` is a good source. |

Then:

```bash
./run.sh                                   # or:
cd backend && uvicorn main:app --host 0.0.0.0 --port 8000
```

In Codespaces, open the forwarded port 8000 from the **Ports** tab. Locally, open
<http://localhost:8000>. Everything else — phone numbers, login codes, Cloudinary
credentials — happens in the browser.

> Keep the port **private** anyway. The passcode is a speed bump against a stray
> scanner, not a substitute for network isolation.

### Headless alternative

If you can't reach the UI (no port forwarding, a setup script), `python add_account.py`
runs the same phone → code → 2FA flow on the command line and writes the account into
the same database.

### Verify without touching real accounts

```bash
cd backend && pip install httpx && python smoke_test.py
```

44 assertions covering passcode lockout, OTP sign-in including 2FA, per-account
Cloudinary isolation, migration, resume-skipping, pause → resume → stop, WebSocket
authorisation, and account switching. Run it after any change to auth or the migration
loop.

---

## Why Codespaces and not Cloudflare Workers

Telegram's client protocol (MTProto) runs over a **raw, persistent TCP socket**.
Workers, Pages Functions, and similar edge runtimes only offer `fetch` — outbound HTTP —
so they physically cannot speak MTProto, and they cap execution time far below what a
multi-gigabyte transfer needs.

This backend therefore assumes a long-running process with a real filesystem: a
persistent `uvicorn` server holding one authenticated Telegram connection per signed-in
account, streaming files to local disk and pushing them to Cloudinary in chunks. A
Codespace (or any small VM) gives exactly that, plus fast egress and port forwarding.

---

## How each control behaves

| Control | What happens |
| --- | --- |
| **Select all** | Selects every file in the current filtered view that isn't already migrated. |
| **Start migration** | Migrates only the selected files. `409` if a run is already active, `428` if Cloudinary isn't set up. |
| **Sync all** | Scans the entire history of the selected chat server-side and migrates everything matching. Already-migrated messages are skipped during the scan. |
| **Pause** | Sets a gate. The file currently in flight finishes cleanly; the loop then waits. Nothing is downloaded, uploaded, or half-written while paused. |
| **Resume** | Opens the gate. The run continues at the *exact* next file. |
| **Stop** | Ends the run. If paused, it exits immediately; if a file is mid-transfer, that file completes first so Cloudinary never receives a truncated asset. |
| **Clear** / `Esc` | Clears the current selection. |
| **Load older files** | Pages further back through history using Telethon's `offset_id`. |

Every completed file is committed to SQLite immediately. A restart, a crash, a browser
refresh, or a fresh "Sync all" will never re-upload what is already there. To redo a
chat, clear its record:

```bash
curl -X DELETE http://localhost:8000/api/channels/<chat_id>/migrated \
     --cookie "migrator_session=<your cookie>"
```

---

## API

Everything under `/api` needs an unlocked session cookie. Everything under
`/api/channels`, `/api/migrate` and `/api/account` additionally needs a signed-in
Telegram account (`428` if absent).

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/auth/session` | Unauthenticated probe: locked? signed in? which accounts exist? |
| `POST` | `/api/auth/unlock` · `/api/auth/lock` | Passcode gate |
| `POST` | `/api/auth/telegram/send-code` | `{phone}` → `{login_id}`, code sent |
| `POST` | `/api/auth/telegram/verify-code` | `{login_id, code}` → signed in, or `needs_password` |
| `POST` | `/api/auth/telegram/password` | `{login_id, password}` — two-step verification |
| `POST` | `/api/auth/telegram/cancel` | Abandon a half-finished login |
| `GET` | `/api/accounts` | Saved accounts + which one is active |
| `POST` | `/api/accounts/{id}/activate` | Switch the active account |
| `DELETE` | `/api/accounts/{id}` | Sign out; `?revoke=false` to keep the Telegram session alive |
| `GET`/`PUT` | `/api/account/cloudinary` | Per-account Cloudinary target (secret is write-only) |
| `GET` | `/api/health` | Connection state, cloud, migrated count |
| `GET` | `/api/channels` | Dialogs with `migrated_count` |
| `GET` | `/api/channels/{chat_id}/files` | Paginated media (`offset_id`, `limit`, `type_filter`) |
| `DELETE` | `/api/channels/{chat_id}/migrated` | Forget a chat's history |
| `POST` | `/api/migrate/start` | `{chat_id, message_ids}` — `null` = full sync |
| `POST` | `/api/migrate/pause` · `/resume` · `/stop` | Run controls |
| `GET` | `/api/migrate/status` | Snapshot for reconnecting clients |
| `WS` | `/ws/logs` | `history`, `log`, `progress`, `file_progress` — scoped to the active account |

Interactive docs: <http://localhost:8000/docs>.

---

## File type routing

| Extensions | Cloudinary `resource_type` |
| --- | --- |
| `.mp4 .mov .mkv .avi .webm` | `video` |
| `.jpg .jpeg .png .gif .webp` | `image` |
| `.pdf .html .htm` | `raw` |

Uploads use `upload_large()` with a 20 MB chunk size, `use_filename=True`,
`unique_filename=False`, `overwrite=False`, into `<folder>/<chat-slug>/` (uncheck
"sub-folder per chat" for one flat folder). Stickers, voice notes and video notes are
skipped by default so they don't sneak in through `.webp` and `.mp4`.

---

## Design notes

**`cloudinary.config()` is never called.** It sets process-global state, and with one
Cloudinary target per account that would mean account B's upload could land in account
A's cloud depending on timing. Credentials are passed explicitly on every SDK call
instead.

**One runtime per account.** Each signed-in account gets its own Telegram connection,
migration manager and event hub, so two accounts can migrate simultaneously without
sharing a log stream or a rate-limit budget. Runtimes start lazily after a restart and
stop on sign-out.

**Half-finished logins hold a live client.** A Telegram login code is only valid on the
connection that requested it, so the pending `TelegramClient` stays in memory between
"send code" and "verify". A sweeper disconnects any login nobody finished within
10 minutes. On success the connection is *adopted* rather than reconnected — it is
already authorised, so a second handshake would be pure waste.

**The event loop never blocks.** Both the Cloudinary SDK and `sqlite3` are synchronous,
so both go through `asyncio.to_thread`. Without that, a multi-GB `upload_large` would
freeze the WebSocket log and make Pause look broken.

**Control is cooperative, not violent.** Pause is an `asyncio.Event` awaited *between*
files; Stop is a flag checked each iteration that also releases the pause gate. Nothing
cancels a task mid-transfer, so there are no orphaned partial assets.

**Disk stays flat.** Each file downloads into its own scratch directory and that
directory is deleted in a `finally` block whether the upload succeeded or failed. Peak
usage is one file, not the 20 GB of the run. The per-file directory also keeps the local
name identical to the Telegram filename, which is what `use_filename=True` turns into
the Cloudinary `public_id`.

**Listing is bounded.** A chat with 200k text messages would stall a naive listing
request, so each page scans at most `SCAN_BATCH` raw messages and returns
`next_offset_id` for the next page.

**Secrets never reach the browser.** Session strings and API secrets go into SQLite;
the API returns `api_secret_set: true`, never the value. Cookies are `HttpOnly`, tokens
are stored hashed, and the phone number is masked in every response.

---

## Layout

```
backend/
  main.py               FastAPI app: auth gates, routes, WebSocket, static mount
  auth.py               Passcode lockout, OTP state machine, session cookies
  db.py                 SQLite schema + data access (accounts, configs, history, sessions)
  runtime.py            Per-account runtime registry
  config.py             .env loading, extension → resource_type routing
  telegram_service.py   Telethon: dialogs, media discovery, downloads
  cloudinary_service.py upload_large in a worker thread, per-account credentials
  migration.py          The run loop: pause gate, stop flag, resumability
  state.py              Account-scoped view of the migrated-files table
  events.py             WebSocket fan-out + replay buffer
  add_account.py        Headless sign-in (same flow, no browser)
  import_legacy.py      Import a v1 migrated_ids.json
  smoke_test.py         Offline end-to-end check (44 assertions)
frontend/
  index.html style.css app.js
.devcontainer/devcontainer.json
run.sh
```

Runtime files, all gitignored: `backend/migrator.db` (**holds live session strings and
API secrets** — created 0600, treat it like a password manager export) and
`backend/tmp_downloads/`.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `DASHBOARD_PASSCODE must be at least 8 characters` | The server refuses to start with a weak gate. Set a longer one. |
| "Too many failed attempts. Locked for 15 minutes." | Wait it out, or restart the server — the lockout is in memory. |
| "That code isn't right" but the code is right | Codes are per-login. If you re-sent one, use the newest; the older `login_id` is dead. |
| "This login expired. Start again." | More than 10 minutes passed between requesting and entering the code. |
| "This Telegram session is no longer valid" | The session was revoked from another device. The account is dropped; sign in again. |
| `Cloudinary rejected those credentials` | Check cloud name / key / secret. The check runs before saving, so nothing was stored. |
| Log console shows "Offline" | The server stopped or the Codespace port slept. It reconnects automatically with backoff. |
| Log shows `Telegram rate limit: wait Ns` | Telethon sleeps through FloodWaits under `FLOOD_SLEEP_THRESHOLD` (120s); longer ones surface as a failure. Wait, then re-run — completed files are skipped. |

## Non-goals

No multi-tenant user management (the passcode is one shared gate, not per-person
accounts), no retry-with-backoff beyond what the SDKs already do, no public
deployment. This is a private tool for one operator's own accounts.
