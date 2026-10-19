"""
main.py — Link Unlock Bot & API  (private, owner-controlled multi-user edition)
===============================================================================
A single-file FastAPI + python-telegram-bot (v20+) application.

Runs a Telegram bot (button-driven link creation / edit / delete + admin tools)
and a REST API (serves link configs to the frontend) concurrently in a single
asyncio event loop.

Access model
------------
* Owner  : the Telegram numeric user ID in ADMIN_ID. Always authorized.
* Approved users : rows in ``approved_users`` with status = 'approved'.
* Everyone else (never approved / removed / banned) is "unauthorized".

All authorization decisions use Telegram's verified numeric user ID — never
usernames — and are re-read from SQLite on every update (no caching), so
revocation is immediate.

Setup:
    pip install -r requirements.txt
    cp .env.example .env     # then edit BOT_TOKEN and ADMIN_ID
    python main.py

Environment (read from the process environment, or from a ``.env`` file that
sits next to main.py):
    BOT_TOKEN           Telegram bot token from @BotFather              (required)
    ADMIN_ID            Owner's numeric Telegram user ID                (required)
    DB_PATH             SQLite file path                                (default: links.db)
    FRONTEND_BASE_URL   Base URL used to build generated links          (default: https://hornyunlock.netlify.app)
    API_HOST            Host for the REST API                           (default: 0.0.0.0)
    API_PORT            Port for the REST API                           (default: 8000)
"""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:  # python-telegram-bot >= 20.8 / Bot API 7.0
    from telegram import LinkPreviewOptions

    NO_PREVIEW: dict[str, Any] = {
        "link_preview_options": LinkPreviewOptions(is_disabled=True)
    }
except ImportError:  # pragma: no cover - older PTB
    NO_PREVIEW = {"disable_web_page_preview": True}

# ==========================================================================
# SECTION 1 — Configuration
# ==========================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("link_unlock")


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no extra dependency). Never overrides real env vars."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(os.environ.get("ENV_FILE") or Path(__file__).resolve().parent / ".env"))


def _parse_positive_int(raw: str) -> int:
    raw = raw.strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= 19:
        value = int(raw)
        if 0 < value < 2**63:
            return value
    return 0


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = _parse_positive_int(os.environ.get("ADMIN_ID", ""))
DB_PATH = os.environ.get("DB_PATH", "links.db")
FRONTEND_BASE_URL = os.environ.get(
    "FRONTEND_BASE_URL", "https://hornyunlock.netlify.app"
).rstrip("/")
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
_API_PORT_RAW = os.environ.get("API_PORT", "8000")
API_PORT = _parse_positive_int(_API_PORT_RAW) or 8000

MAX_URL_LENGTH = 2048          # hard limit for any stored URL
USERS_PER_PAGE = 10            # admin user list page size
LINKS_PER_PAGE = 5             # My Links / admin link list page size
SQLITE_TIMEOUT = 15            # seconds to wait on a locked database

STATUS_APPROVED = "approved"
STATUS_REMOVED = "removed"
STATUS_BANNED = "banned"


def validate_config() -> None:
    """Fail fast with a readable message if required configuration is wrong."""
    errors: list[str] = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        errors.append("BOT_TOKEN is missing or malformed (get one from @BotFather).")
    if ADMIN_ID <= 0:
        errors.append(
            "ADMIN_ID is missing or not a positive number "
            "(your numeric Telegram user ID, e.g. from @userinfobot)."
        )
    if validate_url(FRONTEND_BASE_URL)[1] is not None:
        errors.append("FRONTEND_BASE_URL must be a valid http:// or https:// URL.")
    if _parse_positive_int(_API_PORT_RAW) == 0 or API_PORT > 65535:
        errors.append("API_PORT must be a number between 1 and 65535.")
    if not DB_PATH.strip():
        errors.append("DB_PATH must not be empty.")
    if errors:
        raise RuntimeError(
            "Invalid configuration:\n  - " + "\n  - ".join(errors)
            + "\nSee .env.example for the expected settings."
        )


# Conversation states
ASK_OPEN_URL, ASK_UNLOCK_URL = range(2)
EDIT_CHOOSE_LINK, EDIT_CHOOSE_FIELD, EDIT_NEW_VALUE = range(2, 5)
SET_OPEN_URL_VALUE = 5
DELETE_CHOOSE_LINK, DELETE_CONFIRM = 6, 7
ADMIN_ASK_ID = 8

# ==========================================================================
# SECTION 2 — Small helpers (validation, formatting)
# ==========================================================================

_URL_FORBIDDEN_CHARS = re.compile(r"[\s\x00-\x1f\x7f<>\"`\\]")


def validate_url(text: str | None) -> tuple[str | None, str | None]:
    """Return (clean_url, None) or (None, reason). Only http(s) URLs are allowed."""
    value = (text or "").strip()
    if not value:
        return None, "The URL is empty."
    if len(value) > MAX_URL_LENGTH:
        return None, f"The URL is too long (max {MAX_URL_LENGTH} characters)."
    if _URL_FORBIDDEN_CHARS.search(value):
        return None, "The URL contains spaces or unsafe characters."
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return None, "The URL is malformed."
    if parts.scheme.lower() not in ("http", "https"):
        return None, "Only http:// and https:// URLs are allowed."
    if not parts.hostname:
        return None, "The URL has no host name."
    return value, None


def is_valid_url(text: str) -> bool:
    return validate_url(text)[1] is None


def parse_telegram_id(text: str | None) -> int | None:
    value = (text or "").strip()
    if value.isascii() and value.isdigit() and len(value) <= 19:
        number = int(value)
        if 0 < number < 2**63:
            return number
    return None


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def short(text: str, limit: int = 80) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def page_url(link_id: int) -> str:
    return f"{FRONTEND_BASE_URL}/?id={link_id}"


def audit(action: str, actor: int, target: Any = "-", result: str = "ok") -> None:
    logger.info("audit | action=%s actor=%s target=%s result=%s", action, actor, target, result)


# ==========================================================================
# SECTION 3 — Database layer (aiosqlite) + non-destructive migrations
# ==========================================================================

SCHEMA_VERSION = 2


@asynccontextmanager
async def db_connect():
    """Autocommit connection (explicit transactions via ``transaction()``)."""
    db = await aiosqlite.connect(DB_PATH, timeout=SQLITE_TIMEOUT, isolation_level=None)
    try:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT * 1000}")
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def transaction():
    """BEGIN IMMEDIATE … COMMIT (ROLLBACK on error). Serializes writers safely."""
    async with db_connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            try:
                await db.execute("ROLLBACK")
            except Exception:  # connection may already be unusable
                pass
            raise
        else:
            try:
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise


async def _table_columns(db: aiosqlite.Connection, table: str) -> list[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:  # constant names only
        return [row["name"] for row in await cur.fetchall()]


async def init_db() -> None:
    """Create / upgrade the schema. Idempotent, never drops or rewrites user data."""
    parent = Path(DB_PATH).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    async with db_connect() as db:
        try:  # WAL lets the API read while the bot writes; persistent setting
            await db.execute("PRAGMA journal_mode = WAL")
        except Exception as exc:  # pragma: no cover
            logger.warning("Could not enable WAL mode: %s", exc)

    async with transaction() as db:
        # --- original tables (no-ops on an existing production database) ---
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL,
                open_url     TEXT NOT NULL,
                unlock_url   TEXT NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_prefs (
                telegram_id     INTEGER PRIMARY KEY,
                saved_open_url  TEXT
            )
            """
        )

        # --- legacy column rename (saved_unlock_url -> saved_open_url) ---
        pref_cols = await _table_columns(db, "user_prefs")
        if "saved_open_url" not in pref_cols:
            if "saved_unlock_url" in pref_cols:
                await db.execute(
                    "ALTER TABLE user_prefs RENAME COLUMN saved_unlock_url TO saved_open_url"
                )
            else:
                await db.execute("ALTER TABLE user_prefs ADD COLUMN saved_open_url TEXT")

        # --- access control (new) ---
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS approved_users (
                telegram_id  INTEGER PRIMARY KEY,
                status       TEXT NOT NULL DEFAULT 'approved'
                             CHECK (status IN ('approved', 'removed', 'banned')),
                added_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                added_by     INTEGER
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_links_telegram_id ON links (telegram_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_approved_users_status ON approved_users (status)"
        )

        # Global, never-reused IDs rely on AUTOINCREMENT; warn if a legacy table lacks it.
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'links'"
        ) as cur:
            row = await cur.fetchone()
        if row and "AUTOINCREMENT" not in (row["sql"] or "").upper():
            logger.warning(
                "links table was created without AUTOINCREMENT; deleted IDs could be reused."
            )

        async with db.execute("PRAGMA user_version") as cur:
            current = (await cur.fetchone())[0]
        if current < SCHEMA_VERSION:
            await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    logger.info("Database ready at %s (schema v%s)", DB_PATH, SCHEMA_VERSION)


# ---- access control -------------------------------------------------------


def _auth_condition(telegram_id: int) -> tuple[str, tuple]:
    """SQL condition proving the user is authorized *inside* the write statement."""
    if telegram_id == ADMIN_ID:
        return "1 = 1", ()
    return (
        "EXISTS (SELECT 1 FROM approved_users WHERE telegram_id = ? AND status = 'approved')",
        (telegram_id,),
    )


async def get_user_status(telegram_id: int) -> str | None:
    async with db_connect() as db:
        async with db.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["status"] if row else None


async def is_authorized(telegram_id: int) -> bool:
    """Owner, or a user whose status is currently 'approved'. Read fresh every time."""
    if telegram_id == ADMIN_ID:
        return True
    return await get_user_status(telegram_id) == STATUS_APPROVED


async def approve_user(target: int, admin: int) -> tuple[str, int]:
    """Returns (result, link_count). result: added|readded|already|banned|owner."""
    if target == ADMIN_ID:
        return "owner", 0
    async with transaction() as db:
        async with db.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?", (target,)
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM links WHERE telegram_id = ?", (target,)
        ) as cur:
            link_count = (await cur.fetchone())[0]
        if row is None:
            await db.execute(
                "INSERT INTO approved_users (telegram_id, status, added_by) VALUES (?, 'approved', ?)",
                (target, admin),
            )
            return "added", link_count
        if row["status"] == STATUS_APPROVED:
            return "already", link_count
        if row["status"] == STATUS_BANNED:
            return "banned", link_count
        await db.execute(
            "UPDATE approved_users SET status = 'approved', added_by = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE telegram_id = ?",
            (admin, target),
        )
        return "readded", link_count


async def remove_user(target: int) -> tuple[str, int]:
    """result: removed|already_removed|banned|unknown|owner. Never touches links/prefs."""
    if target == ADMIN_ID:
        return "owner", 0
    async with transaction() as db:
        async with db.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?", (target,)
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM links WHERE telegram_id = ?", (target,)
        ) as cur:
            link_count = (await cur.fetchone())[0]
        if row is None:
            return "unknown", link_count
        if row["status"] == STATUS_BANNED:
            return "banned", link_count
        if row["status"] == STATUS_REMOVED:
            return "already_removed", link_count
        await db.execute(
            "UPDATE approved_users SET status = 'removed', updated_at = CURRENT_TIMESTAMP "
            "WHERE telegram_id = ?",
            (target,),
        )
        return "removed", link_count


async def ban_user(target: int, admin: int) -> tuple[str, int]:
    """result: banned|banned_new|already_banned|owner. Never touches links/prefs."""
    if target == ADMIN_ID:
        return "owner", 0
    async with transaction() as db:
        async with db.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?", (target,)
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM links WHERE telegram_id = ?", (target,)
        ) as cur:
            link_count = (await cur.fetchone())[0]
        if row is None:
            await db.execute(
                "INSERT INTO approved_users (telegram_id, status, added_by) VALUES (?, 'banned', ?)",
                (target, admin),
            )
            return "banned_new", link_count
        if row["status"] == STATUS_BANNED:
            return "already_banned", link_count
        await db.execute(
            "UPDATE approved_users SET status = 'banned', updated_at = CURRENT_TIMESTAMP "
            "WHERE telegram_id = ?",
            (target,),
        )
        return "banned", link_count


async def unban_user(target: int) -> tuple[str, int]:
    """Lifts a ban; the user ends up 'removed' (must be re-approved explicitly)."""
    if target == ADMIN_ID:
        return "owner", 0
    async with transaction() as db:
        async with db.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?", (target,)
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM links WHERE telegram_id = ?", (target,)
        ) as cur:
            link_count = (await cur.fetchone())[0]
        if row is None:
            return "unknown", link_count
        if row["status"] != STATUS_BANNED:
            return "not_banned", link_count
        await db.execute(
            "UPDATE approved_users SET status = 'removed', updated_at = CURRENT_TIMESTAMP "
            "WHERE telegram_id = ?",
            (target,),
        )
        return "unbanned", link_count


_USER_FILTERS = {
    "approved": "u.status = 'approved'",
    "removed": "u.status = 'removed'",
    "banned": "u.status = 'banned'",
    "all": "1 = 1",
}


async def list_users(status_filter: str, limit: int, offset: int) -> tuple[list[aiosqlite.Row], int]:
    where = _USER_FILTERS.get(status_filter, _USER_FILTERS["approved"])
    async with db_connect() as db:
        async with db.execute(f"SELECT COUNT(*) FROM approved_users u WHERE {where}") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            f"""
            SELECT u.telegram_id, u.status, u.added_at, u.updated_at, u.added_by,
                   (SELECT COUNT(*) FROM links l WHERE l.telegram_id = u.telegram_id) AS link_count
            FROM approved_users u WHERE {where}
            ORDER BY u.added_at DESC, u.telegram_id DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cur:
            return await cur.fetchall(), total


async def get_user_info(target: int) -> dict[str, Any] | None:
    async with db_connect() as db:
        async with db.execute(
            "SELECT * FROM approved_users WHERE telegram_id = ?", (target,)
        ) as cur:
            access = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id, "
            "MAX(created_at) AS last_created FROM links WHERE telegram_id = ?",
            (target,),
        ) as cur:
            links = await cur.fetchone()
        async with db.execute(
            "SELECT saved_open_url FROM user_prefs WHERE telegram_id = ?", (target,)
        ) as cur:
            pref = await cur.fetchone()
    if access is None and links["n"] == 0 and pref is None:
        return None
    return {
        "access": dict(access) if access else None,
        "link_count": links["n"],
        "first_id": links["first_id"],
        "last_id": links["last_id"],
        "last_created": links["last_created"],
        "saved_open_url": pref["saved_open_url"] if pref else None,
    }


async def get_system_stats() -> dict[str, Any]:
    async with db_connect() as db:
        stats: dict[str, Any] = {STATUS_APPROVED: 0, STATUS_REMOVED: 0, STATUS_BANNED: 0}
        async with db.execute(
            "SELECT status, COUNT(*) AS n FROM approved_users GROUP BY status"
        ) as cur:
            for row in await cur.fetchall():
                stats[row["status"]] = row["n"]
        async with db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT telegram_id), COALESCE(MAX(id), 0), "
            "COALESCE(SUM(created_at >= datetime('now', '-1 day')), 0) FROM links"
        ) as cur:
            total, creators, last_id, last_day = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM links WHERE telegram_id != ? AND telegram_id NOT IN "
            "(SELECT telegram_id FROM approved_users WHERE status = 'approved')",
            (ADMIN_ID,),
        ) as cur:
            inactive_links = (await cur.fetchone())[0]
    stats.update(
        total_links=total,
        creators=creators,
        last_id=last_id,
        links_last_24h=last_day,
        links_without_access=inactive_links,
    )
    return stats


# ---- links ---------------------------------------------------------------

_UPSERT_PREF = (
    "INSERT INTO user_prefs (telegram_id, saved_open_url) VALUES (?, ?) "
    "ON CONFLICT(telegram_id) DO UPDATE SET saved_open_url = excluded.saved_open_url"
)


async def create_link(telegram_id: int, open_url: str, unlock_url: str) -> int | None:
    """Insert a link and save the Open URL preference atomically.

    Authorization is re-checked *inside* the INSERT, so a user revoked a moment
    ago cannot create a link. Returns the new global ID, or None if not authorized.
    """
    cond, cond_params = _auth_condition(telegram_id)
    async with transaction() as db:
        cur = await db.execute(
            f"INSERT INTO links (telegram_id, open_url, unlock_url) SELECT ?, ?, ? WHERE {cond}",
            (telegram_id, open_url, unlock_url, *cond_params),
        )
        if cur.rowcount != 1:
            return None
        link_id = cur.lastrowid
        await db.execute(_UPSERT_PREF, (telegram_id, open_url))
    return link_id


async def get_link(link_id: int) -> aiosqlite.Row | None:
    """Public lookup for the frontend. Returns only non-private fields."""
    if not 0 < link_id < 2**63:
        return None
    async with db_connect() as db:
        async with db.execute(
            "SELECT id, open_url, unlock_url FROM links WHERE id = ?", (link_id,)
        ) as cur:
            return await cur.fetchone()


async def get_link_for_user(link_id: int, telegram_id: int) -> aiosqlite.Row | None:
    if not 0 < link_id < 2**63:
        return None
    async with db_connect() as db:
        async with db.execute(
            "SELECT * FROM links WHERE id = ? AND telegram_id = ?", (link_id, telegram_id)
        ) as cur:
            return await cur.fetchone()


async def count_links_for_user(telegram_id: int) -> int:
    async with db_connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM links WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return (await cur.fetchone())[0]


async def get_links_for_user(telegram_id: int, limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
    async with db_connect() as db:
        async with db.execute(
            "SELECT * FROM links WHERE telegram_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (telegram_id, limit, offset),
        ) as cur:
            return await cur.fetchall()


async def update_link(link_id: int, telegram_id: int, field: str, value: str) -> bool:
    """Update open_url or unlock_url. Ownership AND authorization are part of the SQL."""
    if field not in ("open_url", "unlock_url") or not 0 < link_id < 2**63:
        return False
    cond, cond_params = _auth_condition(telegram_id)
    column = "open_url" if field == "open_url" else "unlock_url"
    async with transaction() as db:
        cur = await db.execute(
            f"UPDATE links SET {column} = ? WHERE id = ? AND telegram_id = ? AND {cond}",
            (value, link_id, telegram_id, *cond_params),
        )
        if cur.rowcount != 1:
            return False
        if column == "open_url":
            await db.execute(_UPSERT_PREF, (telegram_id, value))
    return True


async def delete_link(link_id: int, telegram_id: int) -> bool:
    if not 0 < link_id < 2**63:
        return False
    cond, cond_params = _auth_condition(telegram_id)
    async with db_connect() as db:
        cur = await db.execute(
            f"DELETE FROM links WHERE id = ? AND telegram_id = ? AND {cond}",
            (link_id, telegram_id, *cond_params),
        )
        return cur.rowcount == 1


async def get_saved_open_url(telegram_id: int) -> str | None:
    async with db_connect() as db:
        async with db.execute(
            "SELECT saved_open_url FROM user_prefs WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            if row and row["saved_open_url"]:
                return row["saved_open_url"]
        # Fallback (existing behaviour): most recent open_url from this user's links
        async with db.execute(
            "SELECT open_url FROM links WHERE telegram_id = ? ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row["open_url"] if row and row["open_url"] else None


async def set_saved_open_url(telegram_id: int, open_url: str) -> bool:
    """Persist the personal Open URL. Returns False if the user is not authorized."""
    cond, cond_params = _auth_condition(telegram_id)
    async with db_connect() as db:
        cur = await db.execute(
            "INSERT INTO user_prefs (telegram_id, saved_open_url) SELECT ?, ? "
            f"WHERE {cond} "
            "ON CONFLICT(telegram_id) DO UPDATE SET saved_open_url = excluded.saved_open_url",
            (telegram_id, open_url, *cond_params),
        )
        return cur.rowcount == 1


async def list_all_links(limit: int, offset: int) -> tuple[list[aiosqlite.Row], int]:
    """Admin-only data access (callers must be behind require_admin)."""
    async with db_connect() as db:
        async with db.execute("SELECT COUNT(*) FROM links") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT * FROM links ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ) as cur:
            return await cur.fetchall(), total


# ==========================================================================
# SECTION 4 — FastAPI app (public, read-only; contract unchanged)
# ==========================================================================

api = FastAPI(title="Link Unlock API")

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@api.get("/api/config/{config_id}")
async def get_config(config_id: int) -> dict:
    # Works for every stored link regardless of the creator's current bot access.
    # Owner Telegram IDs are never included in the response.
    row = await get_link(config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Link configuration not found")

    return {
        "openLinkUrl": row["open_url"],
        "unlockUrl": row["unlock_url"],
        "branding": {
            "pageTitle": "Link Unlock — Premium Content Gate",
            "cardTitle": "UNLOCK <span>LINK</span>",
            "cardSubtitle": "Complete the actions below to unlock the content",
        },
        "steps": {
            "step1Label": "Step 1 — Visit Link",
            "step2Label": "Step 2 — Unlock Content",
            "openButtonText": "OPEN LINK",
            "unlockButtonText": "Complete Steps to Unlock",
        },
        "timer": {"duration": 30},
        "messages": {
            "successTitle": "Link Unlocked!",
            "successSub": (
                "Your content has been unlocked successfully.<br/>"
                "The link has been opened in a new tab."
            ),
        },
    }


# ==========================================================================
# SECTION 5 — Telegram: access-control helpers & decorators
# ==========================================================================

RESTRICTED_TEXT = (
    "🔒 <b>Access Restricted</b>\n\n"
    "This is a private Link Unlock Bot.\n\n"
    "You are not authorized to create or manage links.\n\n"
    "Please contact the administrator to request access.\n\n"
    "<i>Use /help for more information.</i>"
)

TgHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def _is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def deny_access(update: Update) -> None:
    query = update.callback_query
    if query is not None:
        try:
            await query.answer("🔒 Access restricted.", show_alert=True)
            await query.edit_message_text(RESTRICTED_TEXT, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return
    if update.effective_chat is not None:
        await update.effective_chat.send_message(RESTRICTED_TEXT, parse_mode=ParseMode.HTML)


async def deny_admin(update: Update) -> None:
    query = update.callback_query
    if query is not None:
        try:
            await query.answer("⛔ Administrator only.", show_alert=True)
        except Exception:
            pass
        return
    if update.effective_chat is not None:
        await update.effective_chat.send_message(
            "⛔ This command is restricted to the administrator."
        )


def require_approved(func: TgHandler) -> TgHandler:
    """Run the handler only for the owner / currently-approved users (checked live).

    Used on EVERY handler, including each step of a conversation, so a user who is
    removed or banned mid-conversation is stopped at their very next interaction.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not _is_private_chat(update):
            return ConversationHandler.END
        if not await is_authorized(user.id):
            context.user_data.clear()
            logger.info("auth | denied handler=%s user=%s", func.__name__, user.id)
            await deny_access(update)
            return ConversationHandler.END
        return await func(update, context)

    return wrapper


def require_admin(func: TgHandler) -> TgHandler:
    """Run the handler only for the verified numeric ADMIN_ID."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not _is_private_chat(update):
            return ConversationHandler.END
        if user.id != ADMIN_ID:
            logger.warning("auth | admin-only handler=%s denied user=%s", func.__name__, user.id)
            await deny_admin(update)
            return ConversationHandler.END
        return await func(update, context)

    return wrapper


# ==========================================================================
# SECTION 6 — Telegram: keyboards & shared UI helpers
# ==========================================================================


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🆕 Create Link", callback_data="create_new")],
        [
            InlineKeyboardButton("📋 My Links", callback_data="my_links"),
            InlineKeyboardButton("🔗 Set Open URL", callback_data="set_open_url"),
        ],
        [
            InlineKeyboardButton("📊 Statistics", callback_data="my_stats"),
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ]
    if user_id == ADMIN_ID:
        rows += [
            [InlineKeyboardButton("👥 Manage Users", callback_data="adm:menu")],
            [
                InlineKeyboardButton("➕ Add User", callback_data="adm:ask:add"),
                InlineKeyboardButton("➖ Remove User", callback_data="adm:ask:remove"),
            ],
            [
                InlineKeyboardButton("📃 View All Users", callback_data="adm:users:all:0"),
                InlineKeyboardButton("📈 System Statistics", callback_data="adm:stats"),
            ],
        ]
    return InlineKeyboardMarkup(rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])


def after_create_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Create Another", callback_data="create_new")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
    )


def nav_row(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("◀ Prev", callback_data=f"{prefix}{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(InlineKeyboardButton("Next ▶", callback_data=f"{prefix}{page + 1}"))
    return row


async def send_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the triggering callback-query message when possible, else send a new message."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, **NO_PREVIEW
            )
            return
        except Exception:
            pass
    await update.effective_chat.send_message(
        text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, **NO_PREVIEW
    )


WELCOME_TEXT = "👋 <b>Welcome to Link Unlock</b>\n\nChoose an option below:"


# ==========================================================================
# SECTION 7 — Telegram: /start, /help, menu, statistics
# ==========================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user is None or not _is_private_chat(update):
        return ConversationHandler.END
    context.user_data.clear()
    if not await is_authorized(user.id):
        await update.effective_chat.send_message(RESTRICTED_TEXT, parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    await update.effective_chat.send_message(
        WELCOME_TEXT, reply_markup=main_menu_keyboard(user.id), parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help and the Help button — available to everyone (private chats only)."""
    user = update.effective_user
    if user is None or not _is_private_chat(update):
        return
    if await is_authorized(user.id):
        text = (
            "❓ <b>Help</b>\n\n"
            "🆕 <b>Create Link</b> — add an Open URL and an Unlock URL, get a shareable page link\n"
            "📋 <b>My Links</b> — view, edit or delete your own links\n"
            "🔗 <b>Set Open URL</b> — save the Open URL reused for new links\n"
            "📊 <b>Statistics</b> — your link counts\n\n"
            "Commands: /start, /help, /cancel"
        )
        if user.id == ADMIN_ID:
            text += (
                "\n\n<b>Administrator commands</b>\n"
                "/adduser &lt;telegram_id&gt; — approve a user\n"
                "/removeuser &lt;telegram_id&gt; — revoke access (data is kept)\n"
                "/users [approved|removed|banned|all] [page] — list users\n"
                "/userinfo &lt;telegram_id&gt; — user details\n"
                "/ban &lt;telegram_id&gt; · /unban &lt;telegram_id&gt;\n"
                "/stats — system statistics\n"
                "/alllinks [page] — inspect all links"
            )
        markup = home_keyboard()
    else:
        text = (
            "🔒 <b>Link Unlock — private bot</b>\n\n"
            "This bot is invitation-only. To request access, send the administrator "
            "your Telegram ID:\n"
            f"<code>{user.id}</code>\n\n"
            "Once you are approved, send /start to open your dashboard."
        )
        markup = None
    if update.callback_query:
        await send_or_edit(update, text, markup)
    else:
        await update.effective_chat.send_message(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_approved
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(update, WELCOME_TEXT, reply_markup=main_menu_keyboard(update.effective_user.id))


@require_approved
async def show_my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    info = await get_user_info(uid)
    count = info["link_count"] if info else 0
    lines = ["📊 <b>Your Statistics</b>\n", f"Links created: <b>{count}</b>"]
    if info and count:
        lines.append(f"Newest link ID: <b>{info['last_id']}</b>")
        lines.append(f"Last created: {esc(info['last_created'])} UTC")
    saved = await get_saved_open_url(uid)
    lines.append(f"Saved Open URL: <code>{esc(short(saved))}</code>" if saved else "Saved Open URL: <i>not set</i>")
    await send_or_edit(update, "\n".join(lines), home_keyboard())


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()


@require_approved
async def stray_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stale / unknown buttons from older messages: just show the menu again."""
    await send_or_edit(update, WELCOME_TEXT, reply_markup=main_menu_keyboard(update.effective_user.id))


async def stray_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any text outside a conversation (including unknown commands)."""
    user = update.effective_user
    if user is None or not _is_private_chat(update):
        return
    if not await is_authorized(user.id):
        await update.effective_chat.send_message(RESTRICTED_TEXT, parse_mode=ParseMode.HTML)
        return
    await update.effective_chat.send_message(
        "Use the menu below, or /help.",
        reply_markup=main_menu_keyboard(user.id),
    )


# ==========================================================================
# SECTION 8 — Telegram: Create Link conversation
# ==========================================================================


@require_approved
async def start_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    telegram_id = update.effective_user.id
    saved = await get_saved_open_url(telegram_id)

    if saved:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Use saved open URL", callback_data="use_saved_open")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]
        )
        await send_or_edit(
            update,
            "🔗 <b>Step 1/2 — Open Link URL</b>\n\n"
            f"You have a saved open URL:\n<code>{esc(short(saved, 200))}</code>\n\n"
            "Tap the button to reuse it, or send a new URL.",
            reply_markup=keyboard,
        )
    else:
        await send_or_edit(
            update,
            "🔗 <b>Step 1/2 — Open Link URL</b>\n\n"
            "Send me the URL you want as the <b>Open Link</b> "
            "(shown to visitors as Step 1).\n\n"
            "<i>It will be saved for reuse next time.</i>",
            reply_markup=cancel_keyboard(),
        )
    return ASK_OPEN_URL


@require_approved
async def use_saved_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    saved = await get_saved_open_url(telegram_id)
    if not saved:
        await update.callback_query.answer("No saved open URL found.", show_alert=True)
        await update.callback_query.edit_message_text(
            "🔗 <b>Step 1/2 — Open Link URL</b>\n\nNo saved open URL. Please send one now.",
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return ASK_OPEN_URL

    context.user_data["open_url"] = saved
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🔓 <b>Step 2/2 — Unlock Link URL</b>\n\n"
        f"Open URL (saved): <code>{esc(short(saved, 200))}</code>\n\n"
        "Now send me the <b>Unlock Link</b> URL "
        "(revealed after visitors complete the steps).",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_UNLOCK_URL


@require_approved
async def receive_open_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clean, error = validate_url(update.message.text)
    if error:
        await update.message.reply_text(
            f"⚠️ {error}\nPlease send the Open Link URL again.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_OPEN_URL

    context.user_data["open_url"] = clean
    await update.message.reply_text(
        "🔓 <b>Step 2/2 — Unlock Link URL</b>\n\n"
        "Now send me the URL you want as the <b>Unlock Link</b> "
        "(revealed to visitors after they complete the steps).",
        reply_markup=cancel_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ASK_UNLOCK_URL


@require_approved
async def receive_unlock_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    open_url = context.user_data.get("open_url")
    if not open_url:  # state lost (restart, or access was revoked and restored)
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ This session has expired. Please start again.",
            reply_markup=main_menu_keyboard(telegram_id),
        )
        return ConversationHandler.END

    unlock_url, error = validate_url(update.message.text)
    if error:
        await update.message.reply_text(
            f"⚠️ {error}\nPlease send the Unlock Link URL again.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_UNLOCK_URL

    link_id = await create_link(telegram_id, open_url, unlock_url)
    context.user_data.clear()
    if link_id is None:  # authorization was revoked between the check and the write
        audit("create_link", telegram_id, result="denied")
        await update.message.reply_text(RESTRICTED_TEXT, parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    audit("create_link", telegram_id, target=link_id)

    await update.message.reply_text(
        f"✅ <b>Link created!</b>\n\n"
        f"<b>ID:</b> {link_id}\n"
        f"<b>Open URL:</b> <code>{esc(short(open_url, 200))}</code>\n"
        f"<b>Unlock URL:</b> <code>{esc(short(unlock_url, 200))}</code>\n\n"
        f"<b>Your link:</b>\n{esc(page_url(link_id))}\n\n"
        f"<i>Open URL saved for next time.</i>",
        reply_markup=after_create_keyboard(),
        parse_mode=ParseMode.HTML,
        **NO_PREVIEW,
    )
    return ConversationHandler.END


@require_approved
async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_or_edit(
        update,
        "❌ <b>Cancelled.</b>\n\nChoose an option below:",
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )
    return ConversationHandler.END


@require_approved
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.", reply_markup=main_menu_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


# ==========================================================================
# SECTION 9 — Telegram: Set Open URL conversation
# ==========================================================================


@require_approved
async def start_set_open_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    saved = await get_saved_open_url(update.effective_user.id)
    current = f"Current: <code>{esc(short(saved, 200))}</code>\n\n" if saved else ""
    await send_or_edit(
        update,
        "🔗 <b>Set Open URL</b>\n\n"
        f"{current}"
        "Send the URL to use as your default <b>Open Link</b>. "
        "It only affects links you create from now on, and only your own account.",
        reply_markup=cancel_keyboard(),
    )
    return SET_OPEN_URL_VALUE


@require_approved
async def receive_set_open_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    clean, error = validate_url(update.message.text)
    if error:
        await update.message.reply_text(
            f"⚠️ {error}\nPlease send a valid URL.", reply_markup=cancel_keyboard()
        )
        return SET_OPEN_URL_VALUE

    saved = await set_saved_open_url(telegram_id, clean)
    context.user_data.clear()
    if not saved:
        audit("set_open_url", telegram_id, result="denied")
        await update.message.reply_text(RESTRICTED_TEXT, parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    audit("set_open_url", telegram_id)
    await update.message.reply_text(
        f"✅ <b>Open URL saved</b>\n\n<code>{esc(short(clean, 200))}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(telegram_id),
    )
    return ConversationHandler.END


# ==========================================================================
# SECTION 10 — Telegram: My Links, Edit, Delete
# ==========================================================================


@require_approved
async def show_my_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    data = update.callback_query.data if update.callback_query else ""
    page = int(data.split(":", 1)[1]) if data.startswith("ml:") else 0

    total = await count_links_for_user(telegram_id)
    pages = max(1, math.ceil(total / LINKS_PER_PAGE))
    page = min(max(page, 0), pages - 1)

    if total == 0:
        await send_or_edit(
            update,
            "📋 <b>My Links</b>\n\nYou haven't created any links yet.",
            home_keyboard(),
        )
        return

    rows = await get_links_for_user(telegram_id, LINKS_PER_PAGE, page * LINKS_PER_PAGE)
    lines = [f"📋 <b>Your Links</b> ({total} total)\n"]
    for row in rows:
        lines.append(
            f"<b>ID {row['id']}</b>\n"
            f"🔗 Open: <code>{esc(short(row['open_url']))}</code>\n"
            f"🔓 Unlock: <code>{esc(short(row['unlock_url']))}</code>\n"
            f"🌐 Page: {esc(page_url(row['id']))}\n"
        )
    keyboard = InlineKeyboardMarkup(
        [
            nav_row("ml:", page, pages),
            [
                InlineKeyboardButton("✏️ Edit a Link", callback_data="edit_link"),
                InlineKeyboardButton("🗑 Delete a Link", callback_data="delete_link"),
            ],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
    )
    await send_or_edit(update, "\n".join(lines), reply_markup=keyboard)


@require_approved
async def start_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_or_edit(
        update,
        "✏️ <b>Edit Link</b>\n\nSend the <b>ID</b> of the link you want to edit:",
        reply_markup=cancel_keyboard(),
    )
    return EDIT_CHOOSE_LINK


@require_approved
async def edit_choose_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not (text.isascii() and text.isdigit() and len(text) <= 19):
        await update.message.reply_text(
            "⚠️ Please send a valid numeric ID.", reply_markup=cancel_keyboard()
        )
        return EDIT_CHOOSE_LINK

    link_id = int(text)
    # Ownership enforced in the query itself (WHERE id = ? AND telegram_id = ?).
    row = await get_link_for_user(link_id, update.effective_user.id)
    if row is None:
        await update.message.reply_text(
            "⚠️ Link not found or you don't own it.", reply_markup=cancel_keyboard()
        )
        return EDIT_CHOOSE_LINK

    context.user_data["edit_link_id"] = link_id
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Change Open URL", callback_data="edit_open")],
            [InlineKeyboardButton("🔓 Change Unlock URL", callback_data="edit_unlock")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
    )
    await update.message.reply_text(
        f"Editing <b>ID {link_id}</b>\n\n"
        f"Current Open: <code>{esc(short(row['open_url'], 200))}</code>\n"
        f"Current Unlock: <code>{esc(short(row['unlock_url'], 200))}</code>\n\n"
        "What do you want to change?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return EDIT_CHOOSE_FIELD


@require_approved
async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if "edit_link_id" not in context.user_data:
        context.user_data.clear()
        await query.edit_message_text(
            "⚠️ This session has expired. Please start again.",
            reply_markup=main_menu_keyboard(update.effective_user.id),
        )
        return ConversationHandler.END

    if query.data == "edit_open":
        context.user_data["edit_field"] = "open_url"
        field_name = "Open URL"
    else:
        context.user_data["edit_field"] = "unlock_url"
        field_name = "Unlock URL"

    await query.edit_message_text(
        f"Send the new <b>{field_name}</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(),
    )
    return EDIT_NEW_VALUE


@require_approved
async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    link_id = context.user_data.get("edit_link_id")
    field = context.user_data.get("edit_field")
    if link_id is None or field not in ("open_url", "unlock_url"):
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ This session has expired. Please start again.",
            reply_markup=main_menu_keyboard(telegram_id),
        )
        return ConversationHandler.END

    clean, error = validate_url(update.message.text)
    if error:
        await update.message.reply_text(f"⚠️ {error}", reply_markup=cancel_keyboard())
        return EDIT_NEW_VALUE

    success = await update_link(link_id, telegram_id, field, clean)
    context.user_data.clear()
    audit("edit_link", telegram_id, target=link_id, result="ok" if success else "denied")

    if success:
        label = "Open URL" if field == "open_url" else "Unlock URL"
        extra = "\n\n<i>Open URL also saved for reuse.</i>" if field == "open_url" else ""
        await update.message.reply_text(
            f"✅ <b>Updated!</b>\n\n"
            f"Link ID <b>{link_id}</b>\n"
            f"{label}: <code>{esc(short(clean, 200))}</code>{extra}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(telegram_id),
        )
    else:
        await update.message.reply_text(
            "⚠️ Failed to update. Link not found.",
            reply_markup=main_menu_keyboard(telegram_id),
        )
    return ConversationHandler.END


@require_approved
async def start_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_or_edit(
        update,
        "🗑 <b>Delete Link</b>\n\nSend the <b>ID</b> of the link you want to delete:",
        reply_markup=cancel_keyboard(),
    )
    return DELETE_CHOOSE_LINK


@require_approved
async def delete_choose_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not (text.isascii() and text.isdigit() and len(text) <= 19):
        await update.message.reply_text(
            "⚠️ Please send a valid numeric ID.", reply_markup=cancel_keyboard()
        )
        return DELETE_CHOOSE_LINK

    link_id = int(text)
    row = await get_link_for_user(link_id, update.effective_user.id)
    if row is None:
        await update.message.reply_text(
            "⚠️ Link not found or you don't own it.", reply_markup=cancel_keyboard()
        )
        return DELETE_CHOOSE_LINK

    context.user_data["delete_link_id"] = link_id
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Yes, delete it", callback_data="delete_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
    )
    await update.message.reply_text(
        f"Delete <b>ID {link_id}</b>?\n\n"
        f"Open: <code>{esc(short(row['open_url'], 200))}</code>\n"
        f"Unlock: <code>{esc(short(row['unlock_url'], 200))}</code>\n\n"
        "⚠️ The public page link will stop working. The ID is never reused.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return DELETE_CONFIRM


@require_approved
async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    link_id = context.user_data.get("delete_link_id")
    context.user_data.clear()
    if link_id is None:
        await send_or_edit(
            update, "⚠️ This session has expired. Please start again.", main_menu_keyboard(telegram_id)
        )
        return ConversationHandler.END

    deleted = await delete_link(link_id, telegram_id)  # ownership enforced in SQL
    audit("delete_link", telegram_id, target=link_id, result="ok" if deleted else "denied")
    await send_or_edit(
        update,
        f"✅ Link <b>ID {link_id}</b> deleted." if deleted else "⚠️ Link not found.",
        main_menu_keyboard(telegram_id),
    )
    return ConversationHandler.END


# ==========================================================================
# SECTION 11 — Telegram: administrator tools
# ==========================================================================

_STATUS_LABEL = {
    STATUS_APPROVED: "✅ approved",
    STATUS_REMOVED: "⛔ removed",
    STATUS_BANNED: "🚫 banned",
}
_INVALID_ID_TEXT = "⚠️ Invalid Telegram ID. Send the numeric user ID, e.g. <code>123456789</code>."
_OWNER_TEXT = "ℹ️ That is the owner account. It always has full access and cannot be removed or banned."


def _forget_conversation_state(context: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    """Drop any half-finished conversation data held in memory for a revoked user."""
    try:
        data = context.application.user_data.get(target)
        if data is not None:
            data.clear()
    except Exception:
        pass


async def _action_add(raw: str, admin: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    target = parse_telegram_id(raw)
    if target is None:
        return _INVALID_ID_TEXT
    result, links = await approve_user(target, admin)
    audit("adduser", admin, target, result)
    ident = f"<code>{target}</code>"
    if result == "owner":
        return _OWNER_TEXT
    if result == "already":
        return f"ℹ️ User {ident} is already approved."
    if result == "banned":
        return f"🚫 User {ident} is banned. Use /unban first, then /adduser."
    try:
        await context.bot.send_message(
            target,
            "✅ <b>You have been approved.</b>\n\nSend /start to open your dashboard.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # user may never have started the bot
        pass
    if result == "readded":
        return (
            f"✅ User {ident} re-approved.\n"
            f"Access restored to their {links} existing link(s) and saved settings."
        )
    return f"✅ User {ident} approved."


async def _action_remove(raw: str, admin: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    target = parse_telegram_id(raw)
    if target is None:
        return _INVALID_ID_TEXT
    result, links = await remove_user(target)
    audit("removeuser", admin, target, result)
    ident = f"<code>{target}</code>"
    if result == "owner":
        return _OWNER_TEXT
    if result == "unknown":
        return f"❓ Unknown user {ident}: no access record."
    if result == "banned":
        return f"🚫 User {ident} is banned (no access). Use /unban to lift the ban."
    if result == "already_removed":
        return f"ℹ️ User {ident} is already removed."
    _forget_conversation_state(context, target)
    return (
        f"⛔ Access revoked for {ident}.\n"
        f"Their {links} link(s) and saved settings are preserved and existing public links keep working. "
        f"Use /adduser to restore access."
    )


async def _action_ban(raw: str, admin: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    target = parse_telegram_id(raw)
    if target is None:
        return _INVALID_ID_TEXT
    result, links = await ban_user(target, admin)
    audit("ban", admin, target, result)
    ident = f"<code>{target}</code>"
    if result == "owner":
        return _OWNER_TEXT
    if result == "already_banned":
        return f"ℹ️ User {ident} is already banned."
    _forget_conversation_state(context, target)
    note = " (no previous record — pre-emptive ban)" if result == "banned_new" else ""
    return (
        f"🚫 User {ident} banned{note}.\n"
        f"Their {links} link(s) and settings are preserved; public links keep working."
    )


async def _action_unban(raw: str, admin: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    target = parse_telegram_id(raw)
    if target is None:
        return _INVALID_ID_TEXT
    result, _ = await unban_user(target)
    audit("unban", admin, target, result)
    ident = f"<code>{target}</code>"
    if result == "owner":
        return _OWNER_TEXT
    if result == "unknown":
        return f"❓ Unknown user {ident}: no access record."
    if result == "not_banned":
        return f"ℹ️ User {ident} is not banned."
    return f"✅ Ban lifted for {ident}. Status is now 'removed' — use /adduser to restore access."


ADMIN_ACTIONS: dict[str, Callable[..., Awaitable[str]]] = {
    "add": _action_add,
    "remove": _action_remove,
    "ban": _action_ban,
    "unban": _action_unban,
}
_ACTION_PROMPT = {
    "add": "➕ <b>Add User</b>",
    "remove": "➖ <b>Remove User</b>",
    "ban": "🚫 <b>Ban User</b>",
    "unban": "✅ <b>Unban User</b>",
}


def _command_action(action: str, usage: str) -> TgHandler:
    @require_admin
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) != 1:
            await update.effective_chat.send_message(
                f"Usage: <code>{esc(usage)}</code>", parse_mode=ParseMode.HTML
            )
            return
        text = await ADMIN_ACTIONS[action](args[0], update.effective_user.id, context)
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)

    handler.__name__ = f"{action}_command"
    return handler


adduser_command = _command_action("add", "/adduser <telegram_id>")
removeuser_command = _command_action("remove", "/removeuser <telegram_id>")
ban_command = _command_action("ban", "/ban <telegram_id>")
unban_command = _command_action("unban", "/unban <telegram_id>")


async def render_user_list(status_filter: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    if status_filter not in _USER_FILTERS:
        status_filter = "approved"
    _, total = await list_users(status_filter, 1, 0)
    pages = max(1, math.ceil(total / USERS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    rows, total = await list_users(status_filter, USERS_PER_PAGE, page * USERS_PER_PAGE)

    title = {"approved": "Approved users", "removed": "Removed users",
             "banned": "Banned users", "all": "All users"}[status_filter]
    lines = [f"👥 <b>{title}</b> ({total})\n"]
    if not rows:
        lines.append("<i>No users.</i>")
    for row in rows:
        lines.append(
            f"• <code>{row['telegram_id']}</code> — {_STATUS_LABEL[row['status']]} · "
            f"{row['link_count']} link(s)"
        )
    keyboard_rows = [nav_row(f"adm:users:{status_filter}:", page, pages)]
    keyboard_rows.append(
        [
            InlineKeyboardButton(label, callback_data=f"adm:users:{key}:0")
            for key, label in (("approved", "✅"), ("removed", "⛔"), ("banned", "🚫"), ("all", "All"))
        ]
    )
    keyboard_rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


@require_admin
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_filter, page = "approved", 0
    for arg in context.args or []:
        if arg.lower() in _USER_FILTERS:
            status_filter = arg.lower()
        elif arg.isascii() and arg.isdigit() and 0 < int(arg) < 1_000_000:
            page = int(arg) - 1
    text, markup = await render_user_list(status_filter, page)
    await update.effective_chat.send_message(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_admin
async def users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, _, status_filter, page = update.callback_query.data.split(":")
    text, markup = await render_user_list(status_filter, int(page))
    await send_or_edit(update, text, markup)


@require_admin
async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) != 1:
        await update.effective_chat.send_message("Usage: <code>/userinfo &lt;telegram_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    target = parse_telegram_id(args[0])
    if target is None:
        await update.effective_chat.send_message(_INVALID_ID_TEXT, parse_mode=ParseMode.HTML)
        return
    if target == ADMIN_ID:
        await update.effective_chat.send_message(_OWNER_TEXT)
        return
    info = await get_user_info(target)
    if info is None:
        await update.effective_chat.send_message(
            f"❓ Unknown user <code>{target}</code>: no access record, links or settings.",
            parse_mode=ParseMode.HTML,
        )
        return
    access = info["access"]
    lines = [f"👤 <b>User</b> <code>{target}</code>\n"]
    if access:
        lines += [
            f"Status: {_STATUS_LABEL[access['status']]}",
            f"Added: {esc(access['added_at'])} UTC",
            f"Updated: {esc(access['updated_at'])} UTC",
            f"Added by: <code>{esc(access['added_by'])}</code>",
        ]
    else:
        lines.append("Status: ❔ no access record (never approved)")
    lines.append(f"Links: <b>{info['link_count']}</b>")
    if info["link_count"]:
        lines.append(f"ID range: {info['first_id']}–{info['last_id']} · last created {esc(info['last_created'])} UTC")
    saved = info["saved_open_url"]
    lines.append(f"Saved Open URL: <code>{esc(short(saved))}</code>" if saved else "Saved Open URL: <i>not set</i>")
    await update.effective_chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)


def render_system_stats(stats: dict[str, Any]) -> str:
    return (
        "📈 <b>System Statistics</b>\n\n"
        f"✅ Approved users: <b>{stats[STATUS_APPROVED]}</b>\n"
        f"⛔ Removed users: <b>{stats[STATUS_REMOVED]}</b>\n"
        f"🚫 Banned users: <b>{stats[STATUS_BANNED]}</b>\n\n"
        f"🔗 Total links: <b>{stats['total_links']}</b> (by {stats['creators']} creator(s))\n"
        f"🆕 Created in last 24h: <b>{stats['links_last_24h']}</b>\n"
        f"🔢 Highest link ID: <b>{stats['last_id']}</b>\n"
        f"📦 Links whose creator has no access: <b>{stats['links_without_access']}</b>"
    )


@require_admin
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        render_system_stats(await get_system_stats()), parse_mode=ParseMode.HTML
    )


@require_admin
async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(update, render_system_stats(await get_system_stats()), home_keyboard())


async def render_all_links(page: int) -> tuple[str, InlineKeyboardMarkup]:
    _, total = await list_all_links(1, 0)
    pages = max(1, math.ceil(total / LINKS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    rows, total = await list_all_links(LINKS_PER_PAGE, page * LINKS_PER_PAGE)
    lines = [f"🗂 <b>All links</b> ({total})\n"]
    if not rows:
        lines.append("<i>No links.</i>")
    for row in rows:
        lines.append(
            f"<b>ID {row['id']}</b> · owner <code>{row['telegram_id']}</code> · {esc(row['created_at'])}\n"
            f"🔗 <code>{esc(short(row['open_url'], 60))}</code>\n"
            f"🔓 <code>{esc(short(row['unlock_url'], 60))}</code>\n"
        )
    keyboard = InlineKeyboardMarkup(
        [nav_row("adm:links:", page, pages), [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
    )
    return "\n".join(lines), keyboard


@require_admin
async def alllinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args or ["1"])[0]
    page = int(arg) - 1 if arg.isascii() and arg.isdigit() and 0 < int(arg) < 1_000_000 else 0
    text, markup = await render_all_links(page)
    await update.effective_chat.send_message(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_admin
async def alllinks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await render_all_links(int(update.callback_query.data.rsplit(":", 1)[1]))
    await send_or_edit(update, text, markup)


@require_admin
async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Add User", callback_data="adm:ask:add"),
                InlineKeyboardButton("➖ Remove User", callback_data="adm:ask:remove"),
            ],
            [
                InlineKeyboardButton("🚫 Ban User", callback_data="adm:ask:ban"),
                InlineKeyboardButton("✅ Unban User", callback_data="adm:ask:unban"),
            ],
            [InlineKeyboardButton("📃 All Users", callback_data="adm:users:all:0")],
            [InlineKeyboardButton("🗂 All Links", callback_data="adm:links:0")],
            [InlineKeyboardButton("📈 System Statistics", callback_data="adm:stats")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
    )
    await send_or_edit(update, "👥 <b>Manage Users</b>", keyboard)


@require_admin
async def admin_ask_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.callback_query.data.rsplit(":", 1)[1]
    if action not in ADMIN_ACTIONS:
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["admin_action"] = action
    await send_or_edit(
        update,
        f"{_ACTION_PROMPT[action]}\n\nSend the user's numeric Telegram ID:",
        cancel_keyboard(),
    )
    return ADMIN_ASK_ID


@require_admin
async def admin_receive_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = context.user_data.get("admin_action")
    if action not in ADMIN_ACTIONS:
        context.user_data.clear()
        return ConversationHandler.END
    text = await ADMIN_ACTIONS[action](update.message.text, update.effective_user.id, context)
    context.user_data.clear()
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


# ==========================================================================
# SECTION 12 — Telegram: error handling & application wiring
# ==========================================================================


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat is not None:
        try:
            await update.effective_chat.send_message("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


def build_bot_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    common_fallbacks = [
        CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
        CommandHandler("cancel", cancel_command),
        CommandHandler("start", start_command),
    ]
    text_only = filters.TEXT & ~filters.COMMAND

    create_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_create, pattern="^create_new$")],
        states={
            ASK_OPEN_URL: [
                MessageHandler(text_only, receive_open_url),
                CallbackQueryHandler(use_saved_open, pattern="^use_saved_open$"),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            ASK_UNLOCK_URL: [
                MessageHandler(text_only, receive_unlock_url),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=common_fallbacks,
        name="create_link_conversation",
        allow_reentry=True,
        per_message=False,
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_edit, pattern="^edit_link$")],
        states={
            EDIT_CHOOSE_LINK: [
                MessageHandler(text_only, edit_choose_link),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            EDIT_CHOOSE_FIELD: [
                CallbackQueryHandler(edit_choose_field, pattern="^edit_(open|unlock)$"),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            EDIT_NEW_VALUE: [
                MessageHandler(text_only, edit_new_value),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=common_fallbacks,
        name="edit_link_conversation",
        allow_reentry=True,
        per_message=False,
    )

    delete_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_delete, pattern="^delete_link$")],
        states={
            DELETE_CHOOSE_LINK: [
                MessageHandler(text_only, delete_choose_link),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            DELETE_CONFIRM: [
                CallbackQueryHandler(delete_confirm, pattern="^delete_confirm$"),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=common_fallbacks,
        name="delete_link_conversation",
        allow_reentry=True,
        per_message=False,
    )

    open_url_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_set_open_url, pattern="^set_open_url$")],
        states={
            SET_OPEN_URL_VALUE: [
                MessageHandler(text_only, receive_set_open_url),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=common_fallbacks,
        name="set_open_url_conversation",
        allow_reentry=True,
        per_message=False,
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_ask_id, pattern=r"^adm:ask:(add|remove|ban|unban)$")],
        states={
            ADMIN_ASK_ID: [
                MessageHandler(text_only, admin_receive_id),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=common_fallbacks,
        name="admin_user_conversation",
        allow_reentry=True,
        per_message=False,
    )

    # Public commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_handler))
    # Admin commands (each handler is wrapped with require_admin)
    application.add_handler(CommandHandler("adduser", adduser_command))
    application.add_handler(CommandHandler("removeuser", removeuser_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("userinfo", userinfo_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("alllinks", alllinks_command))
    # Conversations
    for conv in (create_conv, edit_conv, delete_conv, open_url_conv, admin_conv):
        application.add_handler(conv)
    # Buttons
    application.add_handler(CallbackQueryHandler(show_my_links, pattern=r"^(my_links|ml:\d{1,6})$"))
    application.add_handler(CallbackQueryHandler(show_main_menu, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(show_my_stats, pattern="^my_stats$"))
    application.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))
    application.add_handler(CallbackQueryHandler(admin_menu_callback, pattern="^adm:menu$"))
    application.add_handler(
        CallbackQueryHandler(users_callback, pattern=r"^adm:users:(approved|removed|banned|all):\d{1,6}$")
    )
    application.add_handler(CallbackQueryHandler(stats_callback, pattern="^adm:stats$"))
    application.add_handler(CallbackQueryHandler(alllinks_callback, pattern=r"^adm:links:\d{1,6}$"))
    # Catch-alls (must stay last)
    application.add_handler(CallbackQueryHandler(stray_callback))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, stray_message))
    application.add_error_handler(on_error)

    return application


# ==========================================================================
# SECTION 13 — Entrypoint: run FastAPI + Telegram bot concurrently
# ==========================================================================


async def run() -> None:
    validate_config()
    await init_db()

    application = build_bot_application()
    uvicorn_config = uvicorn.Config(api, host=API_HOST, port=API_PORT, log_level="info")
    server = uvicorn.Server(uvicorn_config)

    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started (owner ID configured).")
        logger.info("Starting API server on http://%s:%s", API_HOST, API_PORT)
        try:
            await server.serve()
        finally:
            logger.info("Shutting down…")
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting.")
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(1)
