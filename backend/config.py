"""Environment-driven configuration.

The environment now holds only what is genuinely machine-level: the Telegram
application identity (api_id / api_hash, which belongs to the *app*, not to a
user), the dashboard passcode, and paths. Per-user secrets — session strings
and Cloudinary credentials — are supplied through the UI and stored per
account in SQLite, so a different person can sit down at the same deployment
and use it without editing files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------- #
# Extension routing
# --------------------------------------------------------------------------- #

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
RAW_EXTENSIONS = {".pdf", ".html", ".htm"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | RAW_EXTENSIONS

KIND_EXTENSIONS: dict[str, set[str]] = {
    "video": VIDEO_EXTENSIONS,
    "image": IMAGE_EXTENSIONS,
    "pdf": {".pdf"},
    "html": {".html", ".htm"},
}


def kind_for_extension(ext: str) -> str:
    ext = ext.lower()
    for kind, extensions in KIND_EXTENSIONS.items():
        if ext in extensions:
            return kind
    return "other"


def resource_type_for_extension(ext: str) -> str:
    """Cloudinary `resource_type` routing: video / image / raw."""
    ext = ext.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "raw"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

MIN_PASSCODE_LENGTH = 8


class ConfigError(RuntimeError):
    """Raised when the environment is incomplete — surfaced at startup."""


@dataclass(frozen=True, slots=True)
class Settings:
    # Telegram application identity (shared by every account that signs in)
    tg_api_id: int
    tg_api_hash: str

    # Dashboard gate
    dashboard_passcode: str
    session_ttl_hours: int = 168          # how long a browser stays unlocked
    login_ttl_minutes: int = 10           # how long an unfinished OTP login lives
    max_passcode_attempts: int = 5
    lockout_minutes: int = 15

    # Optional Cloudinary prefill for the setup form (never required)
    default_cloud_name: str = ""
    default_api_key: str = ""
    default_folder: str = "telegram_migration"

    # Behaviour
    include_dms: bool = True
    skip_stickers: bool = True
    folder_per_chat: bool = True
    scan_batch: int = 300
    page_size: int = 60
    chunk_size: int = 20 * 1024 * 1024
    flood_sleep_threshold: int = 120

    # Paths
    db_path: Path = BASE_DIR / "migrator.db"
    download_dir: Path = BASE_DIR / "tmp_downloads"
    legacy_state_file: Path = BASE_DIR / "migrated_ids.json"

    @property
    def cookie_name(self) -> str:
        return "migrator_session"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return raw.lower() in {"1", "true", "yes", "on"} if raw else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_settings() -> Settings:
    missing = [n for n in ("TG_API_ID", "TG_API_HASH", "DASHBOARD_PASSCODE") if not _env(n)]
    if missing:
        raise ConfigError(
            "Missing environment variables: "
            + ", ".join(missing)
            + f". Copy {BASE_DIR / '.env.example'} to .env and fill it in. "
            "Telegram and Cloudinary credentials are entered in the browser, "
            "not here."
        )

    try:
        api_id = int(_env("TG_API_ID"))
    except ValueError as exc:
        raise ConfigError("TG_API_ID must be an integer.") from exc

    passcode = _env("DASHBOARD_PASSCODE")
    if len(passcode) < MIN_PASSCODE_LENGTH:
        raise ConfigError(
            f"DASHBOARD_PASSCODE must be at least {MIN_PASSCODE_LENGTH} characters. "
            "It is the only thing standing between the open internet and an OTP "
            "sent to your phone."
        )

    return Settings(
        tg_api_id=api_id,
        tg_api_hash=_env("TG_API_HASH"),
        dashboard_passcode=passcode,
        session_ttl_hours=_env_int("SESSION_TTL_HOURS", 168),
        login_ttl_minutes=_env_int("LOGIN_TTL_MINUTES", 10),
        max_passcode_attempts=_env_int("MAX_PASSCODE_ATTEMPTS", 5),
        lockout_minutes=_env_int("LOCKOUT_MINUTES", 15),
        default_cloud_name=_env("CLOUDINARY_CLOUD_NAME"),
        default_api_key=_env("CLOUDINARY_API_KEY"),
        default_folder=_env("CLOUDINARY_FOLDER") or "telegram_migration",
        include_dms=_env_bool("INCLUDE_DMS", True),
        skip_stickers=_env_bool("SKIP_STICKERS", True),
        folder_per_chat=_env_bool("CLOUDINARY_FOLDER_PER_CHAT", True),
        scan_batch=_env_int("SCAN_BATCH", 300),
        page_size=_env_int("PAGE_SIZE", 60),
        chunk_size=_env_int("UPLOAD_CHUNK_SIZE", 20 * 1024 * 1024),
        flood_sleep_threshold=_env_int("FLOOD_SLEEP_THRESHOLD", 120),
        db_path=Path(_env("DB_PATH") or (BASE_DIR / "migrator.db")),
        download_dir=Path(_env("DOWNLOAD_DIR") or (BASE_DIR / "tmp_downloads")),
        legacy_state_file=Path(_env("LEGACY_STATE_FILE") or (BASE_DIR / "migrated_ids.json")),
    )
