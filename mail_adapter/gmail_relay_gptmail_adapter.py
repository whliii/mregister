from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import os
import random
import re
import secrets
import sqlite3
import ssl
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import socks
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field


APP_TITLE = "Gmail Relay GPTMail Adapter"
APP_VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(
    os.getenv("ADAPTER_DB_PATH", str(Path(tempfile.gettempdir()) / "mregister-gmail-relay" / "adapter.db"))
).expanduser()

ADAPTER_API_KEY = (os.getenv("ADAPTER_API_KEY") or "").strip()
ADAPTER_ADMIN_TOKEN = (os.getenv("ADAPTER_ADMIN_TOKEN") or ADAPTER_API_KEY).strip()
ADMIN_USERNAME = (os.getenv("ADAPTER_WEB_USERNAME") or "admin").strip()
ADMIN_PASSWORD = (os.getenv("ADAPTER_WEB_PASSWORD") or "change-this-password").strip()
SYNC_INTERVAL_SECONDS = max(5, int(os.getenv("ADAPTER_SYNC_INTERVAL_SECONDS", "12") or 12))
MESSAGE_RETENTION_MINUTES = max(1, int(os.getenv("ADAPTER_MESSAGE_RETENTION_MINUTES", "120") or 120))
MAILBOX_RETENTION_HOURS = max(1, int(os.getenv("ADAPTER_MAILBOX_RETENTION_HOURS", "24") or 24))
CLEANUP_INTERVAL_SECONDS = max(60, int(os.getenv("ADAPTER_CLEANUP_INTERVAL_SECONDS", "300") or 300))
DELETE_PROCESSED_GMAIL_MESSAGES = (
    str(os.getenv("ADAPTER_DELETE_PROCESSED_GMAIL_MESSAGES", "1")).strip().lower() in {"1", "true", "yes", "y", "on"}
)
GMAIL_PURGE_BATCH_SIZE = max(0, int(os.getenv("ADAPTER_GMAIL_PURGE_BATCH_SIZE", "200") or 200))
SESSION_COOKIE = "adapter_admin_session"
DOMAIN_ROUND_ROBIN_STATE_KEY = "domain_round_robin_last_id"

LOCAL_PART_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,62}$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,62}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
HEADER_CANDIDATES = [
    "Delivered-To",
    "X-Original-To",
    "Original-Recipient",
    "Envelope-To",
    "X-Envelope-To",
    "X-Forwarded-To",
    "To",
    "Cc",
]

db_lock = threading.RLock()
session_lock = threading.RLock()
sync_lock = threading.Lock()
admin_sessions: dict[str, str] = {}
stop_event = threading.Event()
sync_thread: threading.Thread | None = None
last_cleanup_started_at: str | None = None

RUNTIME_SETTING_DEFAULTS = {
    "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
    "message_retention_minutes": MESSAGE_RETENTION_MINUTES,
    "mailbox_retention_minutes": MAILBOX_RETENTION_HOURS * 60,
    "cleanup_interval_seconds": CLEANUP_INTERVAL_SECONDS,
}


class AdapterError(RuntimeError):
    pass


class GenerateEmailPayload(BaseModel):
    domain: str | None = None


class GmailInboxCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email_address: str = Field(min_length=3, max_length=254)
    app_password: str = Field(min_length=8, max_length=256)
    imap_host: str = Field(default="imap.gmail.com", min_length=1, max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_proxy_url: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    notes: str | None = None


class GmailInboxUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email_address: str | None = Field(default=None, min_length=3, max_length=254)
    app_password: str | None = Field(default=None, min_length=8, max_length=256)
    imap_host: str | None = Field(default=None, min_length=1, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_proxy_url: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    notes: str | None = None


class DomainCreatePayload(BaseModel):
    domain: str
    gmail_inbox_id: int
    notes: str | None = None
    enabled: bool = True


class DomainUpdatePayload(BaseModel):
    gmail_inbox_id: int | None = None
    notes: str | None = None
    enabled: bool | None = None


class SyncPayload(BaseModel):
    gmail_inbox_id: int | None = None


class RuntimeSettingsPayload(BaseModel):
    sync_interval_seconds: int = Field(default=SYNC_INTERVAL_SECONDS, ge=5, le=3600)
    message_retention_minutes: int = Field(default=MESSAGE_RETENTION_MINUTES, ge=1, le=10080)
    mailbox_retention_minutes: int = Field(default=MAILBOX_RETENTION_HOURS * 60, ge=1, le=43200)
    cleanup_interval_seconds: int = Field(default=CLEANUP_INTERVAL_SECONDS, ge=60, le=86400)


app = FastAPI(title=APP_TITLE, version=APP_VERSION)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_local() -> datetime:
    return datetime.now().replace(microsecond=0)


def parse_iso(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


@app.exception_handler(AdapterError)
def _handle_adapter_error(_: Request, exc: AdapterError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


class ProxyIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str = "", port: int = imaplib.IMAP4_SSL_PORT, *, proxy_url: str | None = None, timeout: float | None = None):
        self._proxy_url = proxy_url
        super().__init__(host=host, port=port, timeout=timeout)

    def open(self, host: str = "", port: int = imaplib.IMAP4_SSL_PORT, timeout: float | None = None):
        proxy = _proxy_settings(self._proxy_url)
        if proxy is None:
            return super().open(host, port, timeout)
        self.host = host
        self.port = port
        sock = socks.socksocket()
        if timeout is not None:
            sock.settimeout(timeout)
        sock.set_proxy(**proxy)
        sock.connect((host, port))
        self.sock = self.ssl_context.wrap_socket(sock, server_hostname=host)
        self.file = self.sock.makefile("rb")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _secret_key_bytes() -> bytes:
    raw = (os.getenv("ADAPTER_SECRET_KEY") or "").strip()
    if raw:
        try:
            Fernet(raw.encode("utf-8"))
            return raw.encode("utf-8")
        except Exception as exc:
            raise RuntimeError("ADAPTER_SECRET_KEY is invalid. Expected a Fernet key.") from exc
    seed = "|".join(
        [
            ADAPTER_API_KEY or "mregister-mail",
            ADAPTER_ADMIN_TOKEN or "admin",
            ADMIN_USERNAME or "user",
            str(DB_PATH),
        ]
    ).encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return base64.urlsafe_b64encode(digest)


FERNET = Fernet(_secret_key_bytes())


def encrypt_secret(value: str) -> str:
    return FERNET.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return FERNET.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise AdapterError("Stored inbox secret cannot be decrypted") from exc


def _validate_domain_or_raise(domain: str) -> str:
    normalized = domain.strip().lower()
    if not DOMAIN_RE.match(normalized):
        raise AdapterError("Invalid email domain")
    return normalized


def _generate_local_part() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return random.choice(alphabet[:26]) + "".join(random.choice(alphabet) for _ in range(9))


def _normalize_proxy_url(proxy_url: str | None) -> str | None:
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "socks4", "socks4a", "socks5", "socks5h"}:
        raise AdapterError("Unsupported proxy scheme")
    if not parsed.hostname or not parsed.port:
        raise AdapterError("Proxy URL must include host and port")
    return raw


def _proxy_settings(proxy_url: str | None) -> dict[str, Any] | None:
    normalized = _normalize_proxy_url(proxy_url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    scheme = parsed.scheme.lower()
    proxy_type = {
        "http": socks.HTTP,
        "socks4": socks.SOCKS4,
        "socks4a": socks.SOCKS4,
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
    }[scheme]
    rdns = scheme in {"socks4a", "socks5h"}
    return {
        "proxy_type": proxy_type,
        "addr": parsed.hostname,
        "port": parsed.port,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "rdns": rdns,
    }


def _validate_email(email: str) -> bool:
    if "@" not in email:
        return False
    local, domain = email.split("@", 1)
    return bool(LOCAL_PART_RE.fullmatch(local) and DOMAIN_RE.fullmatch(domain))


def _encode_message_id(email: str, message_id: str) -> str:
    encoded_email = base64.urlsafe_b64encode(email.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded_email}.{message_id}"


def _decode_message_id(composite_id: str) -> tuple[str, str]:
    if "." not in composite_id:
        raise AdapterError("Invalid email id format")
    encoded_email, message_id = composite_id.split(".", 1)
    padded = encoded_email + "=" * ((4 - len(encoded_email) % 4) % 4)
    try:
        email = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise AdapterError("Invalid email id encoding") from exc
    if not _validate_email(email):
        raise AdapterError("Invalid email in id")
    return email, message_id


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_lock, closing(_connect()) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS gmail_inboxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                email_address TEXT NOT NULL,
                app_password_enc TEXT NOT NULL,
                imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL,
                imap_proxy_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_sync_at TEXT,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                gmail_inbox_id INTEGER NOT NULL,
                notes TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(gmail_inbox_id) REFERENCES gmail_inboxes(id)
            );

            CREATE TABLE IF NOT EXISTS generated_mailboxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                local_part TEXT NOT NULL,
                domain_id INTEGER NOT NULL,
                gmail_inbox_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_matched_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(domain_id) REFERENCES domains(id),
                FOREIGN KEY(gmail_inbox_id) REFERENCES gmail_inboxes(id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_id INTEGER NOT NULL,
                gmail_inbox_id INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                header_message_id TEXT,
                from_addr TEXT,
                to_addr TEXT,
                subject TEXT,
                text_body TEXT,
                html_body TEXT,
                raw_headers_json TEXT,
                raw_source TEXT,
                received_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(gmail_inbox_id, external_id),
                FOREIGN KEY(mailbox_id) REFERENCES generated_mailboxes(id),
                FOREIGN KEY(gmail_inbox_id) REFERENCES gmail_inboxes(id)
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                gmail_inbox_id INTEGER PRIMARY KEY,
                last_uid INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(gmail_inbox_id) REFERENCES gmail_inboxes(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _ensure_columns(conn, "gmail_inboxes", {"imap_proxy_url": "TEXT"})
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            _migrate_domains_table(conn)
            _migrate_generated_mailboxes_table(conn)
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()


def _migrate_domains_table(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)").fetchall()}
    target_columns = {
        "id",
        "domain",
        "gmail_inbox_id",
        "notes",
        "enabled",
        "created_at",
        "updated_at",
    }
    if columns == target_columns:
        return
    conn.execute("ALTER TABLE domains RENAME TO domains_legacy")
    conn.execute(
        """
        CREATE TABLE domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL UNIQUE,
            gmail_inbox_id INTEGER NOT NULL,
            notes TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(gmail_inbox_id) REFERENCES gmail_inboxes(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO domains (id, domain, gmail_inbox_id, notes, enabled, created_at, updated_at)
        SELECT id, domain, gmail_inbox_id, notes, enabled, created_at, updated_at
        FROM domains_legacy
        """
    )
    conn.execute("DROP TABLE domains_legacy")


def _migrate_generated_mailboxes_table(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_mailboxes)").fetchall()}
    target_columns = {
        "id",
        "email",
        "local_part",
        "domain_id",
        "gmail_inbox_id",
        "created_at",
        "last_checked_at",
        "last_matched_at",
        "active",
    }
    if columns == target_columns:
        return
    conn.execute("ALTER TABLE generated_mailboxes RENAME TO generated_mailboxes_legacy")
    conn.execute(
        """
        CREATE TABLE generated_mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            local_part TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            gmail_inbox_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            last_matched_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(domain_id) REFERENCES domains(id),
            FOREIGN KEY(gmail_inbox_id) REFERENCES gmail_inboxes(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO generated_mailboxes (
            id, email, local_part, domain_id, gmail_inbox_id,
            created_at, last_checked_at, last_matched_at, active
        )
        SELECT
            id, email, local_part, domain_id, gmail_inbox_id,
            created_at, last_checked_at, last_matched_at, active
        FROM generated_mailboxes_legacy
        """
    )
    conn.execute("DROP TABLE generated_mailboxes_legacy")


def row_to_gmail_inbox(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "email_address": str(row["email_address"]),
        "imap_host": str(row["imap_host"]),
        "imap_port": int(row["imap_port"]),
        "imap_proxy_url": row["imap_proxy_url"],
        "enabled": bool(int(row["enabled"])),
        "notes": row["notes"],
        "has_password": bool(row["app_password_enc"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "last_sync_at": row["last_sync_at"],
        "last_error": row["last_error"],
    }


def row_to_domain(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "domain": str(row["domain"]),
        "gmail_inbox_id": int(row["gmail_inbox_id"]),
        "gmail_inbox_name": row["gmail_inbox_name"],
        "notes": row["notes"],
        "enabled": bool(int(row["enabled"])),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def row_to_mailbox(row: sqlite3.Row) -> dict[str, Any]:
    item = {
        "id": int(row["id"]),
        "email": str(row["email"]),
        "domain_id": int(row["domain_id"]),
        "gmail_inbox_id": int(row["gmail_inbox_id"]),
        "created_at": str(row["created_at"]),
        "last_checked_at": row["last_checked_at"],
        "last_matched_at": row["last_matched_at"],
    }
    if "active" in row.keys():
        item["active"] = bool(int(row["active"]))
    return item


def row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    headers: Any = None
    if row["raw_headers_json"]:
        try:
            headers = json.loads(str(row["raw_headers_json"]))
        except json.JSONDecodeError:
            headers = row["raw_headers_json"]
    email_address = str(row["email"])
    return {
        "id": _encode_message_id(email_address, f"local-{int(row['id'])}"),
        "mail_id": f"local-{int(row['id'])}",
        "email": email_address,
        "provider": "gmail-relay",
        "message_id": row["header_message_id"],
        "from": row["from_addr"],
        "subject": row["subject"],
        "body": row["text_body"] or row["html_body"] or "",
        "text_body": row["text_body"],
        "html_body": row["html_body"],
        "raw": row["raw_source"],
        "headers": headers,
        "received_at": row["received_at"],
        "created_at": row["created_at"],
    }


def get_runtime_settings() -> dict[str, int]:
    settings = dict(RUNTIME_SETTING_DEFAULTS)
    settings["mailbox_retention_minutes"] = MAILBOX_RETENTION_HOURS * 60
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        key = str(row["key"])
        if key not in settings and key != "mailbox_retention_hours":
            continue
        try:
            value = int(str(row["value"]))
        except Exception:
            continue
        if key == "mailbox_retention_hours":
            settings["mailbox_retention_minutes"] = max(1, value) * 60
            continue
        settings[key] = max(1, value)
    settings["sync_interval_seconds"] = max(5, settings["sync_interval_seconds"])
    settings["message_retention_minutes"] = max(1, settings["message_retention_minutes"])
    settings["mailbox_retention_minutes"] = max(1, settings["mailbox_retention_minutes"])
    settings["cleanup_interval_seconds"] = max(60, settings["cleanup_interval_seconds"])
    return settings


def _get_state_int(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return int(str(row["value"]))
    except Exception:
        return None


def _set_state_value(conn: sqlite3.Connection, key: str, value: int | str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value), now_iso()),
    )


def update_runtime_settings(payload: RuntimeSettingsPayload) -> dict[str, int]:
    timestamp = now_iso()
    values = payload.model_dump()
    with db_lock, closing(_connect()) as conn:
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, str(int(value)), timestamp),
            )
        conn.commit()
    return get_runtime_settings()


def get_all_gmail_inboxes() -> list[dict[str, Any]]:
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM gmail_inboxes ORDER BY enabled DESC, id ASC").fetchall()
    return [row_to_gmail_inbox(row) for row in rows]


def get_all_domains() -> list[dict[str, Any]]:
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT d.*, g.name AS gmail_inbox_name
            FROM domains d
            JOIN gmail_inboxes g ON g.id = d.gmail_inbox_id
            ORDER BY d.enabled DESC, d.id ASC
            """
        ).fetchall()
    return [row_to_domain(row) for row in rows]


def get_enabled_domains() -> list[dict[str, Any]]:
    return [item for item in get_all_domains() if item["enabled"]]


def _require_api_key(x_api_key: str | None) -> None:
    if not ADAPTER_API_KEY:
        raise HTTPException(status_code=500, detail="ADAPTER_API_KEY is not configured")
    if (x_api_key or "").strip() != ADAPTER_API_KEY:
        raise HTTPException(status_code=401, detail="API key is invalid")


def _create_admin_session() -> str:
    token = secrets.token_urlsafe(24)
    with session_lock:
        admin_sessions[token] = ADMIN_USERNAME
    return token


def _is_admin_session(request: Request) -> bool:
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if not token:
        return False
    with session_lock:
        return token in admin_sessions


def _clear_admin_session(request: Request) -> None:
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if not token:
        return
    with session_lock:
        admin_sessions.pop(token, None)


def _require_admin_access(request: Request, x_admin_token: str | None) -> None:
    if x_admin_token and (x_admin_token or "").strip() == ADAPTER_ADMIN_TOKEN:
        return
    if _is_admin_session(request):
        return
    raise HTTPException(status_code=401, detail="Admin token is invalid")


def create_gmail_inbox(payload: GmailInboxCreatePayload) -> dict[str, Any]:
    timestamp = now_iso()
    email_address = payload.email_address.strip().lower()
    if "@" not in email_address:
        raise AdapterError("Invalid Gmail address")
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT id FROM gmail_inboxes WHERE name = ?", (payload.name.strip(),)).fetchone()
        if row is not None:
            raise AdapterError("邮箱名称已存在")
        cursor = conn.execute(
            """
            INSERT INTO gmail_inboxes (
                name, email_address, app_password_enc, imap_host, imap_port, imap_proxy_url,
                enabled, notes, created_at, updated_at, last_sync_at, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                payload.name.strip(),
                email_address,
                encrypt_secret(payload.app_password.strip()),
                payload.imap_host.strip(),
                payload.imap_port,
                _normalize_proxy_url(payload.imap_proxy_url),
                1 if payload.enabled else 0,
                (payload.notes or "").strip() or None,
                timestamp,
                timestamp,
            ),
        )
        inbox_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO sync_state (gmail_inbox_id, last_uid, updated_at) VALUES (?, 0, ?)",
            (inbox_id, timestamp),
        )
        conn.commit()
        created = conn.execute("SELECT * FROM gmail_inboxes WHERE id = ?", (inbox_id,)).fetchone()
    if created is None:
        raise AdapterError("创建 Gmail 邮箱失败")
    return row_to_gmail_inbox(created)


def update_gmail_inbox(inbox_id: int, payload: GmailInboxUpdatePayload) -> dict[str, Any]:
    timestamp = now_iso()
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM gmail_inboxes WHERE id = ?", (inbox_id,)).fetchone()
        if row is None:
            raise AdapterError("Gmail 邮箱不存在")
        name = row["name"] if payload.name is None else payload.name.strip()
        email_address = row["email_address"] if payload.email_address is None else payload.email_address.strip().lower()
        imap_host = row["imap_host"] if payload.imap_host is None else payload.imap_host.strip()
        imap_port = int(row["imap_port"]) if payload.imap_port is None else payload.imap_port
        imap_proxy_url = row["imap_proxy_url"] if payload.imap_proxy_url is None else _normalize_proxy_url(payload.imap_proxy_url)
        enabled = int(row["enabled"]) if payload.enabled is None else (1 if payload.enabled else 0)
        notes = row["notes"] if payload.notes is None else ((payload.notes or "").strip() or None)
        app_password_enc = str(row["app_password_enc"])
        if payload.app_password:
            app_password_enc = encrypt_secret(payload.app_password.strip())
        conflict = conn.execute("SELECT id FROM gmail_inboxes WHERE name = ? AND id != ?", (name, inbox_id)).fetchone()
        if conflict is not None:
            raise AdapterError("邮箱名称已存在")
        conn.execute(
            """
            UPDATE gmail_inboxes
            SET name = ?, email_address = ?, app_password_enc = ?, imap_host = ?, imap_port = ?, imap_proxy_url = ?,
                enabled = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, email_address, app_password_enc, imap_host, imap_port, imap_proxy_url, enabled, notes, timestamp, inbox_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM gmail_inboxes WHERE id = ?", (inbox_id,)).fetchone()
    if updated is None:
        raise AdapterError("更新后未找到 Gmail 邮箱")
    return row_to_gmail_inbox(updated)


def delete_gmail_inbox(inbox_id: int) -> None:
    with db_lock, closing(_connect()) as conn:
        in_use = conn.execute("SELECT id FROM domains WHERE gmail_inbox_id = ? LIMIT 1", (inbox_id,)).fetchone()
        if in_use is not None:
            raise AdapterError("该 Gmail 邮箱仍被域名配置使用")
        conn.execute("DELETE FROM sync_state WHERE gmail_inbox_id = ?", (inbox_id,))
        conn.execute("DELETE FROM gmail_inboxes WHERE id = ?", (inbox_id,))
        conn.commit()


def create_domain(payload: DomainCreatePayload) -> dict[str, Any]:
    domain = _validate_domain_or_raise(payload.domain)
    timestamp = now_iso()
    with db_lock, closing(_connect()) as conn:
        inbox = conn.execute("SELECT id FROM gmail_inboxes WHERE id = ?", (payload.gmail_inbox_id,)).fetchone()
        if inbox is None:
            raise AdapterError("Gmail 邮箱不存在")
        existing = conn.execute("SELECT id FROM domains WHERE domain = ?", (domain,)).fetchone()
        if existing is not None:
            raise AdapterError("域名已存在")
        cursor = conn.execute(
            """
            INSERT INTO domains (domain, gmail_inbox_id, notes, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                domain,
                payload.gmail_inbox_id,
                (payload.notes or "").strip() or None,
                1 if payload.enabled else 0,
                timestamp,
                timestamp,
            ),
        )
        domain_id = int(cursor.lastrowid)
        conn.commit()
        row = conn.execute(
            """
            SELECT d.*, g.name AS gmail_inbox_name
            FROM domains d
            JOIN gmail_inboxes g ON g.id = d.gmail_inbox_id
            WHERE d.id = ?
            """,
            (domain_id,),
        ).fetchone()
    if row is None:
        raise AdapterError("创建域名失败")
    return row_to_domain(row)


def update_domain(domain_id: int, payload: DomainUpdatePayload) -> dict[str, Any]:
    timestamp = now_iso()
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if row is None:
            raise AdapterError("域名不存在")
        gmail_inbox_id = int(row["gmail_inbox_id"]) if payload.gmail_inbox_id is None else payload.gmail_inbox_id
        inbox = conn.execute("SELECT id FROM gmail_inboxes WHERE id = ?", (gmail_inbox_id,)).fetchone()
        if inbox is None:
            raise AdapterError("Gmail 邮箱不存在")
        notes = row["notes"] if payload.notes is None else ((payload.notes or "").strip() or None)
        enabled = int(row["enabled"]) if payload.enabled is None else (1 if payload.enabled else 0)
        conn.execute(
            """
            UPDATE domains
            SET gmail_inbox_id = ?, notes = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (gmail_inbox_id, notes, enabled, timestamp, domain_id),
        )
        conn.commit()
        updated = conn.execute(
            """
            SELECT d.*, g.name AS gmail_inbox_name
            FROM domains d
            JOIN gmail_inboxes g ON g.id = d.gmail_inbox_id
            WHERE d.id = ?
            """,
            (domain_id,),
        ).fetchone()
    if updated is None:
        raise AdapterError("更新后未找到域名")
    return row_to_domain(updated)


def delete_domain(domain_id: int) -> None:
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT id FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if row is None:
            raise AdapterError("域名不存在")
        conn.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
        conn.commit()


def choose_domain(requested_domain: str | None) -> sqlite3.Row:
    with db_lock, closing(_connect()) as conn:
        if requested_domain:
            domain = _validate_domain_or_raise(requested_domain)
            row = conn.execute(
                """
                SELECT d.*, g.enabled AS inbox_enabled
                FROM domains d
                JOIN gmail_inboxes g ON g.id = d.gmail_inbox_id
                WHERE d.domain = ?
                """,
                (domain,),
            ).fetchone()
            if row is None or not int(row["enabled"]) or not int(row["inbox_enabled"]):
                raise AdapterError("域名不可用")
            return row
        rows = conn.execute(
            """
            SELECT d.*, g.enabled AS inbox_enabled
            FROM domains d
            JOIN gmail_inboxes g ON g.id = d.gmail_inbox_id
            WHERE d.enabled = 1 AND g.enabled = 1
            ORDER BY d.id ASC
            """
        ).fetchall()
        if not rows:
            raise AdapterError("No enabled domain is configured")
        last_domain_id = _get_state_int(conn, DOMAIN_ROUND_ROBIN_STATE_KEY)
        selected = None
        if last_domain_id is not None:
            for row in rows:
                if int(row["id"]) > last_domain_id:
                    selected = row
                    break
        if selected is None:
            selected = rows[0]
        _set_state_value(conn, DOMAIN_ROUND_ROBIN_STATE_KEY, int(selected["id"]))
        conn.commit()
        return selected


def get_mailbox_by_email(email: str) -> sqlite3.Row | None:
    with db_lock, closing(_connect()) as conn:
        return conn.execute("SELECT * FROM generated_mailboxes WHERE email = ? AND active = 1", (email,)).fetchone()


def _compose_local_part(local_part: str) -> str:
    local_part = str(local_part or "").strip().lower()
    if len(local_part) > 64:
        local_part = local_part[:64]
    if not LOCAL_PART_RE.fullmatch(local_part):
        raise AdapterError("Generated local part is invalid")
    return local_part


def create_mailbox(requested_domain: str | None) -> dict[str, Any]:
    domain_row = choose_domain(requested_domain)
    with db_lock, closing(_connect()) as conn:
        for _ in range(12):
            local_part = _compose_local_part(_generate_local_part())
            email = f"{local_part}@{str(domain_row['domain'])}"
            existing = conn.execute("SELECT * FROM generated_mailboxes WHERE email = ?", (email,)).fetchone()
            if existing is not None:
                continue
            timestamp = now_iso()
            cursor = conn.execute(
                """
                INSERT INTO generated_mailboxes (
                    email, local_part, domain_id, gmail_inbox_id,
                    created_at, last_checked_at, last_matched_at, active
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL, 1)
                """,
                (
                    email,
                    local_part,
                    int(domain_row["id"]),
                    int(domain_row["gmail_inbox_id"]),
                    timestamp,
                ),
            )
            conn.commit()
            created = conn.execute("SELECT * FROM generated_mailboxes WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
            if created is not None:
                return row_to_mailbox(created)
    raise AdapterError("Failed to generate mailbox")


def list_admin_mailboxes(search: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    query = """
        SELECT
            mb.*,
            d.domain,
            g.name AS gmail_inbox_name,
            COUNT(msg.id) AS message_count,
            MAX(msg.received_at) AS latest_received_at
        FROM generated_mailboxes mb
        JOIN domains d ON d.id = mb.domain_id
        JOIN gmail_inboxes g ON g.id = mb.gmail_inbox_id
        LEFT JOIN messages msg ON msg.mailbox_id = mb.id
    """
    params: list[Any] = []
    if search and search.strip():
        query += " WHERE mb.email LIKE ? OR g.name LIKE ?"
        needle = f"%{search.strip().lower()}%"
        params.extend([needle, needle])
    query += """
        GROUP BY mb.id, d.domain, g.name
        ORDER BY mb.id DESC
        LIMIT ?
    """
    params.append(max(1, min(limit, 500)))
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_mailbox(row)
        item["domain"] = str(row["domain"])
        item["gmail_inbox_name"] = str(row["gmail_inbox_name"])
        item["message_count"] = int(row["message_count"] or 0)
        item["latest_received_at"] = row["latest_received_at"]
        items.append(item)
    return items


def list_messages_for_email(email: str) -> list[dict[str, Any]]:
    if not _validate_email(email):
        raise AdapterError("Invalid email")
    mailbox = get_mailbox_by_email(email)
    if mailbox is None:
        return []
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT msg.*, mb.email
            FROM messages msg
            JOIN generated_mailboxes mb ON mb.id = msg.mailbox_id
            WHERE mb.email = ?
            ORDER BY msg.id DESC
            """,
            (email,),
        ).fetchall()
        conn.execute("UPDATE generated_mailboxes SET last_checked_at = ? WHERE id = ?", (now_iso(), int(mailbox["id"])))
        conn.commit()
    return [row_to_message(row) for row in rows]


def list_admin_messages(mailbox_id: int, limit: int = 100) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with db_lock, closing(_connect()) as conn:
        mailbox = conn.execute(
            """
            SELECT
                mb.*,
                d.domain,
                g.name AS gmail_inbox_name
            FROM generated_mailboxes mb
            JOIN domains d ON d.id = mb.domain_id
            JOIN gmail_inboxes g ON g.id = mb.gmail_inbox_id
            WHERE mb.id = ?
            """,
            (mailbox_id,),
        ).fetchone()
        if mailbox is None:
            raise AdapterError("临时邮箱不存在")
        rows = conn.execute(
            """
            SELECT msg.*, mb.email
            FROM messages msg
            JOIN generated_mailboxes mb ON mb.id = msg.mailbox_id
            WHERE msg.mailbox_id = ?
            ORDER BY msg.id DESC
            LIMIT ?
            """,
            (mailbox_id, max(1, min(limit, 500))),
        ).fetchall()
    mailbox_item = row_to_mailbox(mailbox)
    mailbox_item["domain"] = str(mailbox["domain"])
    mailbox_item["gmail_inbox_name"] = str(mailbox["gmail_inbox_name"])
    return mailbox_item, [row_to_message(row) for row in rows]


def get_message_detail(email: str, raw_id: str) -> dict[str, Any]:
    if not raw_id.startswith("local-") or not raw_id[6:].isdigit():
        raise AdapterError("Invalid message id")
    row_id = int(raw_id[6:])
    with db_lock, closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT msg.*, mb.email
            FROM messages msg
            JOIN generated_mailboxes mb ON mb.id = msg.mailbox_id
            WHERE msg.id = ? AND mb.email = ?
            """,
            (row_id, email),
        ).fetchone()
    if row is None:
        raise AdapterError("邮件不存在")
    return row_to_message(row)


def delete_message(email: str, raw_id: str) -> None:
    if not raw_id.startswith("local-") or not raw_id[6:].isdigit():
        raise AdapterError("Invalid message id")
    row_id = int(raw_id[6:])
    with db_lock, closing(_connect()) as conn:
        conn.execute(
            "DELETE FROM messages WHERE id = ? AND mailbox_id IN (SELECT id FROM generated_mailboxes WHERE email = ?)",
            (row_id, email),
        )
        conn.commit()


def clear_mailbox(email: str) -> None:
    with db_lock, closing(_connect()) as conn:
        mailbox = conn.execute("SELECT id FROM generated_mailboxes WHERE email = ?", (email,)).fetchone()
        if mailbox is None:
            return
        conn.execute("DELETE FROM messages WHERE mailbox_id = ?", (int(mailbox["id"]),))
        conn.commit()


def clear_mailbox_by_id(mailbox_id: int) -> None:
    with db_lock, closing(_connect()) as conn:
        mailbox = conn.execute("SELECT id FROM generated_mailboxes WHERE id = ?", (mailbox_id,)).fetchone()
        if mailbox is None:
            raise AdapterError("临时邮箱不存在")
        conn.execute("DELETE FROM messages WHERE mailbox_id = ?", (mailbox_id,))
        conn.commit()


def delete_mailbox_by_id(mailbox_id: int) -> None:
    with db_lock, closing(_connect()) as conn:
        mailbox = conn.execute("SELECT id FROM generated_mailboxes WHERE id = ?", (mailbox_id,)).fetchone()
        if mailbox is None:
            raise AdapterError("临时邮箱不存在")
        conn.execute("DELETE FROM messages WHERE mailbox_id = ?", (mailbox_id,))
        conn.execute("DELETE FROM generated_mailboxes WHERE id = ?", (mailbox_id,))
        conn.commit()


def _mailbox_cutoff_iso() -> str:
    settings = get_runtime_settings()
    return (now_local() - timedelta(minutes=settings["mailbox_retention_minutes"])).strftime("%Y-%m-%d %H:%M:%S")


def count_pending_mailboxes(*, gmail_inbox_id: int | None = None) -> int:
    query = """
        SELECT COUNT(*) AS c
        FROM generated_mailboxes
        WHERE active = 1
          AND last_matched_at IS NULL
          AND created_at >= ?
    """
    params: list[Any] = [_mailbox_cutoff_iso()]
    if gmail_inbox_id is not None:
        query += " AND gmail_inbox_id = ?"
        params.append(gmail_inbox_id)
    with db_lock, closing(_connect()) as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return int(row["c"]) if row else 0


def cleanup_expired_data(*, force: bool = False) -> dict[str, Any]:
    global last_cleanup_started_at
    settings = get_runtime_settings()
    current = now_local()
    if not force:
        previous = parse_iso(last_cleanup_started_at)
        if previous is not None and (current - previous).total_seconds() < settings["cleanup_interval_seconds"]:
            return {
                "ran": False,
                "last_cleanup_started_at": last_cleanup_started_at,
                "message_retention_minutes": settings["message_retention_minutes"],
                "mailbox_retention_minutes": settings["mailbox_retention_minutes"],
                "cleanup_interval_seconds": settings["cleanup_interval_seconds"],
            }

    started_at = current.strftime("%Y-%m-%d %H:%M:%S")
    message_cutoff = (current - timedelta(minutes=settings["message_retention_minutes"])).strftime("%Y-%m-%d %H:%M:%S")
    mailbox_cutoff = (current - timedelta(minutes=settings["mailbox_retention_minutes"])).strftime("%Y-%m-%d %H:%M:%S")

    with db_lock, closing(_connect()) as conn:
        deleted_messages = conn.execute("DELETE FROM messages WHERE created_at < ?", (message_cutoff,)).rowcount
        deleted_mailboxes = conn.execute(
            """
            DELETE FROM generated_mailboxes
            WHERE COALESCE(last_matched_at, last_checked_at, created_at) < ?
              AND NOT EXISTS (SELECT 1 FROM messages WHERE messages.mailbox_id = generated_mailboxes.id)
            """,
            (mailbox_cutoff,),
        ).rowcount
        conn.commit()

    last_cleanup_started_at = started_at
    return {
        "ran": True,
        "deleted_messages": max(0, int(deleted_messages or 0)),
        "deleted_mailboxes": max(0, int(deleted_mailboxes or 0)),
        "last_cleanup_started_at": started_at,
        "message_retention_minutes": settings["message_retention_minutes"],
        "mailbox_retention_minutes": settings["mailbox_retention_minutes"],
        "cleanup_interval_seconds": settings["cleanup_interval_seconds"],
    }


def _headers_to_json(msg) -> str:
    items = [{"name": key, "value": value} for key, value in msg.raw_items()]
    return json.dumps(items, ensure_ascii=False)


def _decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = str(part.get_payload() or "").encode("utf-8", errors="replace")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


def _extract_bodies(msg) -> tuple[str | None, str | None]:
    text_body: str | None = None
    html_body: str | None = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
                continue
            content_type = (part.get_content_type() or "").lower()
            if content_type == "text/plain" and text_body is None:
                text_body = _decode_payload(part)
            elif content_type == "text/html" and html_body is None:
                html_body = _decode_payload(part)
    else:
        content_type = (msg.get_content_type() or "").lower()
        if content_type == "text/html":
            html_body = _decode_payload(msg)
        else:
            text_body = _decode_payload(msg)
    return text_body, html_body


def _parse_received_at(msg) -> str:
    raw_date = (msg.get("Date") or "").strip()
    if not raw_date:
        return now_iso()
    try:
        parsed = parsedate_to_datetime(raw_date)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return now_iso()


def _extract_addresses_from_value(value: str) -> set[str]:
    emails: set[str] = set()
    for _, addr in getaddresses([value]):
        addr = (addr or "").strip().lower()
        if _validate_email(addr):
            emails.add(addr)
    for match in EMAIL_RE.findall(value or ""):
        addr = match.strip().lower().strip("<>;, ")
        if _validate_email(addr):
            emails.add(addr)
    return emails


def _match_mailbox_from_message(msg, raw_source: str) -> sqlite3.Row | None:
    candidates: set[str] = set()
    for header_name in HEADER_CANDIDATES:
        for value in msg.get_all(header_name, []):
            candidates.update(_extract_addresses_from_value(str(value)))
    candidates.update(_extract_addresses_from_value(raw_source[:20000]))
    if not candidates:
        return None
    with db_lock, closing(_connect()) as conn:
        domains = {
            str(row["domain"]).lower()
            for row in conn.execute(
                """
                SELECT d.domain
                FROM domains d
                JOIN gmail_inboxes g ON g.id = d.gmail_inbox_id
                WHERE d.enabled = 1 AND g.enabled = 1
                """,
            ).fetchall()
        }
        if not domains:
            return None
        filtered = sorted({addr for addr in candidates if addr.split("@", 1)[1] in domains})
        if not filtered:
            return None
        placeholders = ",".join("?" for _ in filtered)
        return conn.execute(
            f"""
            SELECT *
            FROM generated_mailboxes
            WHERE active = 1 AND email IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(filtered),
        ).fetchone()


def _store_message_for_mailbox(mailbox_row: sqlite3.Row, gmail_inbox_id: int, uid: int, msg, raw_source: str) -> None:
    text_body, html_body = _extract_bodies(msg)
    timestamp = now_iso()
    with db_lock, closing(_connect()) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                mailbox_id, gmail_inbox_id, external_id, header_message_id,
                from_addr, to_addr, subject, text_body, html_body,
                raw_headers_json, raw_source, received_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(mailbox_row["id"]),
                gmail_inbox_id,
                str(uid),
                (msg.get("Message-ID") or "").strip() or None,
                (msg.get("From") or "").strip() or None,
                str(mailbox_row["email"]),
                (msg.get("Subject") or "").strip() or None,
                text_body,
                html_body,
                _headers_to_json(msg),
                raw_source,
                _parse_received_at(msg),
                timestamp,
            ),
        )
        conn.execute("UPDATE generated_mailboxes SET last_matched_at = ? WHERE id = ?", (timestamp, int(mailbox_row["id"])))
        conn.commit()


def _load_sync_state(conn: sqlite3.Connection, gmail_inbox_id: int) -> int:
    row = conn.execute("SELECT last_uid FROM sync_state WHERE gmail_inbox_id = ?", (gmail_inbox_id,)).fetchone()
    return int(row["last_uid"]) if row is not None else 0


def _save_sync_state(conn: sqlite3.Connection, gmail_inbox_id: int, last_uid: int, error: str | None) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO sync_state (gmail_inbox_id, last_uid, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(gmail_inbox_id) DO UPDATE SET last_uid = excluded.last_uid, updated_at = excluded.updated_at
        """,
        (gmail_inbox_id, last_uid, timestamp),
    )
    conn.execute(
        "UPDATE gmail_inboxes SET last_sync_at = ?, last_error = ? WHERE id = ?",
        (timestamp, error, gmail_inbox_id),
    )


def _fetch_uid_raw(mail: imaplib.IMAP4_SSL, uid: int) -> bytes | None:
    status, data = mail.uid("fetch", str(uid), "(UID BODY.PEEK[])")
    if status != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _mark_uid_deleted(mail: imaplib.IMAP4_SSL, uid: int) -> bool:
    status, _ = mail.uid("store", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
    return status == "OK"


def _expunge_mailbox(mail: imaplib.IMAP4_SSL) -> bool:
    status, _ = mail.expunge()
    return status == "OK"


def _stored_uids_for_inbox(gmail_inbox_id: int, candidate_uids: list[int]) -> list[int]:
    if not candidate_uids:
        return []
    placeholders = ",".join("?" for _ in candidate_uids)
    params: list[Any] = [gmail_inbox_id, *[str(uid) for uid in candidate_uids]]
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT external_id
            FROM messages
            WHERE gmail_inbox_id = ?
              AND external_id IN ({placeholders})
            """,
            tuple(params),
        ).fetchall()
    result: list[int] = []
    for row in rows:
        try:
            result.append(int(str(row["external_id"])))
        except Exception:
            continue
    return sorted(set(result))


def _purge_old_processed_uids(mail: imaplib.IMAP4_SSL, gmail_inbox_id: int, last_uid: int) -> int:
    if not DELETE_PROCESSED_GMAIL_MESSAGES or GMAIL_PURGE_BATCH_SIZE <= 0 or last_uid <= 0:
        return 0
    status, data = mail.uid("search", None, f"UID 1:{last_uid}")
    if status != "OK":
        return 0
    old_uids = [int(x) for x in (data[0].decode("utf-8", errors="ignore").split() if data and data[0] else [])]
    if not old_uids:
        return 0
    candidate_uids = old_uids[:GMAIL_PURGE_BATCH_SIZE]
    matched_uids = _stored_uids_for_inbox(gmail_inbox_id, candidate_uids)
    deleted_count = 0
    for uid in matched_uids:
        if _mark_uid_deleted(mail, uid):
            deleted_count += 1
    if deleted_count > 0:
        _expunge_mailbox(mail)
    return deleted_count


def sync_inbox(gmail_inbox_id: int) -> None:
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM gmail_inboxes WHERE id = ?", (gmail_inbox_id,)).fetchone()
        if row is None or not int(row["enabled"]):
            return
        last_uid = _load_sync_state(conn, gmail_inbox_id)
    error: str | None = None
    max_uid = last_uid
    try:
        app_password = decrypt_secret(str(row["app_password_enc"]))
        mail = ProxyIMAP4_SSL(
            str(row["imap_host"]),
            int(row["imap_port"]),
            proxy_url=row["imap_proxy_url"],
            timeout=15,
        )
        try:
            mail.login(str(row["email_address"]), app_password)
            mail.select("INBOX", readonly=not DELETE_PROCESSED_GMAIL_MESSAGES)
            status, data = mail.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise AdapterError("IMAP search failed")
            uids = [int(x) for x in (data[0].decode("utf-8", errors="ignore").split() if data and data[0] else [])]
            deleted_new_uids = 0
            for uid in uids:
                raw_bytes = _fetch_uid_raw(mail, uid)
                max_uid = max(max_uid, uid)
                if not raw_bytes:
                    continue
                raw_source = raw_bytes.decode("utf-8", errors="replace")
                msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                mailbox_row = _match_mailbox_from_message(msg, raw_source)
                if mailbox_row is None:
                    continue
                _store_message_for_mailbox(mailbox_row, gmail_inbox_id, uid, msg, raw_source)
                if DELETE_PROCESSED_GMAIL_MESSAGES and _mark_uid_deleted(mail, uid):
                    deleted_new_uids += 1
            if deleted_new_uids > 0:
                _expunge_mailbox(mail)
            _purge_old_processed_uids(mail, gmail_inbox_id, last_uid)
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    except Exception as exc:
        error = str(exc)[:500]
    with db_lock, closing(_connect()) as conn:
        _save_sync_state(conn, gmail_inbox_id, max_uid, error)
        conn.commit()
    if error:
        raise AdapterError(error)


def sync_all_inboxes(*, force: bool = False) -> None:
    if not sync_lock.acquire(blocking=False):
        return
    try:
        if not force and count_pending_mailboxes() <= 0:
            return
        for inbox in get_all_gmail_inboxes():
            if not inbox["enabled"]:
                continue
            try:
                sync_inbox(int(inbox["id"]))
            except Exception:
                continue
    finally:
        sync_lock.release()


def _sync_loop() -> None:
    while not stop_event.is_set():
        sync_all_inboxes()
        try:
            cleanup_expired_data()
        except Exception:
            pass
        stop_event.wait(get_runtime_settings()["sync_interval_seconds"])


def start_sync_thread() -> None:
    global sync_thread
    if sync_thread and sync_thread.is_alive():
        return
    stop_event.clear()
    sync_thread = threading.Thread(target=_sync_loop, name="gmail-relay-sync", daemon=True)
    sync_thread.start()


def stop_sync_thread() -> None:
    global sync_thread
    stop_event.set()
    if sync_thread and sync_thread.is_alive():
        sync_thread.join(timeout=3)
    sync_thread = None


def get_status() -> dict[str, Any]:
    settings = get_runtime_settings()
    pending_mailbox_count = count_pending_mailboxes()
    with db_lock, closing(_connect()) as conn:
        inbox_count = conn.execute("SELECT COUNT(*) AS c FROM gmail_inboxes").fetchone()
        domain_count = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        mailbox_count = conn.execute("SELECT COUNT(*) AS c FROM generated_mailboxes").fetchone()
        message_count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
        latest_sync = conn.execute("SELECT MAX(last_sync_at) AS latest_sync FROM gmail_inboxes").fetchone()
    return {
        "sync_interval_seconds": settings["sync_interval_seconds"],
        "inboxes": int(inbox_count["c"]) if inbox_count else 0,
        "domains": int(domain_count["c"]) if domain_count else 0,
        "mailboxes": int(mailbox_count["c"]) if mailbox_count else 0,
        "pending_mailboxes": pending_mailbox_count,
        "messages": int(message_count["c"]) if message_count else 0,
        "latest_sync_at": latest_sync["latest_sync"] if latest_sync else None,
        "gmail_polling_active": pending_mailbox_count > 0,
        "gmail_polling_reason": "pending-mailboxes" if pending_mailbox_count > 0 else "no-pending-mailboxes",
        "delete_processed_gmail_messages": DELETE_PROCESSED_GMAIL_MESSAGES,
        "gmail_purge_batch_size": GMAIL_PURGE_BATCH_SIZE,
        "message_retention_minutes": settings["message_retention_minutes"],
        "mailbox_retention_minutes": settings["mailbox_retention_minutes"],
        "cleanup_interval_seconds": settings["cleanup_interval_seconds"],
        "last_cleanup_started_at": last_cleanup_started_at,
    }


def _render_login_page(error: str | None = None) -> HTMLResponse:
    error_html = f'<div class="error">{error}</div>' if error else ""
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{APP_TITLE} Login</title>
  <style>
    :root {{ --bg: #efe3d3; --panel: rgba(255, 249, 241, 0.86); --panel-strong: rgba(255, 253, 249, 0.94); --line: rgba(110, 91, 61, 0.18); --ink: #182119; --muted: #5f685a; --accent: #1f5c4a; --accent-strong: #174436; --warning: #bf6c2f; --danger: #a13a2f; --shadow: 0 28px 70px rgba(44, 31, 16, 0.16); }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; font-family: "Trebuchet MS", "Gill Sans", sans-serif; color: var(--ink); background:
      radial-gradient(circle at top left, rgba(255, 250, 239, 0.92), transparent 28%),
      radial-gradient(circle at bottom right, rgba(187, 216, 195, 0.42), transparent 24%),
      linear-gradient(135deg, #e7d7c5 0%, #f5eddf 42%, #dbe5d6 100%); }}
    body::before {{ content: ""; position: fixed; inset: 0; pointer-events: none; background:
      linear-gradient(120deg, rgba(255,255,255,.08) 0, rgba(255,255,255,.08) 1px, transparent 1px, transparent 32px),
      linear-gradient(210deg, rgba(0,0,0,.03) 0, rgba(0,0,0,.03) 1px, transparent 1px, transparent 32px); opacity: .35; }}
    .login-shell {{ position: relative; min-height: 100vh; display: grid; place-items: center; padding: 32px 20px; }}
    .login-grid {{ width: min(1080px, 100%); display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(320px, .85fr); gap: 22px; align-items: stretch; }}
    .hero, .card {{ position: relative; overflow: hidden; border: 1px solid var(--line); border-radius: 32px; box-shadow: var(--shadow); backdrop-filter: blur(14px); }}
    .hero {{ padding: 36px; background: linear-gradient(155deg, rgba(24, 33, 25, 0.94), rgba(33, 68, 58, 0.88)); color: #f5efe4; }}
    .hero::after {{ content: ""; position: absolute; right: -70px; top: -50px; width: 220px; height: 220px; border-radius: 999px; background: radial-gradient(circle, rgba(255,255,255,.16), transparent 68%); }}
    .hero small {{ display: inline-flex; align-items: center; padding: 8px 12px; border-radius: 999px; letter-spacing: .08em; text-transform: uppercase; background: rgba(255,255,255,.12); color: rgba(255,255,255,.88); }}
    .hero h1 {{ margin: 18px 0 12px; font-family: "Iowan Old Style", "Palatino Linotype", serif; font-size: clamp(36px, 6vw, 54px); line-height: 1.02; }}
    .hero p {{ margin: 0; max-width: 34rem; font-size: 17px; line-height: 1.65; color: rgba(245, 239, 228, .82); }}
    .hero ul {{ list-style: none; padding: 0; margin: 28px 0 0; display: grid; gap: 10px; }}
    .hero li {{ display: flex; gap: 12px; align-items: center; color: rgba(245, 239, 228, .88); }}
    .hero li::before {{ content: ""; width: 10px; height: 10px; border-radius: 999px; background: linear-gradient(180deg, #f5c676, #d67a36); box-shadow: 0 0 0 6px rgba(245, 198, 118, .14); flex: 0 0 auto; }}
    .card {{ padding: 30px; background: var(--panel-strong); }}
    .card h2 {{ margin: 0 0 8px; font-family: "Iowan Old Style", "Palatino Linotype", serif; font-size: 34px; }}
    .lead {{ margin: 0 0 22px; color: var(--muted); line-height: 1.6; }}
    form {{ display: grid; gap: 14px; }}
    label {{ display: grid; gap: 8px; font-size: 14px; color: var(--muted); }}
    input {{ width: 100%; border: 1px solid rgba(110, 91, 61, 0.18); border-radius: 16px; padding: 14px 15px; font: inherit; color: var(--ink); background: rgba(255,255,255,.86); }}
    input:focus {{ outline: none; border-color: rgba(31, 92, 74, 0.52); box-shadow: 0 0 0 4px rgba(31, 92, 74, 0.08); }}
    button {{ border: 0; border-radius: 999px; padding: 14px 16px; font: inherit; font-weight: 600; cursor: pointer; color: #fff; background: linear-gradient(180deg, var(--accent), var(--accent-strong)); box-shadow: 0 14px 28px rgba(31, 92, 74, .18); }}
    .error {{ margin-bottom: 4px; border: 1px solid rgba(161, 58, 47, .18); border-radius: 16px; padding: 12px 14px; color: var(--danger); background: rgba(161, 58, 47, .08); font-size: 14px; }}
    .footnote {{ margin-top: 14px; font-size: 13px; color: var(--muted); }}
    @media (max-width: 900px) {{ .login-grid {{ grid-template-columns: 1fr; }} .hero, .card {{ border-radius: 28px; }} .hero {{ padding: 28px; }} }}
  </style>
</head>
<body>
  <div class="login-shell">
    <div class="login-grid">
      <section class="hero">
        <small>Operations Console</small>
        <h1>Gmail Relay 管理后台</h1>
        <p>管理 Gmail 收件箱、域名映射、轮询参数和临时邮箱缓存。这个后台面向运维，不是演示页，所以信息密度和状态可读性更重要。</p>
        <ul>
          <li>集中查看同步状态、缓存量和待匹配邮箱</li>
          <li>配置 Gmail IMAP、域名绑定和清理策略</li>
          <li>作为 GPTMail 兼容接口的运维控制面板</li>
        </ul>
      </section>
      <section class="card">
        <h2>登录</h2>
        <p class="lead">登录后进入控制台。建议尽快替换默认后台账号，并限制公网访问来源。</p>
        {error_html}
        <form method="post" action="/admin/login">
          <label><span>用户名</span><input name="username" type="text" autocomplete="username" required></label>
          <label><span>密码</span><input name="password" type="password" autocomplete="current-password" required></label>
          <button type="submit">进入控制台</button>
        </form>
        <div class="footnote">提示：这个管理后台使用 cookie 会话；API 仍然支持 `X-Admin-Token`。</div>
      </section>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(page)


def _render_admin_page() -> HTMLResponse:
    page = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Gmail Relay 管理台</title>
  <style>
    :root { --bg: #efe4d3; --panel: rgba(255, 249, 241, 0.86); --panel-strong: rgba(255, 253, 249, 0.94); --panel-dark: rgba(24, 33, 25, 0.92); --line: rgba(107, 88, 58, 0.18); --line-strong: rgba(107, 88, 58, 0.28); --ink: #182119; --muted: #627062; --accent: #1f5c4a; --accent-strong: #174436; --warm: #bf6c2f; --danger: #a13a2f; --shadow: 0 22px 60px rgba(55, 39, 22, 0.12); --radius-lg: 28px; --radius-md: 22px; --radius-sm: 16px; }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body { margin: 0; font-family: "Trebuchet MS", "Gill Sans", sans-serif; color: var(--ink); background:
      radial-gradient(circle at top left, rgba(255, 248, 238, 0.92), transparent 22%),
      radial-gradient(circle at top right, rgba(214, 231, 218, 0.56), transparent 26%),
      linear-gradient(180deg, #f2e7d7 0%, #f7f0e4 40%, #ece3d3 100%); }
    body::before { content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .34; background:
      linear-gradient(90deg, rgba(255,255,255,.08) 0, rgba(255,255,255,.08) 1px, transparent 1px, transparent 36px),
      linear-gradient(0deg, rgba(0,0,0,.02) 0, rgba(0,0,0,.02) 1px, transparent 1px, transparent 36px); }
    h1, h2, h3, h4 { font-family: "Iowan Old Style", "Palatino Linotype", serif; letter-spacing: -.02em; }
    button, input, textarea, select { font: inherit; }
    button { border: 0; border-radius: 999px; padding: 11px 15px; font-weight: 600; cursor: pointer; color: #fff; background: linear-gradient(180deg, var(--accent), var(--accent-strong)); box-shadow: 0 12px 24px rgba(31, 92, 74, .18); transition: transform .16s ease, box-shadow .16s ease, opacity .16s ease; }
    button:hover { transform: translateY(-1px); }
    button.secondary { background: linear-gradient(180deg, #cb7a39, #ad5f23); box-shadow: 0 12px 24px rgba(191, 108, 47, .18); }
    button.danger { background: linear-gradient(180deg, #b24a3e, #933126); box-shadow: 0 12px 24px rgba(161, 58, 47, .18); }
    button.ghost { color: var(--ink); background: rgba(255,255,255,.56); border: 1px solid var(--line); box-shadow: none; }
    button:disabled { opacity: .52; cursor: not-allowed; transform: none; box-shadow: none; }
    input, textarea, select { width: 100%; border: 1px solid var(--line); border-radius: 16px; padding: 12px 14px; color: var(--ink); background: rgba(255,255,255,.88); }
    input:focus, textarea:focus, select:focus { outline: none; border-color: rgba(31, 92, 74, .48); box-shadow: 0 0 0 4px rgba(31, 92, 74, .08); }
    textarea { min-height: 108px; resize: vertical; }
    label { display: grid; gap: 8px; font-size: 14px; color: var(--muted); }
    code { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; background: rgba(24, 33, 25, .07); font-size: 12px; }
    .shell { position: relative; max-width: 1540px; margin: 0 auto; padding: 24px 20px 56px; }
    .hero { display: grid; gap: 18px; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); align-items: stretch; margin-bottom: 22px; }
    .hero-main, .hero-side, .panel, .metric-card, .workspace, .list-card, .detail-card, .note-card { position: relative; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-lg); box-shadow: var(--shadow); backdrop-filter: blur(14px); }
    .hero-main { padding: 30px; background: linear-gradient(145deg, rgba(24, 33, 25, 0.95), rgba(33, 68, 58, 0.88)); color: #f5efe4; }
    .hero-main::after { content: ""; position: absolute; right: -60px; top: -70px; width: 240px; height: 240px; border-radius: 999px; background: radial-gradient(circle, rgba(255,255,255,.16), transparent 68%); }
    .hero-side { padding: 22px; background: rgba(255, 252, 246, .82); display: grid; gap: 14px; align-content: start; }
    .kicker { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
    .kicker::before { content: ""; width: 8px; height: 8px; border-radius: 999px; background: linear-gradient(180deg, #efb969, #d67837); box-shadow: 0 0 0 6px rgba(239, 185, 105, .14); }
    .hero-main h1 { margin: 16px 0 10px; font-size: clamp(36px, 6vw, 56px); line-height: 1.02; }
    .hero-main p { margin: 0; max-width: 42rem; color: rgba(245, 239, 228, .82); font-size: 16px; line-height: 1.7; }
    .hero-tags { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }
    .hero-tag, .badge, .subtle-badge { display: inline-flex; align-items: center; gap: 8px; border-radius: 999px; padding: 8px 12px; font-size: 12px; white-space: nowrap; }
    .hero-tag { background: rgba(255,255,255,.12); color: rgba(255,255,255,.88); }
    .badge { background: rgba(31, 92, 74, .12); color: var(--accent); }
    .badge.off { background: rgba(161, 58, 47, .12); color: var(--danger); }
    .badge.warn { background: rgba(191, 108, 47, .12); color: var(--warm); }
    .subtle-badge { border: 1px solid var(--line); background: rgba(255,255,255,.62); color: var(--muted); }
    .hero-side h2 { margin: 0; font-size: 28px; }
    .hero-side p { margin: 0; color: var(--muted); line-height: 1.55; }
    .action-stack { display: grid; gap: 10px; }
    .action-stack form { margin: 0; }
    .metrics { display: grid; gap: 14px; grid-template-columns: repeat(6, minmax(0, 1fr)); margin-bottom: 18px; }
    .metric-card { padding: 16px 18px; background: var(--panel); }
    .metric-card strong { display: block; margin: 10px 0 8px; font-size: 28px; line-height: 1; }
    .metric-card span { display: block; font-size: 12px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
    .metric-card p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .tabs { display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 20px; }
    .tab-btn { border: 1px solid var(--line); background: rgba(255,255,255,.62); color: var(--ink); box-shadow: none; }
    .tab-btn.active { color: #fff; background: linear-gradient(180deg, var(--accent), var(--accent-strong)); border-color: transparent; }
    .section { display: none; }
    .section.active { display: block; }
    .section-stack { display: grid; gap: 20px; }
    .workspace, .list-card, .detail-card, .note-card { background: var(--panel); }
    .workspace { padding: 24px; }
    .workspace-head, .panel-head, .detail-head { display: flex; gap: 14px; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; }
    .workspace-head h2, .panel-head h3, .detail-head h2 { margin: 6px 0 0; font-size: 30px; }
    .workspace-copy, .panel-copy { max-width: 40rem; color: var(--muted); line-height: 1.65; }
    .workspace-grid { display: grid; gap: 18px; grid-template-columns: minmax(320px, 380px) minmax(0, 1fr); margin-top: 20px; align-items: start; }
    .composer-card, .summary-card { border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--panel-strong); padding: 20px; }
    .composer-card { position: sticky; top: 20px; }
    .composer-card h3, .summary-card h3 { margin: 0 0 6px; font-size: 24px; }
    .summary-card p, .composer-card p { margin: 0 0 16px; color: var(--muted); line-height: 1.6; }
    .stack { display: grid; gap: 14px; }
    .field-grid { display: grid; gap: 12px; grid-template-columns: minmax(0, 1fr) 150px; }
    .toggle-row { display: flex; gap: 10px; align-items: center; justify-content: flex-start; }
    .toggle-row input { width: auto; margin: 0; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .status { min-height: 22px; font-size: 14px; color: var(--muted); }
    .list-card, .detail-card, .note-card { padding: 22px; }
    .collection { display: grid; gap: 12px; }
    .collection-scroll { max-height: 560px; overflow: auto; padding-right: 4px; }
    .data-card, .mailbox-row, .message-card, .empty-state { border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 16px; background: rgba(255,255,255,.7); }
    .data-card:hover, .mailbox-row:hover, .message-card:hover { border-color: var(--line-strong); }
    .data-card-header, .mailbox-header, .message-header { display: flex; gap: 12px; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; }
    .data-card h4, .mailbox-row h4, .message-card h4 { margin: 0; font-size: 22px; }
    .data-card p, .mailbox-row p, .message-card p { margin: 0; }
    .meta, .meta-grid { color: var(--muted); font-size: 13px; line-height: 1.6; }
    .meta-grid { display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 14px; }
    .meta-block strong { display: block; margin-bottom: 4px; color: var(--ink); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    .note-line { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(107, 88, 58, .12); }
    .note-line.warn { color: var(--danger); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    .mail-layout { display: grid; gap: 18px; grid-template-columns: minmax(320px, 420px) minmax(0, 1fr); align-items: start; }
    .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 18px 0 16px; }
    .toolbar input { flex: 1; min-width: 220px; }
    .mailbox-row.active { border-color: rgba(31, 92, 74, .46); box-shadow: inset 0 0 0 1px rgba(31, 92, 74, .18); background: linear-gradient(180deg, rgba(255,255,255,.88), rgba(231, 242, 237, .82)); }
    .mailbox-row h4 { font-size: 19px; }
    .mailbox-summary { display: grid; gap: 10px; margin-top: 12px; }
    .message-stack { display: grid; gap: 12px; margin-top: 18px; }
    .message-body { max-height: 360px; overflow: auto; padding: 14px; border-radius: 16px; border: 1px solid var(--line); background: rgba(255,255,255,.82); white-space: pre-wrap; word-break: break-word; color: #334036; line-height: 1.65; margin-top: 14px; }
    .settings-grid { display: grid; gap: 18px; grid-template-columns: minmax(320px, 420px) minmax(0, 1fr); align-items: start; }
    .notes-grid { display: grid; gap: 12px; margin-top: 18px; }
    .note-card { padding: 18px; border-radius: var(--radius-md); }
    .note-card strong { display: block; margin-bottom: 6px; font-size: 18px; }
    .empty-state { padding: 28px 20px; text-align: center; background: rgba(255,255,255,.52); }
    .empty-state strong { display: block; margin-bottom: 8px; font-size: 20px; }
    .empty-state p { color: var(--muted); line-height: 1.6; }
    @media (max-width: 1280px) { .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
    @media (max-width: 1100px) { .hero, .workspace-grid, .mail-layout, .settings-grid { grid-template-columns: 1fr; } .composer-card { position: static; } .collection-scroll { max-height: none; } }
    @media (max-width: 720px) { .shell { padding-left: 14px; padding-right: 14px; } .hero-main, .hero-side, .workspace, .list-card, .detail-card { border-radius: 24px; } .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } .meta-grid { grid-template-columns: 1fr; } .field-grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="shell">
    <header class="hero">
      <section class="hero-main">
        <div class="kicker">Operations Console</div>
        <h1>Gmail Relay 管理台</h1>
        <p>Cloudflare Email Routing / Worker 到 Gmail 的收件链路，经过 IMAP 轮询后转成 GPTMail 兼容接口。这个版本把信息层级重新整理成更接近正式后台的操作面板。</p>
        <div class="hero-tags">
          <span class="hero-tag">Gmail Inbox Routing</span>
          <span class="hero-tag">Temporary Mailbox Cache</span>
          <span class="hero-tag">GPTMail Compatible API</span>
        </div>
      </section>
      <aside class="hero-side">
        <div class="kicker">Quick Actions</div>
        <h2>运维操作</h2>
        <p>同步、清理和退出集中到右侧，避免打断主工作区。</p>
        <div class="action-stack">
          <button id="sync-all-btn" type="button" class="secondary">立即同步全部收件箱</button>
          <button id="cleanup-btn" type="button" class="ghost">清理过期数据</button>
          <form method="post" action="/admin/logout"><button type="submit" class="ghost">退出登录</button></form>
        </div>
      </aside>
    </header>

    <div id="metrics" class="metrics"></div>

    <nav class="tabs">
      <button type="button" class="tab-btn active" data-section="overview">概览</button>
      <button type="button" class="tab-btn" data-section="mailboxes">临时邮箱</button>
      <button type="button" class="tab-btn" data-section="settings">设置</button>
    </nav>

    <section id="section-overview" class="section active">
      <div class="section-stack">
        <section class="workspace">
          <div class="workspace-head">
            <div>
              <div class="kicker">Inbox Control</div>
              <h2>Gmail 收件箱</h2>
              <div class="workspace-copy">新增或编辑 Gmail IMAP 配置，并在右侧集中查看同步状态、错误和代理信息。列表现在适合更高数量的收件箱配置。</div>
            </div>
            <div id="inbox-count-hint" class="subtle-badge">0 个收件箱</div>
          </div>
          <div class="workspace-grid">
            <section class="composer-card">
              <h3>新增或编辑</h3>
              <p>保留原有字段和行为，重点提升信息排版与编辑态清晰度。</p>
              <form id="inbox-form" class="stack">
                <input type="hidden" name="inbox_id" value="">
                <label><span>名称</span><input name="name" placeholder="gmail-main" required></label>
                <label><span>Gmail 邮箱</span><input name="email_address" placeholder="name@gmail.com" required></label>
                <label><span>应用专用密码</span><input name="app_password" placeholder="16 位 App Password" required></label>
                <label><span>IMAP 代理</span><input name="imap_proxy_url" placeholder="socks5h://host.docker.internal:7890"></label>
                <div class="field-grid">
                  <label><span>IMAP Host</span><input name="imap_host" value="imap.gmail.com" required></label>
                  <label><span>IMAP Port</span><input name="imap_port" type="number" value="993" required></label>
                </div>
                <label><span>备注</span><textarea name="notes" placeholder="可选"></textarea></label>
                <label class="toggle-row"><input type="checkbox" name="enabled" checked><span>启用收件箱</span></label>
                <div class="row">
                  <button id="inbox-submit-btn" type="submit">新增 Gmail 邮箱</button>
                  <button id="inbox-cancel-btn" type="button" class="ghost" style="display:none;">取消编辑</button>
                </div>
                <div id="inbox-status" class="status"></div>
              </form>
            </section>
            <section class="list-card">
              <div class="panel-head">
                <div>
                  <div class="kicker">Inbox Directory</div>
                  <h3>当前收件箱</h3>
                </div>
              </div>
              <div id="inboxes" class="collection collection-scroll"></div>
            </section>
          </div>
        </section>

        <section class="workspace">
          <div class="workspace-head">
            <div>
              <div class="kicker">Domain Mapping</div>
              <h2>域名管理</h2>
              <div class="workspace-copy">把域名与 Gmail 收件箱绑定。域名列表改成更紧凑的操作卡片，适合多域名场景。</div>
            </div>
            <div id="domain-count-hint" class="subtle-badge">0 个域名</div>
          </div>
          <div class="workspace-grid">
            <section class="composer-card">
              <h3>新增域名</h3>
              <p>域名映射仍然通过现有 API 保存，页面只重新组织展示。</p>
              <form id="domain-form" class="stack">
                <label><span>域名</span><input name="domain" placeholder="mail.example.com" required></label>
                <label><span>绑定 Gmail 邮箱</span><select name="gmail_inbox_id" id="domain-inbox-select" required></select></label>
                <label><span>备注</span><textarea name="notes" placeholder="可选"></textarea></label>
                <label class="toggle-row"><input type="checkbox" name="enabled" checked><span>启用域名</span></label>
                <div class="row"><button type="submit">新增域名</button></div>
                <div id="domain-status" class="status"></div>
              </form>
            </section>
            <section class="list-card">
              <div class="panel-head">
                <div>
                  <div class="kicker">Domain Directory</div>
                  <h3>当前域名</h3>
                </div>
              </div>
              <div id="domains" class="collection collection-scroll"></div>
            </section>
          </div>
        </section>
      </div>
    </section>

    <section id="section-mailboxes" class="section">
      <div class="mail-layout">
        <section class="list-card">
          <div class="panel-head">
            <div>
              <div class="kicker">Mailbox Directory</div>
              <h3>临时邮箱列表</h3>
              <div class="panel-copy">左侧列表改成更适合大规模浏览的紧凑卡片；搜索和刷新保留原有行为。</div>
            </div>
            <div id="mailbox-result-count" class="subtle-badge">0 条结果</div>
          </div>
          <div class="toolbar">
            <input id="mailbox-search" type="text" placeholder="搜索临时邮箱、标签或 Gmail 邮箱">
            <button id="mailbox-search-btn" type="button">搜索</button>
            <button id="mailbox-refresh-btn" type="button" class="ghost">刷新</button>
          </div>
          <div id="mailbox-list" class="collection collection-scroll"></div>
        </section>

        <section class="detail-card">
          <div class="detail-head">
            <div>
              <div class="kicker">Mailbox Detail</div>
              <h2 id="mailbox-detail-title">临时邮箱详情</h2>
              <div id="mailbox-detail-meta" class="meta">选择左侧临时邮箱后可查看邮件并执行清理。</div>
            </div>
            <div class="row">
              <button id="mailbox-detail-refresh" type="button" class="ghost" disabled>刷新邮件</button>
              <button id="mailbox-detail-clear" type="button" class="danger" disabled>清空邮件</button>
              <button id="mailbox-detail-delete" type="button" class="danger" disabled>删除临时邮箱</button>
            </div>
          </div>
          <div id="message-list" class="message-stack"></div>
        </section>
      </div>
    </section>

    <section id="section-settings" class="section">
      <div class="settings-grid">
        <section class="composer-card">
          <h3>运行设置</h3>
          <p>同步、保留与清理参数保持原有接口，只重新整理为更正式的控制面板。</p>
          <form id="settings-form" class="stack">
            <label><span>轮询间隔（秒）</span><input type="number" min="5" max="3600" name="sync_interval_seconds" required></label>
            <label><span>邮件保留时长（分钟）</span><input type="number" min="1" max="10080" name="message_retention_minutes" required></label>
            <label><span>临时邮箱保留时长（分钟）</span><input type="number" min="1" max="43200" name="mailbox_retention_minutes" required></label>
            <label><span>清理间隔（秒）</span><input type="number" min="60" max="86400" name="cleanup_interval_seconds" required></label>
            <div class="row"><button type="submit">保存设置</button></div>
            <div id="settings-status" class="status"></div>
          </form>
        </section>
        <section class="list-card">
          <div class="panel-head">
            <div>
              <div class="kicker">Runtime Notes</div>
              <h3>说明与状态</h3>
              <div class="panel-copy">把静态说明和动态状态汇总分开放，减少之前大段文本堆积造成的压迫感。</div>
            </div>
          </div>
          <div class="notes-grid">
            <article class="note-card"><strong>按需轮询</strong><div class="meta">只有存在待处理临时邮箱时，后台才会轮询 Gmail，避免空转。</div></article>
            <article class="note-card"><strong>邮件清理</strong><div class="meta">过期验证码邮件会被移除，减少重复匹配和存储占用。</div></article>
            <article class="note-card"><strong>邮箱清理</strong><div class="meta">长期未使用且没有邮件记录的临时邮箱会被自动删除。</div></article>
            <article class="note-card"><strong>联动删除</strong><div class="meta">删除临时邮箱会同时删除该邮箱下的缓存邮件。</div></article>
          </div>
          <div id="settings-summary" class="collection" style="margin-top:18px;"></div>
        </section>
      </div>
    </section>
  </div>

  <script>
    const state = {
      status: {},
      settings: {},
      inboxes: [],
      domains: [],
      mailboxes: [],
      selectedMailboxId: null,
      selectedMailbox: null,
      selectedMessages: [],
    };

    function escapeHtml(value) {
      return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    }

    function clip(value, max = 160) {
      const text = String(value ?? '').trim();
      return text.length > max ? `${text.slice(0, max)}...` : text;
    }

    function badge(text, tone = '') {
      return `<span class="badge${tone ? ` ${tone}` : ''}">${escapeHtml(text)}</span>`;
    }

    function subtleBadge(text) {
      return `<span class="subtle-badge">${escapeHtml(text)}</span>`;
    }

    function emptyState(title, text) {
      return `<article class="empty-state"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(text)}</p></article>`;
    }

    async function request(path, options = {}) {
      const response = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({ detail: '请求失败' }));
        throw new Error(payload.detail || '请求失败');
      }
      return response.json();
    }

    function formToObject(form) {
      const data = Object.fromEntries(new FormData(form).entries());
      Object.keys(data).forEach((key) => { if (data[key] === '') data[key] = null; });
      if (form.elements.namedItem('enabled')) data.enabled = form.elements.enabled.checked;
      ['imap_port', 'gmail_inbox_id', 'inbox_id', 'sync_interval_seconds', 'message_retention_minutes', 'mailbox_retention_minutes', 'cleanup_interval_seconds'].forEach((key) => {
        if (key in data && data[key] !== null) data[key] = Number(data[key]);
      });
      return data;
    }

    function setStatus(id, text, bad = false) {
      const node = document.getElementById(id);
      node.textContent = text;
      node.style.color = bad ? '#9d2b2b' : '#6a6f5e';
    }

    function showSection(section) {
      document.querySelectorAll('.section').forEach((node) => node.classList.toggle('active', node.id === `section-${section}`));
      document.querySelectorAll('.tab-btn').forEach((button) => button.classList.toggle('active', button.dataset.section === section));
    }

    function renderMetrics() {
      const data = state.status || {};
      const items = [
        { label: 'Gmail 收件箱', value: data.inboxes || 0, note: '已接入用于 IMAP 轮询的收件箱数量' },
        { label: '域名', value: data.domains || 0, note: '当前启用或可配置的接收域名' },
        { label: '临时邮箱', value: data.mailboxes || 0, note: '系统内已生成的临时邮箱总数' },
        { label: '待匹配邮箱', value: data.pending_mailboxes || 0, note: '等待 Gmail 轮询命中的邮箱数' },
        { label: '缓存邮件', value: data.messages || 0, note: '保存在本地数据库中的邮件总数' },
        { label: '同步状态', value: data.gmail_polling_active ? '运行中' : '空闲', note: data.latest_sync_at ? `最近同步 ${data.latest_sync_at}` : `原因 ${data.gmail_polling_reason || '-'}` },
      ];
      document.getElementById('metrics').innerHTML = items.map((item) => `
        <article class="metric-card">
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(item.value)}</strong>
          <p>${escapeHtml(item.note)}</p>
        </article>`).join('');
    }

    function renderInboxOptions() {
      const select = document.getElementById('domain-inbox-select');
      select.innerHTML = state.inboxes.length
        ? state.inboxes.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} | ${escapeHtml(item.email_address)}</option>`).join('')
        : '<option value="">请先新增 Gmail 邮箱</option>';
      select.disabled = !state.inboxes.length;
    }

    function renderInboxes() {
      document.getElementById('inbox-count-hint').textContent = `${state.inboxes.length} 个收件箱`;
      document.getElementById('inboxes').innerHTML = state.inboxes.length ? state.inboxes.map((item) => `
        <article class="data-card">
          <div class="data-card-header">
            <div>
              <h4>${escapeHtml(item.name)}</h4>
              <div class="meta">${escapeHtml(item.email_address)}</div>
            </div>
            ${item.enabled ? badge('已启用') : badge('已停用', 'off')}
          </div>
          <div class="meta-grid">
            <div class="meta-block">
              <strong>IMAP</strong>
              <div class="meta">${escapeHtml(item.imap_host)}:${item.imap_port}</div>
            </div>
            <div class="meta-block">
              <strong>代理</strong>
              <div class="meta">${escapeHtml(item.imap_proxy_url || '-')}</div>
            </div>
            <div class="meta-block">
              <strong>最近同步</strong>
              <div class="meta">${escapeHtml(item.last_sync_at || '-')}</div>
            </div>
            <div class="meta-block">
              <strong>错误</strong>
              <div class="meta">${escapeHtml(item.last_error || '无')}</div>
            </div>
          </div>
          <div class="note-line${item.last_error ? ' warn' : ''}">${escapeHtml(item.notes || (item.last_error ? clip(item.last_error, 180) : '无备注'))}</div>
          <div class="actions">
            <button type="button" data-action="edit-inbox" data-id="${item.id}" class="ghost">编辑</button>
            <button type="button" data-action="toggle-inbox" data-id="${item.id}" class="secondary">${item.enabled ? '停用' : '启用'}</button>
            <button type="button" data-action="sync-inbox" data-id="${item.id}">同步</button>
            <button type="button" data-action="delete-inbox" data-id="${item.id}" class="danger">删除</button>
          </div>
        </article>`).join('') : emptyState('暂无 Gmail 收件箱', '先新增一个 Gmail Inbox，再继续绑定域名和轮询流程。');
    }

    function renderDomains() {
      document.getElementById('domain-count-hint').textContent = `${state.domains.length} 个域名`;
      document.getElementById('domains').innerHTML = state.domains.length ? state.domains.map((item) => `
        <article class="data-card">
          <div class="data-card-header">
            <div>
              <h4>${escapeHtml(item.domain)}</h4>
              <div class="meta">绑定 Gmail：${escapeHtml(item.gmail_inbox_name)}</div>
            </div>
            ${item.enabled ? badge('已启用') : badge('已停用', 'off')}
          </div>
          <div class="meta-grid">
            <div class="meta-block">
              <strong>创建时间</strong>
              <div class="meta">${escapeHtml(item.created_at || '-')}</div>
            </div>
            <div class="meta-block">
              <strong>更新时间</strong>
              <div class="meta">${escapeHtml(item.updated_at || '-')}</div>
            </div>
          </div>
          <div class="note-line">${escapeHtml(item.notes || '无备注')}</div>
          <div class="actions">
            <button type="button" data-action="toggle-domain" data-id="${item.id}" class="secondary">${item.enabled ? '停用' : '启用'}</button>
            <button type="button" data-action="delete-domain" data-id="${item.id}" class="danger">删除</button>
          </div>
        </article>`).join('') : emptyState('暂无域名配置', '配置至少一个域名后，系统才能分配对应的临时邮箱地址。');
    }

    function resetInboxForm() {
      const form = document.getElementById('inbox-form');
      form.reset();
      form.elements.inbox_id.value = '';
      form.elements.enabled.checked = true;
      form.elements.imap_host.value = 'imap.gmail.com';
      form.elements.imap_port.value = '993';
      form.elements.app_password.required = true;
      form.elements.app_password.placeholder = '16 ? App Password';
      document.getElementById('inbox-submit-btn').textContent = '新增 Gmail 邮箱';
      document.getElementById('inbox-cancel-btn').style.display = 'none';
    }

    function startInboxEdit(item) {
      const form = document.getElementById('inbox-form');
      form.elements.inbox_id.value = String(item.id);
      form.elements.name.value = item.name || '';
      form.elements.email_address.value = item.email_address || '';
      form.elements.app_password.value = '';
      form.elements.app_password.required = false;
      form.elements.app_password.placeholder = '留空则保持当前密码';
      form.elements.imap_proxy_url.value = item.imap_proxy_url || '';
      form.elements.imap_host.value = item.imap_host || 'imap.gmail.com';
      form.elements.imap_port.value = item.imap_port || 993;
      form.elements.notes.value = item.notes || '';
      form.elements.enabled.checked = Boolean(item.enabled);
      document.getElementById('inbox-submit-btn').textContent = '保存 Gmail 邮箱';
      document.getElementById('inbox-cancel-btn').style.display = 'inline-flex';
      setStatus('inbox-status', `正在编辑：${item.name}`);
      showSection('overview');
      form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function renderMailboxList() {
      const wrap = document.getElementById('mailbox-list');
      document.getElementById('mailbox-result-count').textContent = `${state.mailboxes.length} 条结果`;
      wrap.innerHTML = state.mailboxes.length ? state.mailboxes.map((item) => `
        <article class="mailbox-row ${state.selectedMailboxId === item.id ? 'active' : ''}">
          <div class="mailbox-header">
            <div>
              <h4>${escapeHtml(item.email)}</h4>
              <div class="meta">域名 <code>${escapeHtml(item.domain)}</code> · Gmail ${escapeHtml(item.gmail_inbox_name)}</div>
            </div>
            ${subtleBadge(`${item.message_count} 封邮件`)}
          </div>
          <div class="mailbox-summary">
            <div class="meta">创建时间：${escapeHtml(item.created_at)}</div>
            <div class="meta">最近匹配：${escapeHtml(item.last_matched_at || '-')}</div>
            <div class="meta">最近收件：${escapeHtml(item.latest_received_at || '-')}</div>
          </div>
          <div class="actions">
            <button type="button" data-action="select-mailbox" data-id="${item.id}">${state.selectedMailboxId === item.id ? '已选中' : '查看详情'}</button>
            <button type="button" data-action="delete-mailbox" data-id="${item.id}" class="danger">删除临时邮箱</button>
          </div>
        </article>`).join('') : emptyState('没有找到临时邮箱', '当前没有匹配结果，可以调整搜索词或等待新邮箱生成。');
    }

    function renderMailboxDetail() {
      const title = document.getElementById('mailbox-detail-title');
      const meta = document.getElementById('mailbox-detail-meta');
      const refreshBtn = document.getElementById('mailbox-detail-refresh');
      const clearBtn = document.getElementById('mailbox-detail-clear');
      const deleteBtn = document.getElementById('mailbox-detail-delete');
      if (!state.selectedMailbox) {
        title.textContent = '临时邮箱详情';
        meta.textContent = '选择左侧临时邮箱后可查看邮件并执行清理。';
        refreshBtn.disabled = true;
        clearBtn.disabled = true;
        deleteBtn.disabled = true;
        document.getElementById('message-list').innerHTML = emptyState('暂无详情', '从左侧选择一个临时邮箱后，这里会展示邮件内容和操作入口。');
        return;
      }
      title.textContent = state.selectedMailbox.email;
      meta.innerHTML = `Gmail：<code>${escapeHtml(state.selectedMailbox.gmail_inbox_name)}</code> · 最近检查：${escapeHtml(state.selectedMailbox.last_checked_at || '-')} · 最近匹配：${escapeHtml(state.selectedMailbox.last_matched_at || '-')}`;
      refreshBtn.disabled = false;
      clearBtn.disabled = false;
      deleteBtn.disabled = false;
      document.getElementById('message-list').innerHTML = state.selectedMessages.length ? state.selectedMessages.map((item) => `
        <article class="message-card">
          <div class="message-header">
            <div>
              <h4>${escapeHtml(item.subject || '(无主题)')}</h4>
              <div class="meta">发件人：${escapeHtml(item.from || '-')} · 收件时间：${escapeHtml(item.received_at || '-')}</div>
            </div>
            <button type="button" data-action="delete-message" data-id="${item.id}" class="danger">删除邮件</button>
          </div>
          <div class="message-body">${escapeHtml((item.text_body || item.body || item.html_body || '').slice(0, 4000))}</div>
        </article>`).join('') : emptyState('当前没有缓存邮件', '该临时邮箱还没有命中任何邮件，或者邮件已经被清理。');
    }

    function renderSettings() {
      const settings = state.settings || {};
      const form = document.getElementById('settings-form');
      ['sync_interval_seconds', 'message_retention_minutes', 'mailbox_retention_minutes', 'cleanup_interval_seconds'].forEach((key) => {
        if (form.elements[key]) form.elements[key].value = settings[key] ?? '';
      });
      const status = state.status || {};
      document.getElementById('settings-summary').innerHTML = `
        <article class="data-card">
          <div class="data-card-header">
            <div>
              <h4>轮询状态</h4>
              <div class="meta">当前后台是否需要主动轮询 Gmail。</div>
            </div>
            ${status.gmail_polling_active ? badge('运行中') : badge('空闲', 'off')}
          </div>
          <div class="note-line">原因：${escapeHtml(status.gmail_polling_reason || '-')}</div>
        </article>
        <article class="data-card">
          <div class="data-card-header">
            <div>
              <h4>最近清理</h4>
              <div class="meta">最近一次清理任务启动时间。</div>
            </div>
            ${subtleBadge(`${status.cleanup_interval_seconds || '-'} 秒周期`)}
          </div>
          <div class="note-line">开始时间：${escapeHtml(status.last_cleanup_started_at || '-')}</div>
        </article>
      `;
    }

    async function loadOverview() {
      const [statusData, inboxData, domainData, settingsData] = await Promise.all([
        request('/api/admin/status'),
        request('/api/admin/gmail-inboxes'),
        request('/api/admin/domains'),
        request('/api/admin/settings'),
      ]);
      state.status = statusData.data || {};
      state.inboxes = inboxData.data?.inboxes || [];
      state.domains = domainData.data?.domains || [];
      state.settings = settingsData.data?.settings || {};
      renderMetrics();
      renderInboxOptions();
      renderInboxes();
      renderDomains();
      renderSettings();
    }

    async function loadMailboxes() {
      const keyword = document.getElementById('mailbox-search').value.trim();
      const data = await request(`/api/admin/mailboxes${keyword ? `?search=${encodeURIComponent(keyword)}` : ''}`);
      state.mailboxes = data.data?.mailboxes || [];
      if (!state.mailboxes.some((item) => item.id === state.selectedMailboxId)) {
        state.selectedMailboxId = state.mailboxes[0]?.id || null;
      }
      renderMailboxList();
      if (state.selectedMailboxId) {
        await loadMailboxMessages(state.selectedMailboxId);
      } else {
        state.selectedMailbox = null;
        state.selectedMessages = [];
        renderMailboxDetail();
      }
    }

    async function loadMailboxMessages(mailboxId) {
      const data = await request(`/api/admin/mailboxes/${mailboxId}/messages`);
      state.selectedMailboxId = mailboxId;
      state.selectedMailbox = data.data?.mailbox || null;
      state.selectedMessages = data.data?.messages || [];
      renderMailboxList();
      renderMailboxDetail();
    }

    document.querySelectorAll('.tab-btn').forEach((button) => {
      button.addEventListener('click', () => showSection(button.dataset.section));
    });

    document.getElementById('inbox-form').addEventListener('submit', async (event) => {
      const form = event.currentTarget;
      event.preventDefault();
      setStatus('inbox-status', '');
      try {
        const payload = formToObject(form);
        const inboxId = payload.inbox_id || null;
        delete payload.inbox_id;
        if (!payload.app_password) delete payload.app_password;
        if (inboxId) {
          await request(`/api/admin/gmail-inboxes/${inboxId}`, { method: 'PUT', body: JSON.stringify(payload) });
          setStatus('inbox-status', 'Gmail 邮箱已更新');
        } else {
          await request('/api/admin/gmail-inboxes', { method: 'POST', body: JSON.stringify(payload) });
          setStatus('inbox-status', 'Gmail 邮箱已创建');
        }
        resetInboxForm();
        await loadOverview();
      } catch (error) {
        setStatus('inbox-status', error.message, true);
      }
    });

    document.getElementById('inbox-cancel-btn').addEventListener('click', () => {
      resetInboxForm();
      setStatus('inbox-status', '');
    });

    document.getElementById('domain-form').addEventListener('submit', async (event) => {
      const form = event.currentTarget;
      event.preventDefault();
      setStatus('domain-status', '');
      try {
        await request('/api/admin/domains', { method: 'POST', body: JSON.stringify(formToObject(form)) });
        form.reset();
        form.elements.enabled.checked = true;
        setStatus('domain-status', '域名已创建');
        await loadOverview();
      } catch (error) {
        setStatus('domain-status', error.message, true);
      }
    });

    document.getElementById('settings-form').addEventListener('submit', async (event) => {
      const form = event.currentTarget;
      event.preventDefault();
      setStatus('settings-status', '');
      try {
        await request('/api/admin/settings', { method: 'POST', body: JSON.stringify(formToObject(form)) });
        setStatus('settings-status', '设置已保存');
        await loadOverview();
      } catch (error) {
        setStatus('settings-status', error.message, true);
      }
    });

    document.getElementById('sync-all-btn').addEventListener('click', async () => {
      try {
        await request('/api/admin/sync', { method: 'POST', body: '{}' });
        await Promise.all([loadOverview(), loadMailboxes()]);
      } catch (error) {
        window.alert(error.message);
      }
    });

    document.getElementById('cleanup-btn').addEventListener('click', async () => {
      try {
        await request('/api/admin/cleanup', { method: 'POST', body: '{}' });
        await Promise.all([loadOverview(), loadMailboxes()]);
      } catch (error) {
        window.alert(error.message);
      }
    });

    document.getElementById('mailbox-search-btn').addEventListener('click', async () => { await loadMailboxes(); });
    document.getElementById('mailbox-refresh-btn').addEventListener('click', async () => { await loadMailboxes(); });
    document.getElementById('mailbox-search').addEventListener('keydown', async (event) => { if (event.key === 'Enter') { event.preventDefault(); await loadMailboxes(); } });
    document.getElementById('mailbox-detail-refresh').addEventListener('click', async () => { if (state.selectedMailboxId) await loadMailboxMessages(state.selectedMailboxId); });
    document.getElementById('mailbox-detail-clear').addEventListener('click', async () => {
      if (!state.selectedMailboxId) return;
      if (!window.confirm('确认清空当前临时邮箱下的全部邮件吗？')) return;
      await request(`/api/admin/mailboxes/${state.selectedMailboxId}/messages`, { method: 'DELETE' });
      await Promise.all([loadOverview(), loadMailboxMessages(state.selectedMailboxId), loadMailboxes()]);
    });
    document.getElementById('mailbox-detail-delete').addEventListener('click', async () => {
      if (!state.selectedMailboxId) return;
      if (!window.confirm('确认删除当前临时邮箱及其所有缓存邮件吗？')) return;
      await request(`/api/admin/mailboxes/${state.selectedMailboxId}`, { method: 'DELETE' });
      state.selectedMailboxId = null;
      state.selectedMailbox = null;
      state.selectedMessages = [];
      await Promise.all([loadOverview(), loadMailboxes()]);
    });

    document.body.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const { action, id } = button.dataset;
      try {
        if (action === 'edit-inbox') {
          const item = state.inboxes.find((entry) => String(entry.id) === String(id));
          if (!item) throw new Error('未找到 Gmail 邮箱');
          startInboxEdit(item);
          return;
        }
        if (action === 'toggle-inbox') await request(`/api/admin/gmail-inboxes/${id}`, { method: 'PUT', body: JSON.stringify({ enabled: button.textContent.trim() === '启用' }) });
        if (action === 'sync-inbox') await request('/api/admin/sync', { method: 'POST', body: JSON.stringify({ gmail_inbox_id: Number(id) }) });
        if (action === 'delete-inbox') {
          if (!window.confirm('确认删除这个 Gmail 邮箱配置吗？')) return;
          await request(`/api/admin/gmail-inboxes/${id}`, { method: 'DELETE' });
          resetInboxForm();
        }
        if (action === 'toggle-domain') await request(`/api/admin/domains/${id}`, { method: 'PUT', body: JSON.stringify({ enabled: button.textContent.trim() === '启用' }) });
        if (action === 'delete-domain') {
          if (!window.confirm('确认删除这个域名吗？')) return;
          await request(`/api/admin/domains/${id}`, { method: 'DELETE' });
        }
        if (action === 'select-mailbox') {
          await loadMailboxMessages(Number(id));
          return;
        }
        if (action === 'delete-mailbox') {
          if (!window.confirm('确认删除这个临时邮箱及其全部缓存邮件吗？')) return;
          await request(`/api/admin/mailboxes/${id}`, { method: 'DELETE' });
          if (state.selectedMailboxId === Number(id)) {
            state.selectedMailboxId = null;
            state.selectedMailbox = null;
            state.selectedMessages = [];
          }
        }
        if (action === 'delete-message') {
          if (!window.confirm('确认删除这封邮件吗？')) return;
          await request(`/api/admin/messages/${encodeURIComponent(id)}`, { method: 'DELETE' });
          if (state.selectedMailboxId) await loadMailboxMessages(state.selectedMailboxId);
        }
        await Promise.all([loadOverview(), loadMailboxes()]);
      } catch (error) {
        window.alert(error.message);
      }
    });

    resetInboxForm();
    showSection('overview');
    Promise.all([loadOverview(), loadMailboxes()]).catch((error) => window.alert(error.message));
  </script>
</body>
</html>"""
    return HTMLResponse(page)



@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    cleanup_expired_data(force=True)
    start_sync_thread()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    stop_sync_thread()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return ok({"status": "ok", "version": APP_VERSION})


@app.get("/admin")
@app.get("/admin/login")
def admin_login_page(request: Request):
    if _is_admin_session(request):
        return RedirectResponse(url="/admin/console", status_code=302)
    return _render_login_page()


@app.post("/admin/login")
def admin_login_submit(username: str = Form(...), password: str = Form(...)):
    if username.strip() != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return _render_login_page("用户名或密码错误")
    token = _create_admin_session()
    response = RedirectResponse(url="/admin/console", status_code=302)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400)
    return response


@app.post("/admin/logout")
def admin_logout(request: Request):
    _clear_admin_session(request)
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/admin/console")
@app.get("/admin/domains")
def admin_console(request: Request):
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    return _render_admin_page()


@app.get("/api/generate-email")
def generate_email_get(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    mailbox = create_mailbox(None)
    return ok({"email": mailbox["email"]})


@app.post("/api/generate-email")
def generate_email_post(payload: GenerateEmailPayload, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    mailbox = create_mailbox(payload.domain)
    return ok({"email": mailbox["email"]})


@app.get("/api/emails")
def list_emails(email: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        return ok({"emails": list_messages_for_email(email.strip().lower())})
    except AdapterError as exc:
        return {"success": False, "error": str(exc)}


@app.get("/api/email/{email_id}")
def get_email(email_id: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email, raw_id = _decode_message_id(email_id)
        return ok(get_message_detail(email, raw_id))
    except AdapterError as exc:
        return {"success": False, "error": str(exc)}


@app.delete("/api/email/{email_id}")
def delete_email(email_id: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email, raw_id = _decode_message_id(email_id)
        delete_message(email, raw_id)
        return ok({"deleted": True})
    except AdapterError as exc:
        return {"success": False, "error": str(exc)}


@app.delete("/api/emails/clear")
def clear_emails(email: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        clear_mailbox(email.strip().lower())
        return ok({"deleted": True})
    except AdapterError as exc:
        return {"success": False, "error": str(exc)}


@app.get("/api/domains")
def public_domains(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    return ok({"domains": get_enabled_domains()})


@app.get("/api/admin/status")
def admin_status(request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok(get_status())


@app.get("/api/admin/gmail-inboxes")
def admin_list_gmail_inboxes(request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"inboxes": get_all_gmail_inboxes()})


@app.post("/api/admin/gmail-inboxes")
def admin_create_gmail_inbox(payload: GmailInboxCreatePayload, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"inbox": create_gmail_inbox(payload)})


@app.put("/api/admin/gmail-inboxes/{inbox_id}")
def admin_update_gmail_inbox(inbox_id: int, payload: GmailInboxUpdatePayload, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"inbox": update_gmail_inbox(inbox_id, payload)})


@app.delete("/api/admin/gmail-inboxes/{inbox_id}")
def admin_delete_gmail_inbox(inbox_id: int, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    delete_gmail_inbox(inbox_id)
    return ok({"deleted": True})


@app.get("/api/admin/domains")
def admin_list_domains(request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"domains": get_all_domains()})


@app.get("/api/admin/mailboxes")
def admin_list_mailboxes(
    request: Request,
    search: str | None = None,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"mailboxes": list_admin_mailboxes(search)})


@app.get("/api/admin/mailboxes/{mailbox_id}/messages")
def admin_list_mailbox_messages(
    mailbox_id: int,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    mailbox, messages = list_admin_messages(mailbox_id)
    return ok({"mailbox": mailbox, "messages": messages})


@app.delete("/api/admin/mailboxes/{mailbox_id}/messages")
def admin_clear_mailbox_messages(
    mailbox_id: int,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    clear_mailbox_by_id(mailbox_id)
    return ok({"cleared": True})


@app.delete("/api/admin/mailboxes/{mailbox_id}")
def admin_delete_mailbox(
    mailbox_id: int,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    delete_mailbox_by_id(mailbox_id)
    return ok({"deleted": True})


@app.delete("/api/admin/messages/{message_id}")
def admin_delete_message(
    message_id: str,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    email, raw_id = _decode_message_id(message_id)
    delete_message(email, raw_id)
    return ok({"deleted": True})


@app.get("/api/admin/settings")
def admin_get_settings(request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"settings": get_runtime_settings()})


@app.post("/api/admin/settings")
def admin_update_settings(
    payload: RuntimeSettingsPayload,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"settings": update_runtime_settings(payload)})


@app.post("/api/admin/cleanup")
def admin_cleanup_now(request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok(cleanup_expired_data(force=True))


@app.post("/api/admin/domains")
def admin_create_domain(payload: DomainCreatePayload, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"domain": create_domain(payload)})


@app.put("/api/admin/domains/{domain_id}")
def admin_update_domain(domain_id: int, payload: DomainUpdatePayload, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"domain": update_domain(domain_id, payload)})


@app.delete("/api/admin/domains/{domain_id}")
def admin_delete_domain(domain_id: int, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    delete_domain(domain_id)
    return ok({"deleted": True})


@app.post("/api/admin/sync")
def admin_sync_now(payload: SyncPayload, request: Request, x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    if payload.gmail_inbox_id is not None:
        sync_inbox(payload.gmail_inbox_id)
    else:
        sync_all_inboxes(force=True)
    return ok({"synced": True})
