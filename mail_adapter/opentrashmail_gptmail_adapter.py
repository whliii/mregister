from __future__ import annotations

import base64
import json
import os
import random
import re
import secrets
import sqlite3
import string
import tempfile
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel


APP_TITLE = "OpenTrashmail GPTMail Adapter"
APP_VERSION = "0.2.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(
    os.getenv("ADAPTER_DB_PATH", str(Path(tempfile.gettempdir()) / "mregister-mail-adapter" / "adapter_domains.db"))
).expanduser()

OPENTRASHMAIL_BASE_URL = os.getenv("OPENTRASHMAIL_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
OPENTRASHMAIL_PASSWORD = (os.getenv("OPENTRASHMAIL_PASSWORD") or "").strip()
ADAPTER_API_KEY = (os.getenv("ADAPTER_API_KEY") or "").strip()
ADAPTER_ADMIN_TOKEN = (os.getenv("ADAPTER_ADMIN_TOKEN") or ADAPTER_API_KEY).strip()
ADAPTER_INBOUND_TOKEN = (os.getenv("ADAPTER_INBOUND_TOKEN") or ADAPTER_API_KEY).strip()
ADMIN_USERNAME = (os.getenv("ADAPTER_WEB_USERNAME") or "admin").strip()
ADMIN_PASSWORD = (os.getenv("ADAPTER_WEB_PASSWORD") or "change-this-password").strip()
ALLOWED_DOMAINS_RAW = (os.getenv("ADAPTER_ALLOWED_DOMAINS") or "").strip()
DEFAULT_DOMAIN = (os.getenv("ADAPTER_DEFAULT_DOMAIN") or "").strip()
REQUEST_TIMEOUT = float(os.getenv("ADAPTER_TIMEOUT_SECONDS", "20") or 20)
SESSION_COOKIE = "adapter_admin_session"

EMAIL_LOCAL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$")

ALLOWED_DOMAINS = [x.strip().lower() for x in ALLOWED_DOMAINS_RAW.split(",") if x.strip()]
if not DEFAULT_DOMAIN:
    DEFAULT_DOMAIN = ALLOWED_DOMAINS[0] if ALLOWED_DOMAINS else "example.com"

db_lock = threading.RLock()
session_lock = threading.RLock()
admin_sessions: dict[str, str] = {}


class GenerateEmailPayload(BaseModel):
    prefix: str | None = None
    domain: str | None = None


class DomainCreatePayload(BaseModel):
    domain: str
    notes: str | None = None
    enabled: bool = True
    is_default: bool = False


class DomainUpdatePayload(BaseModel):
    notes: str | None = None
    enabled: bool | None = None
    is_default: bool | None = None


class CloudflareInboundPayload(BaseModel):
    mailbox: str
    message_id: str | None = None
    from_addr: str | None = None
    subject: str | None = None
    text_body: str | None = None
    html_body: str | None = None
    raw_content: str | None = None
    headers: dict[str, str] | list[dict[str, str]] | None = None
    received_at: str | None = None
    provider: str = "cloudflare-email-routing"


class AdapterError(RuntimeError):
    pass


app = FastAPI(title=APP_TITLE, version=APP_VERSION)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_domain_or_raise(domain: str) -> str:
    normalized = domain.strip().lower()
    if not DOMAIN_RE.match(normalized):
        raise AdapterError("Invalid email domain")
    return normalized


def _ensure_default_domain(conn: sqlite3.Connection) -> None:
    current = conn.execute("SELECT id FROM domains WHERE is_default = 1 LIMIT 1").fetchone()
    if current is not None:
        return
    row = conn.execute("SELECT id FROM domains WHERE enabled = 1 ORDER BY id ASC LIMIT 1").fetchone()
    if row is not None:
        conn.execute("UPDATE domains SET is_default = 1, updated_at = ? WHERE id = ?", (now_iso(), int(row["id"])))


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    journal_path = Path(f"{DB_PATH}-journal")
    if journal_path.exists():
        try:
            journal_path.unlink()
        except OSError:
            pass
    with db_lock, closing(_connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                notes TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox TEXT NOT NULL,
                provider TEXT NOT NULL,
                external_id TEXT,
                from_addr TEXT,
                subject TEXT,
                text_body TEXT,
                html_body TEXT,
                raw_content TEXT,
                headers_json TEXT,
                received_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(provider, mailbox, external_id)
            );
            """
        )
        timestamp = now_iso()
        for domain in ALLOWED_DOMAINS:
            conn.execute(
                """
                INSERT INTO domains (domain, notes, enabled, is_default, created_at, updated_at)
                VALUES (?, NULL, 1, 0, ?, ?)
                ON CONFLICT(domain) DO NOTHING
                """,
                (domain, timestamp, timestamp),
            )
        if DEFAULT_DOMAIN:
            conn.execute(
                """
                INSERT INTO domains (domain, notes, enabled, is_default, created_at, updated_at)
                VALUES (?, NULL, 1, 0, ?, ?)
                ON CONFLICT(domain) DO NOTHING
                """,
                (DEFAULT_DOMAIN.lower(), timestamp, timestamp),
            )
            conn.execute("UPDATE domains SET is_default = 0 WHERE is_default = 1")
            conn.execute(
                "UPDATE domains SET is_default = 1, enabled = 1, updated_at = ? WHERE domain = ?",
                (timestamp, DEFAULT_DOMAIN.lower()),
            )
        _ensure_default_domain(conn)
        conn.commit()


def row_to_domain(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "domain": str(row["domain"]),
        "notes": row["notes"],
        "enabled": bool(int(row["enabled"])),
        "is_default": bool(int(row["is_default"])),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_all_domains() -> list[dict[str, Any]]:
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM domains ORDER BY is_default DESC, enabled DESC, domain ASC").fetchall()
    return [row_to_domain(row) for row in rows]


def get_enabled_domains() -> list[dict[str, Any]]:
    return [item for item in get_all_domains() if item["enabled"]]


def choose_domain(requested_domain: str | None) -> str:
    with db_lock, closing(_connect()) as conn:
        if requested_domain:
            domain = _validate_domain_or_raise(requested_domain)
            row = conn.execute("SELECT * FROM domains WHERE domain = ?", (domain,)).fetchone()
            if row is None or not int(row["enabled"]):
                raise AdapterError("Domain is not available")
            return str(row["domain"])

        row = conn.execute(
            "SELECT * FROM domains WHERE enabled = 1 AND is_default = 1 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM domains WHERE enabled = 1 ORDER BY id ASC LIMIT 1").fetchone()
        if row is None:
            raise AdapterError("No enabled domain is configured")
        return str(row["domain"])


def create_domain(payload: DomainCreatePayload) -> dict[str, Any]:
    domain = _validate_domain_or_raise(payload.domain)
    timestamp = now_iso()
    with db_lock, closing(_connect()) as conn:
        existing = conn.execute("SELECT id FROM domains WHERE domain = ?", (domain,)).fetchone()
        if existing is not None:
            raise AdapterError("Domain already exists")
        if payload.is_default:
            conn.execute("UPDATE domains SET is_default = 0 WHERE is_default = 1")
        cursor = conn.execute(
            """
            INSERT INTO domains (domain, notes, enabled, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                domain,
                (payload.notes or "").strip() or None,
                1 if payload.enabled else 0,
                1 if payload.is_default else 0,
                timestamp,
                timestamp,
            ),
        )
        if payload.enabled and not payload.is_default:
            _ensure_default_domain(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM domains WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
    if row is None:
        raise AdapterError("Failed to create domain")
    return row_to_domain(row)


def update_domain(domain_id: int, payload: DomainUpdatePayload) -> dict[str, Any]:
    timestamp = now_iso()
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if row is None:
            raise AdapterError("Domain not found")

        enabled = int(row["enabled"]) if payload.enabled is None else (1 if payload.enabled else 0)
        is_default = int(row["is_default"]) if payload.is_default is None else (1 if payload.is_default else 0)
        notes = row["notes"] if payload.notes is None else ((payload.notes or "").strip() or None)

        if enabled == 0:
            is_default = 0

        if is_default:
            enabled = 1
            conn.execute("UPDATE domains SET is_default = 0 WHERE is_default = 1 AND id != ?", (domain_id,))

        conn.execute(
            """
            UPDATE domains
            SET notes = ?, enabled = ?, is_default = ?, updated_at = ?
            WHERE id = ?
            """,
            (notes, enabled, is_default, timestamp, domain_id),
        )
        _ensure_default_domain(conn)
        conn.commit()
        updated = conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
    if updated is None:
        raise AdapterError("Domain not found after update")
    return row_to_domain(updated)


def delete_domain(domain_id: int) -> None:
    with db_lock, closing(_connect()) as conn:
        row = conn.execute("SELECT id FROM domains WHERE id = ?", (domain_id,)).fetchone()
        if row is None:
            raise AdapterError("Domain not found")
        conn.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
        _ensure_default_domain(conn)
        conn.commit()


def _normalize_headers_for_storage(headers: dict[str, str] | list[dict[str, str]] | None) -> str | None:
    if headers is None:
        return None
    if isinstance(headers, dict):
        return json.dumps(headers, ensure_ascii=False)
    if isinstance(headers, list):
        normalized: dict[str, str] = {}
        for item in headers:
            if not isinstance(item, dict):
                continue
            key = str(item.get("name") or item.get("key") or "").strip()
            value = str(item.get("value") or "").strip()
            if key:
                normalized[key] = value
        return json.dumps(normalized, ensure_ascii=False)
    return None


def _local_message_token(message_row_id: int) -> str:
    return f"local-{message_row_id}"


def _is_local_message_token(raw_id: str) -> bool:
    return raw_id.startswith("local-") and raw_id[6:].isdigit()


def _decode_local_message_row_id(raw_id: str) -> int:
    if not _is_local_message_token(raw_id):
        raise AdapterError("Invalid local message id")
    return int(raw_id[6:])


def store_inbound_message(payload: CloudflareInboundPayload) -> dict[str, Any]:
    mailbox = payload.mailbox.strip().lower()
    if not filter_email(mailbox):
        raise AdapterError("Invalid mailbox")
    timestamp = now_iso()
    received_at = (payload.received_at or "").strip() or timestamp
    headers_json = _normalize_headers_for_storage(payload.headers)
    with db_lock, closing(_connect()) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO inbound_messages (
                mailbox, provider, external_id, from_addr, subject,
                text_body, html_body, raw_content, headers_json,
                received_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mailbox,
                (payload.provider or "cloudflare-email-routing").strip() or "cloudflare-email-routing",
                (payload.message_id or "").strip() or None,
                (payload.from_addr or "").strip() or None,
                (payload.subject or "").strip() or None,
                payload.text_body,
                payload.html_body,
                payload.raw_content,
                headers_json,
                received_at,
                timestamp,
            ),
        )
        conn.commit()
        if int(cursor.lastrowid or 0) > 0:
            row = conn.execute("SELECT * FROM inbound_messages WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM inbound_messages
                WHERE provider = ? AND mailbox = ? AND external_id IS ?
                ORDER BY id DESC LIMIT 1
                """,
                (
                    (payload.provider or "cloudflare-email-routing").strip() or "cloudflare-email-routing",
                    mailbox,
                    (payload.message_id or "").strip() or None,
                ),
            ).fetchone()
    if row is None:
        raise AdapterError("Failed to store inbound message")
    return row_to_local_message(row)


def row_to_local_message(row: sqlite3.Row) -> dict[str, Any]:
    headers: Any = None
    if row["headers_json"]:
        try:
            headers = json.loads(str(row["headers_json"]))
        except json.JSONDecodeError:
            headers = row["headers_json"]
    mailbox = str(row["mailbox"])
    return {
        "id": _encode_message_id(mailbox, _local_message_token(int(row["id"]))),
        "mail_id": _local_message_token(int(row["id"])),
        "email": mailbox,
        "provider": str(row["provider"]),
        "message_id": row["external_id"],
        "from": row["from_addr"],
        "subject": row["subject"],
        "body": row["text_body"],
        "text_body": row["text_body"],
        "html_body": row["html_body"],
        "raw": row["raw_content"],
        "headers": headers,
        "received_at": row["received_at"],
        "created_at": row["created_at"],
    }


def list_local_messages(mailbox: str) -> list[dict[str, Any]]:
    with db_lock, closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM inbound_messages WHERE mailbox = ? ORDER BY id DESC",
            (mailbox,),
        ).fetchall()
    return [row_to_local_message(row) for row in rows]


def get_local_message(mailbox: str, raw_id: str) -> dict[str, Any]:
    row_id = _decode_local_message_row_id(raw_id)
    with db_lock, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM inbound_messages WHERE id = ? AND mailbox = ?",
            (row_id, mailbox),
        ).fetchone()
    if row is None:
        raise AdapterError("Local message not found")
    return row_to_local_message(row)


def delete_local_message(mailbox: str, raw_id: str) -> None:
    row_id = _decode_local_message_row_id(raw_id)
    with db_lock, closing(_connect()) as conn:
        conn.execute("DELETE FROM inbound_messages WHERE id = ? AND mailbox = ?", (row_id, mailbox))
        conn.commit()


def clear_local_mailbox(mailbox: str) -> None:
    with db_lock, closing(_connect()) as conn:
        conn.execute("DELETE FROM inbound_messages WHERE mailbox = ?", (mailbox,))
        conn.commit()


def _require_api_key(x_api_key: str | None) -> None:
    # Keep behavior strict and explicit for automation use.
    if not ADAPTER_API_KEY:
        raise HTTPException(status_code=500, detail="ADAPTER_API_KEY is not configured")
    if (x_api_key or "").strip() != ADAPTER_API_KEY:
        raise HTTPException(status_code=401, detail="API key is invalid")


def _require_admin_token(x_admin_token: str | None) -> None:
    if not ADAPTER_ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADAPTER_ADMIN_TOKEN is not configured")
    if (x_admin_token or "").strip() != ADAPTER_ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Admin token is invalid")


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


def _require_inbound_token(x_inbound_token: str | None) -> None:
    if not ADAPTER_INBOUND_TOKEN:
        raise HTTPException(status_code=500, detail="ADAPTER_INBOUND_TOKEN is not configured")
    if (x_inbound_token or "").strip() != ADAPTER_INBOUND_TOKEN:
        raise HTTPException(status_code=401, detail="Inbound token is invalid")


def _sanitize_prefix(prefix: str | None) -> str:
    if prefix:
        p = prefix.strip().lower()
        p = re.sub(r"[^a-z0-9._-]", "", p)
        p = p[:63]
        if EMAIL_LOCAL_RE.match(p):
            return p
    first = random.choice(string.ascii_lowercase)
    rest = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(9))
    return first + rest


def _normalize_domain(domain: str | None) -> str:
    return choose_domain(domain)


def _auth_params() -> dict[str, str]:
    if not OPENTRASHMAIL_PASSWORD:
        return {}
    return {"password": OPENTRASHMAIL_PASSWORD}


def _auth_headers() -> dict[str, str]:
    if not OPENTRASHMAIL_PASSWORD:
        return {}
    return {"PWD": OPENTRASHMAIL_PASSWORD}


def _request_otm_json(path: str, *, method: str = "GET", params: dict[str, Any] | None = None) -> Any:
    url = f"{OPENTRASHMAIL_BASE_URL}{path}"
    merged_params = dict(params or {})
    merged_params.update(_auth_params())
    try:
        resp = requests.request(
            method=method,
            url=url,
            params=merged_params,
            headers={"Accept": "application/json", **_auth_headers()},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AdapterError(f"OpenTrashmail request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AdapterError(f"OpenTrashmail returned status {resp.status_code} for {path}")
    try:
        return resp.json()
    except ValueError as exc:
        raise AdapterError(f"OpenTrashmail returned non-JSON for {path}") from exc


def _encode_message_id(email: str, message_id: str) -> str:
    encoded_email = base64.urlsafe_b64encode(email.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded_email}.{message_id}"


def _decode_message_id(composite_id: str) -> tuple[str, str]:
    if "." not in composite_id:
        raise AdapterError("Invalid email id format")
    encoded_email, message_id = composite_id.split(".", 1)
    try:
        padded = encoded_email + "=" * ((4 - len(encoded_email) % 4) % 4)
        email = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise AdapterError("Invalid email id encoding") from exc
    if not filter_email(email):
        raise AdapterError("Invalid email in id")
    if not (str(message_id).isdigit() or _is_local_message_token(str(message_id))):
        raise AdapterError("Invalid message id")
    return email, message_id


def filter_email(email: str) -> bool:
    if "@" not in email:
        return False
    local, domain = email.split("@", 1)
    if not EMAIL_LOCAL_RE.match(local):
        return False
    if not DOMAIN_RE.match(domain):
        return False
    return True


def _normalize_otm_email_list(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        rows = list(raw.values())
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        email = str(row.get("email") or "").strip().lower()
        msg_id = str(row.get("id") or "").strip()
        if not email or not msg_id:
            continue
        item = dict(row)
        item["id"] = _encode_message_id(email, msg_id)
        item["mail_id"] = msg_id
        out.append(item)
    return out


def ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def fail(error: str) -> dict[str, Any]:
    return {"success": False, "error": error}


def _render_login_page(error: str | None = None) -> HTMLResponse:
    error_html = f'<div class="error">{error}</div>' if error else ""
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{APP_TITLE} Login</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: Arial, sans-serif;
      background: linear-gradient(160deg, #f0ead6, #dde7d6);
      color: #1f2a1f;
    }}
    .card {{
      width: min(92vw, 420px);
      background: rgba(255,255,255,.9);
      border: 1px solid #cfd7c5;
      border-radius: 18px;
      padding: 28px;
      box-shadow: 0 18px 40px rgba(0,0,0,.08);
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ margin: 0 0 18px; color: #4d5b4b; }}
    form {{ display: grid; gap: 12px; }}
    label {{ display: grid; gap: 6px; font-size: 14px; color: #4d5b4b; }}
    input {{
      border: 1px solid #cfd7c5;
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 12px 14px;
      font: inherit;
      cursor: pointer;
      color: #fff;
      background: #355e3b;
    }}
    .error {{
      margin-bottom: 12px;
      color: #9d2b2b;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <section class="card">
    <h1>域名管理登录</h1>
    <p>登录后可直接在网页里管理可用域名和默认域名。</p>
    {error_html}
    <form method="post" action="/admin/login">
      <label>
        <span>用户名</span>
        <input name="username" type="text" autocomplete="username" required>
      </label>
      <label>
        <span>密码</span>
        <input name="password" type="password" autocomplete="current-password" required>
      </label>
      <button type="submit">登录</button>
    </form>
  </section>
</body>
</html>"""
    return HTMLResponse(page)


@app.get("/admin")
@app.get("/admin/login")
def admin_login_page(request: Request):
    if _is_admin_session(request):
        return RedirectResponse(url="/admin/domains", status_code=302)
    return _render_login_page()


@app.post("/admin/login")
def admin_login_submit(username: str = Form(...), password: str = Form(...)):
    if username.strip() != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return _render_login_page("用户名或密码错误")
    token = _create_admin_session()
    response = RedirectResponse(url="/admin/domains", status_code=302)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400)
    return response


@app.post("/admin/logout")
def admin_logout(request: Request):
    _clear_admin_session(request)
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/admin/domains")
def admin_domains_page(request: Request):
    if not _is_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    return _render_admin_page()


def _render_admin_page() -> HTMLResponse:
    page = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Domain Admin</title>
  <style>
    :root {
      --bg: #f3efe5;
      --panel: #fffaf1;
      --line: #d7c9ad;
      --ink: #1f2419;
      --muted: #6a6f5e;
      --accent: #355e3b;
      --accent-2: #b26b2a;
      --danger: #9d2b2b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(178,107,42,.15), transparent 28%),
        linear-gradient(180deg, #f6f1e8, #ece3d2);
    }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 32px 20px 56px; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
    }
    .grid {
      display: grid;
      gap: 20px;
      grid-template-columns: 360px 1fr;
    }
    .card {
      background: rgba(255,250,241,.9);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px;
      box-shadow: 0 18px 50px rgba(40, 35, 24, .08);
    }
    .stack { display: grid; gap: 14px; }
    label { display: grid; gap: 6px; font-size: 14px; color: var(--muted); }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
      background: #fffdf8;
      color: var(--ink);
    }
    textarea { min-height: 86px; resize: vertical; }
    button {
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      cursor: pointer;
      color: #fff;
      background: var(--accent);
    }
    button.secondary { background: var(--accent-2); }
    button.danger { background: var(--danger); }
    button.ghost {
      background: transparent;
      color: var(--ink);
      border: 1px solid var(--line);
    }
    .toolbar, .row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .table { display: grid; gap: 12px; }
    .domain {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255,255,255,.72);
    }
    .domain h3 {
      margin: 0 0 6px;
      font-size: 20px;
    }
    .meta {
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .badge {
      display: inline-block;
      border-radius: 999px;
      padding: 3px 10px;
      margin-right: 8px;
      background: #e7efdf;
      color: var(--accent);
    }
    .badge.off {
      background: #f1e0db;
      color: var(--danger);
    }
    .notes {
      color: var(--muted);
      margin-bottom: 12px;
      white-space: pre-wrap;
    }
    .status {
      min-height: 24px;
      color: var(--muted);
      font-size: 14px;
    }
    @media (max-width: 920px) {
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1 style="margin:0 0 6px;">域名管理系统</h1>
        <div class="meta">登录态管理，不再要求手动填管理 Token。</div>
      </div>
      <form method="post" action="/admin/logout">
        <button class="ghost" type="submit">退出登录</button>
      </form>
    </div>
    <section class="grid">
      <article class="card stack">
        <form id="create-form" class="stack">
          <label>
            <span>域名</span>
            <input name="domain" type="text" placeholder="mail.example.com" required>
          </label>
          <label>
            <span>备注</span>
            <textarea name="notes" placeholder="可选备注"></textarea>
          </label>
          <label class="row">
            <input name="enabled" type="checkbox" checked>
            <span>启用</span>
          </label>
          <label class="row">
            <input name="is_default" type="checkbox">
            <span>设为默认域名</span>
          </label>
          <button type="submit">新增域名</button>
        </form>
        <div class="status" id="status"></div>
      </article>
      <article class="card">
        <div class="toolbar" style="justify-content:space-between;margin-bottom:12px;">
          <strong>域名列表</strong>
          <span id="summary" class="meta"></span>
        </div>
        <div class="table" id="domains"></div>
      </article>
    </section>
  </div>
  <script>
    const statusEl = document.getElementById('status');
    const domainsEl = document.getElementById('domains');
    const summaryEl = document.getElementById('summary');
    function setStatus(message) { statusEl.textContent = message || ''; }
    async function request(path, options = {}) {
      const response = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        ...options,
      });
      const data = await response.json().catch(() => ({ detail: 'Request failed' }));
      if (!response.ok) throw new Error(data.detail || data.error || 'Request failed');
      if (data.success === false) throw new Error(data.error || 'Request failed');
      return data;
    }
    function esc(v) {
      return String(v || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    }
    function domainCard(item) {
      const notes = item.notes ? `<div class="notes">${esc(item.notes)}</div>` : '';
      const enabledLabel = item.enabled ? '<span class="badge">已启用</span>' : '<span class="badge off">已停用</span>';
      const defaultLabel = item.is_default ? '<span class="badge">默认域名</span>' : '';
      return `
        <div class="domain">
          <h3>${esc(item.domain)}</h3>
          <div class="meta">${enabledLabel}${defaultLabel}创建于 ${esc(item.created_at)}</div>
          ${notes}
          <div class="toolbar">
            <button type="button" data-action="toggle" data-id="${item.id}" class="ghost">${item.enabled ? '停用' : '启用'}</button>
            <button type="button" data-action="default" data-id="${item.id}" class="secondary">设为默认</button>
            <button type="button" data-action="delete" data-id="${item.id}" class="danger">删除</button>
          </div>
        </div>
      `;
    }
    async function refreshDomains() {
      setStatus('正在刷新...');
      const data = await request('/api/admin/domains');
      const items = data.data.domains || [];
      summaryEl.textContent = `共 ${items.length} 个域名`;
      domainsEl.innerHTML = items.length ? items.map(domainCard).join('') : '<p class="meta">暂无域名</p>';
      setStatus('');
    }
    document.getElementById('create-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const formEl = event.currentTarget;
      const form = new FormData(formEl);
      const payload = {
        domain: form.get('domain'),
        notes: form.get('notes'),
        enabled: form.get('enabled') === 'on',
        is_default: form.get('is_default') === 'on',
      };
      try {
        await request('/api/admin/domains', { method: 'POST', body: JSON.stringify(payload) });
        formEl.reset();
        setStatus('域名已新增');
        await refreshDomains();
      } catch (error) {
        setStatus(error.message);
      }
    });
    domainsEl.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const id = button.dataset.id;
      const action = button.dataset.action;
      try {
        if (action === 'toggle') {
          const enabled = button.textContent.trim() === '启用';
          await request(`/api/admin/domains/${id}`, { method: 'PUT', body: JSON.stringify({ enabled }) });
        } else if (action === 'default') {
          await request(`/api/admin/domains/${id}`, { method: 'PUT', body: JSON.stringify({ is_default: true }) });
        } else if (action === 'delete') {
          if (!window.confirm('删除后该域名将不可再用于生成邮箱，确认继续？')) return;
          await request(`/api/admin/domains/${id}`, { method: 'DELETE' });
        }
        await refreshDomains();
      } catch (error) {
        setStatus(error.message);
      }
    });
    refreshDomains().catch((error) => setStatus(error.message));
  </script>
</body>
</html>"""
    return HTMLResponse(page)


@app.get("/api/generate-email")
def generate_email_get(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email = f"{_sanitize_prefix(None)}@{_normalize_domain(None)}"
        return ok({"email": email})
    except AdapterError as exc:
        return fail(str(exc))


@app.post("/api/generate-email")
def generate_email_post(
    payload: GenerateEmailPayload,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email = f"{_sanitize_prefix(payload.prefix)}@{_normalize_domain(payload.domain)}"
        return ok({"email": email})
    except AdapterError as exc:
        return fail(str(exc))


@app.get("/api/domains")
def list_enabled_domain_pool(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    return ok({"domains": get_enabled_domains()})


@app.get("/api/admin/domains")
def admin_list_domains(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    return ok({"domains": get_all_domains()})


@app.post("/api/admin/domains")
def admin_create_domain(
    request: Request,
    payload: DomainCreatePayload,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    try:
        return ok({"domain": create_domain(payload)})
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/admin/domains/{domain_id}")
def admin_update_domain(
    request: Request,
    domain_id: int,
    payload: DomainUpdatePayload,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    try:
        return ok({"domain": update_domain(domain_id, payload)})
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/admin/domains/{domain_id}")
def admin_delete_domain(
    request: Request,
    domain_id: int,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _require_admin_access(request, x_admin_token)
    try:
        delete_domain(domain_id)
        return ok({"deleted": True})
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/inbound/cloudflare-email")
def inbound_cloudflare_email(
    payload: CloudflareInboundPayload,
    x_inbound_token: str | None = Header(default=None, alias="X-Inbound-Token"),
) -> dict[str, Any]:
    _require_inbound_token(x_inbound_token)
    try:
        item = store_inbound_message(payload)
        return ok({"message": item})
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/emails")
def list_emails(email: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email = email.strip().lower()
        if not filter_email(email):
            raise AdapterError("Invalid email")
        local_messages = list_local_messages(email)
        if local_messages:
            return ok({"emails": local_messages})
        raw = _request_otm_json(f"/json/{email}")
        return ok({"emails": _normalize_otm_email_list(raw)})
    except AdapterError as exc:
        return fail(str(exc))


@app.get("/api/email/{email_id}")
def get_email(email_id: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email, raw_id = _decode_message_id(email_id)
        if _is_local_message_token(raw_id):
            return ok(get_local_message(email, raw_id))
        detail = _request_otm_json(f"/json/{email}/{raw_id}")
        return ok(detail)
    except AdapterError as exc:
        return fail(str(exc))


@app.delete("/api/email/{email_id}")
def delete_email(email_id: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email, raw_id = _decode_message_id(email_id)
        if _is_local_message_token(raw_id):
            delete_local_message(email, raw_id)
        else:
            _ = _request_otm_json(f"/api/delete/{email}/{raw_id}", method="DELETE")
        return ok({"deleted": True})
    except AdapterError as exc:
        return fail(str(exc))


@app.delete("/api/emails/clear")
def clear_emails(email: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_api_key(x_api_key)
    try:
        email = email.strip().lower()
        if not filter_email(email):
            raise AdapterError("Invalid email")
        clear_local_mailbox(email)
        try:
            _ = _request_otm_json(f"/api/deleteaccount/{email}", method="DELETE")
        except AdapterError:
            pass
        return ok({"cleared": True})
    except AdapterError as exc:
        return fail(str(exc))


init_db()
