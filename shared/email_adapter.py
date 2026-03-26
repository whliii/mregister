import json
import math
import os
import random
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from curl_cffi import requests as curl_requests


SUPPORTED_EMAIL_KINDS = {"gptmail", "duckmail", "tempmail_lol", "mail_tm", "mail_gw", "cfmail"}

DEFAULTS = {
    "duckmail_api_base": "https://api.duckmail.sbs",
    "duckmail_bearer": "",
    "mail_tm_api_base": "https://api.mail.tm",
    "mail_tm_domain": "",
    "mail_gw_api_base": "https://api.mail.gw",
    "mail_gw_domain": "",
    "tempmail_lol_api_base": "https://api.tempmail.lol/v2",
    "gptmail_api_base": "",
    "gptmail_api_key": "",
    "gptmail_domain": "",
    "cfmail_config_path": "",
    "cfmail_profile": "auto",
}


@dataclass(frozen=True)
class CfmailAccount:
    name: str
    worker_domain: str
    email_domain: str
    admin_password: str


_cfmail_account_lock = threading.Lock()
_cfmail_account_index = 0
_cfmail_reload_lock = threading.Lock()
_cfmail_failure_lock = threading.Lock()
_CFMAIL_CONFIG_PATH = ""
CFMAIL_PROFILE_MODE = "auto"
CFMAIL_ACCOUNTS: list[CfmailAccount] = []
CFMAIL_HOT_RELOAD_ENABLED = True
CFMAIL_CONFIG_MTIME: float | None = None
CFMAIL_FAIL_THRESHOLD = 3
CFMAIL_COOLDOWN_SECONDS = 1800
CFMAIL_FAILURE_STATE: dict[str, dict[str, Any]] = {}


def load_email_credentials_from_file(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        raise Exception(f"读取邮箱凭证文件失败: {path} - {exc}")
    if not isinstance(payload, list):
        raise Exception("邮箱凭证文件格式无效，必须是数组")
    items: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in SUPPORTED_EMAIL_KINDS and kind != "cfmail":
            continue
        items.append(
            {
                "id": raw.get("id"),
                "name": str(raw.get("name") or kind),
                "kind": kind,
                "api_key": str(raw.get("api_key") or ""),
                "base_url": str(raw.get("base_url") or ""),
                "prefix": str(raw.get("prefix") or ""),
                "domain": str(raw.get("domain") or ""),
                "notes": str(raw.get("notes") or ""),
            }
        )
    return items


def _normalize_host(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("https://"):
        normalized = normalized[len("https://") :]
    elif normalized.startswith("http://"):
        normalized = normalized[len("http://") :]
    return normalized.strip().strip("/")


def _normalize_cfmail_account(raw: dict[str, Any]) -> Optional[CfmailAccount]:
    if not isinstance(raw, dict):
        return None
    if not raw.get("enabled", True):
        return None
    name = str(raw.get("name") or "").strip()
    worker_domain = _normalize_host(raw.get("worker_domain") or raw.get("WORKER_DOMAIN") or "")
    email_domain = _normalize_host(raw.get("email_domain") or raw.get("EMAIL_DOMAIN") or "")
    admin_password = str(raw.get("admin_password") or raw.get("ADMIN_PASSWORD") or "").strip()
    if not name or not worker_domain or not email_domain or not admin_password:
        return None
    return CfmailAccount(
        name=name,
        worker_domain=worker_domain,
        email_domain=email_domain,
        admin_password=admin_password,
    )


def load_cfmail_accounts_from_file(config_path: str, *, silent: bool = False) -> list[dict[str, Any]]:
    path = str(config_path or "").strip()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        if not silent:
            print(f"[警告] 读取 cfmail 配置文件失败: {path}，错误: {exc}")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            return accounts
    if not silent:
        print(f"[警告] cfmail 配置文件格式无效: {path}")
    return []


def build_cfmail_accounts(raw_accounts: list[Any]) -> list[CfmailAccount]:
    accounts: list[CfmailAccount] = []
    seen_names: set[str] = set()
    for item in raw_accounts:
        account = _normalize_cfmail_account(item)
        if not account:
            continue
        key = account.name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        accounts.append(account)

    env_worker_domain = _normalize_host(os.getenv("CFMAIL_WORKER_DOMAIN", ""))
    env_email_domain = _normalize_host(os.getenv("CFMAIL_EMAIL_DOMAIN", ""))
    env_admin_password = str(os.getenv("CFMAIL_ADMIN_PASSWORD", "")).strip()
    env_profile_name = str(os.getenv("CFMAIL_PROFILE_NAME", "default")).strip() or "default"
    if env_worker_domain and env_email_domain and env_admin_password:
        env_account = CfmailAccount(
            name=env_profile_name,
            worker_domain=env_worker_domain,
            email_domain=env_email_domain,
            admin_password=env_admin_password,
        )
        env_key = env_account.name.lower()
        accounts = [item for item in accounts if item.name.lower() != env_key]
        accounts.insert(0, env_account)
    return accounts


def cfmail_headers(*, jwt: str = "", use_json: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if use_json:
        headers["Content-Type"] = "application/json"
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    return headers


def _cfmail_skip_remaining_seconds(account_name: str) -> int:
    key = str(account_name or "").strip().lower()
    if not key:
        return 0
    with _cfmail_failure_lock:
        state = CFMAIL_FAILURE_STATE.get(key) or {}
        cooldown_until = float(state.get("cooldown_until") or 0)
    remaining = int(math.ceil(cooldown_until - time.time()))
    return max(0, remaining)


def record_cfmail_success(account_name: str) -> None:
    key = str(account_name or "").strip().lower()
    if not key:
        return
    with _cfmail_failure_lock:
        state = CFMAIL_FAILURE_STATE.setdefault(key, {"name": account_name})
        state["consecutive_failures"] = 0
        state["cooldown_until"] = 0
        state["last_error"] = ""
        state["last_success_at"] = time.time()


def record_cfmail_failure(account_name: str, reason: str = "") -> None:
    key = str(account_name or "").strip().lower()
    if not key:
        return
    now = time.time()
    with _cfmail_failure_lock:
        state = CFMAIL_FAILURE_STATE.setdefault(key, {"name": account_name})
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["last_error"] = str(reason or "")[:300]
        state["last_failed_at"] = now
        if state["consecutive_failures"] >= CFMAIL_FAIL_THRESHOLD:
            state["cooldown_until"] = max(float(state.get("cooldown_until") or 0), now + CFMAIL_COOLDOWN_SECONDS)
            state["consecutive_failures"] = 0
            remaining = int(math.ceil(state["cooldown_until"] - now))
            print(f"[警告] cfmail 配置 {account_name} 连续失败，已跳过 {remaining} 秒")


def configure_cfmail_defaults(config_path: str, profile_mode: str) -> None:
    global _CFMAIL_CONFIG_PATH, CFMAIL_PROFILE_MODE, CFMAIL_ACCOUNTS, CFMAIL_CONFIG_MTIME, _cfmail_account_index
    _CFMAIL_CONFIG_PATH = str(config_path or "").strip()
    CFMAIL_PROFILE_MODE = str(profile_mode or "auto").strip() or "auto"
    raw_accounts = load_cfmail_accounts_from_file(_CFMAIL_CONFIG_PATH, silent=True)
    CFMAIL_ACCOUNTS = build_cfmail_accounts(raw_accounts)
    _cfmail_account_index = 0
    CFMAIL_CONFIG_MTIME = os.path.getmtime(_CFMAIL_CONFIG_PATH) if _CFMAIL_CONFIG_PATH and os.path.exists(_CFMAIL_CONFIG_PATH) else None


def reload_cfmail_accounts_if_needed(force: bool = False) -> bool:
    global CFMAIL_CONFIG_MTIME, CFMAIL_ACCOUNTS, _cfmail_account_index
    if not CFMAIL_HOT_RELOAD_ENABLED:
        return False
    config_path = _CFMAIL_CONFIG_PATH
    if not config_path:
        return False
    try:
        mtime = os.path.getmtime(config_path)
    except OSError:
        return False
    with _cfmail_reload_lock:
        if not force and CFMAIL_CONFIG_MTIME == mtime:
            return False
        raw_accounts = load_cfmail_accounts_from_file(config_path)
        new_accounts = build_cfmail_accounts(raw_accounts)
        if not new_accounts:
            CFMAIL_CONFIG_MTIME = mtime
            return False
        CFMAIL_ACCOUNTS = new_accounts
        _cfmail_account_index = 0
        CFMAIL_CONFIG_MTIME = mtime
        return True


def select_cfmail_account(profile_name: str = "auto") -> Optional[CfmailAccount]:
    global _cfmail_account_index
    accounts = CFMAIL_ACCOUNTS
    if not accounts:
        return None
    selected_name = str(profile_name or "auto").strip()
    if selected_name and selected_name.lower() != "auto":
        for account in accounts:
            if account.name.lower() == selected_name.lower():
                return account
        return None
    with _cfmail_account_lock:
        start_index = _cfmail_account_index % len(accounts)
        for offset in range(len(accounts)):
            index = (start_index + offset) % len(accounts)
            account = accounts[index]
            if _cfmail_skip_remaining_seconds(account.name) > 0:
                continue
            _cfmail_account_index = (index + 1) % len(accounts)
            return account
    return None


def extract_generic_code(content: str) -> str | None:
    if not content:
        return None
    patterns = [
        r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        r"code[:：]?\s*([A-Z0-9]{3}-[A-Z0-9]{3})",
        r"verification[^A-Z0-9]*([A-Z0-9]{3}-[A-Z0-9]{3})",
        r">\s*([A-Z0-9]{3}-[A-Z0-9]{3})\s*<",
        r"Verification code:?\s*(\d{6})",
        r"code is\s*(\d{6})",
        r"代码为[:：]?\s*(\d{6})",
        r"验证码[:：]?\s*(\d{6})",
        r">\s*(\d{6})\s*<",
        r"(?<![#&])\b(\d{6})\b",
    ]
    upper = content.upper()
    for pattern in patterns:
        matches = re.findall(pattern, upper, re.I | re.S)
        for code in matches:
            normalized = str(code).replace("-", "").strip().upper()
            if normalized and normalized != "177010":
                return normalized
    return None


class UnifiedEmailAdapter:
    def __init__(
        self,
        *,
        proxy: str = "",
        impersonate: str = "chrome",
        logger: Callable[[str], None] | None = None,
        defaults: dict[str, Any] | None = None,
    ):
        config = dict(DEFAULTS)
        if defaults:
            config.update({k: v for k, v in defaults.items() if v is not None})
        self.proxy = str(proxy or "").strip()
        self.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.impersonate = impersonate
        self._logger = logger
        self._gptmail_api_base = str(config.get("gptmail_api_base") or "").strip().rstrip("/")
        self._gptmail_api_key = str(config.get("gptmail_api_key") or "")
        self._gptmail_domain = str(config.get("gptmail_domain") or "").strip()
        self._duckmail_api_base = str(config.get("duckmail_api_base") or DEFAULTS["duckmail_api_base"]).strip().rstrip("/")
        self._duckmail_bearer = str(config.get("duckmail_bearer") or "")
        self._mail_tm_api_base = str(config.get("mail_tm_api_base") or DEFAULTS["mail_tm_api_base"]).strip().rstrip("/")
        self._mail_tm_domain = str(config.get("mail_tm_domain") or "").strip()
        self._mail_gw_api_base = str(config.get("mail_gw_api_base") or DEFAULTS["mail_gw_api_base"]).strip().rstrip("/")
        self._mail_gw_domain = str(config.get("mail_gw_domain") or "").strip()
        self._tempmail_lol_api_base = str(config.get("tempmail_lol_api_base") or DEFAULTS["tempmail_lol_api_base"]).strip().rstrip("/")
        self._cfmail_config_path = str(config.get("cfmail_config_path") or "").strip()
        self._cfmail_profile_mode = str(config.get("cfmail_profile") or "auto").strip() or "auto"
        self._cfmail_api_base = ""
        self._cfmail_account_name = ""
        self._cfmail_mail_token = ""
        configure_cfmail_defaults(self._cfmail_config_path, self._cfmail_profile_mode)

    def apply_email_credential(self, credential: dict[str, Any] | None) -> None:
        if not credential:
            return
        kind = str(credential.get("kind") or "").strip().lower()
        if kind == "gptmail":
            self._gptmail_api_base = str(credential.get("base_url") or self._gptmail_api_base).strip().rstrip("/")
            self._gptmail_api_key = str(credential.get("api_key") or self._gptmail_api_key)
            self._gptmail_domain = str(credential.get("domain") or self._gptmail_domain).strip()
        elif kind == "duckmail":
            self._duckmail_api_base = str(credential.get("base_url") or self._duckmail_api_base).strip().rstrip("/")
            self._duckmail_bearer = str(credential.get("api_key") or self._duckmail_bearer)
        elif kind == "mail_tm":
            self._mail_tm_api_base = str(credential.get("base_url") or self._mail_tm_api_base).strip().rstrip("/")
            self._mail_tm_domain = str(credential.get("domain") or self._mail_tm_domain).strip()
        elif kind == "mail_gw":
            self._mail_gw_api_base = str(credential.get("base_url") or self._mail_gw_api_base).strip().rstrip("/")
            self._mail_gw_domain = str(credential.get("domain") or self._mail_gw_domain).strip()
        elif kind == "tempmail_lol":
            self._tempmail_lol_api_base = str(credential.get("base_url") or self._tempmail_lol_api_base).strip().rstrip("/")
        elif kind == "cfmail":
            self._cfmail_config_path = str(credential.get("base_url") or self._cfmail_config_path).strip()
            self._cfmail_profile_mode = str(credential.get("domain") or self._cfmail_profile_mode).strip() or "auto"
            configure_cfmail_defaults(self._cfmail_config_path, self._cfmail_profile_mode)

    def create_email_mailbox(self, provider: str) -> tuple[str, str, str]:
        normalized = str(provider or "").strip().lower()
        if normalized == "gptmail":
            return self._create_gptmail_email()
        if normalized == "duckmail":
            return self._create_duckmail_email()
        if normalized == "tempmail_lol":
            return self._create_tempmail_lol_email()
        if normalized == "mail_gw":
            return self._create_mailtm_compatible_email(self._mail_gw_api_base, preferred_domain=self._mail_gw_domain)
        if normalized == "mail_tm":
            return self._create_mailtm_compatible_email(self._mail_tm_api_base, preferred_domain=self._mail_tm_domain)
        if normalized == "cfmail":
            return self._create_cfmail_email()
        raise Exception(f"不支持的邮箱 provider: {provider}")

    def wait_for_code(
        self,
        *,
        provider: str,
        mail_token: str,
        email: str = "",
        timeout: int = 120,
        extractor: Callable[[str], str | None] | None = None,
        message_filter: Callable[[str], bool] | None = None,
        poll_interval: int = 3,
    ) -> str | None:
        effective_extractor = extractor or extract_generic_code
        start_time = time.time()
        seen_ids: set[str] = set()
        while time.time() - start_time < timeout:
            code = self._extract_code_for_provider(
                provider=provider,
                mail_token=mail_token,
                email=email,
                seen_ids=seen_ids,
                extractor=effective_extractor,
                message_filter=message_filter,
            )
            if code:
                return code
            time.sleep(max(1, poll_interval))
        return None

    def _log(self, message: str) -> None:
        if self._logger:
            self._logger(message)

    def _create_mailtm_compatible_session(self):
        session = curl_requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
        if self.proxies:
            session.proxies = self.proxies
        return session

    def _resolve_mailtm_compatible_domain(self, api_base: str, preferred_domain: str = "") -> str:
        session = self._create_mailtm_compatible_session()
        resp = session.get(
            f"{api_base.rstrip('/')}/domains",
            headers={"Accept": "application/json"},
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code != 200:
            raise Exception(f"读取域名列表失败: {resp.status_code} - {resp.text[:200]}")
        data = resp.json() if resp.content else {}
        if isinstance(data, list):
            domains = data
        elif isinstance(data, dict):
            domains = data.get("hydra:member") or data.get("member") or data.get("data") or []
        else:
            domains = []
        if not isinstance(domains, list):
            domains = []

        preferred = str(preferred_domain or "").strip().lower()
        chosen = ""
        for item in domains:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip().lower()
            is_active = item.get("isActive", True)
            is_private = item.get("isPrivate", False)
            if not domain or is_private or is_active is False:
                continue
            if preferred and domain == preferred:
                return domain
            if not chosen:
                chosen = domain
        if preferred:
            raise Exception(f"指定域名不可用: {preferred}")
        if not chosen:
            raise Exception("没有可用域名")
        return chosen

    def _create_mailtm_compatible_email(self, api_base: str, preferred_domain: str = "") -> tuple[str, str, str]:
        api_base = api_base.rstrip("/")
        if not api_base:
            raise Exception("Mail.tm compatible API Base 未设置")
        domain = self._resolve_mailtm_compatible_domain(api_base, preferred_domain)
        chars = string.ascii_lowercase + string.digits
        email_local = "".join(random.choice(chars) for _ in range(random.randint(8, 13)))
        email = f"{email_local}@{domain}"
        password = secrets.token_urlsafe(18)
        session = self._create_mailtm_compatible_session()
        resp = session.post(
            f"{api_base}/accounts",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"address": email, "password": password},
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"创建邮箱失败: {resp.status_code} - {resp.text[:200]}")
        token_resp = session.post(
            f"{api_base}/token",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"address": email, "password": password},
            timeout=15,
            impersonate=self.impersonate,
        )
        if token_resp.status_code != 200:
            raise Exception(f"获取 Token 失败: {token_resp.status_code} - {token_resp.text[:200]}")
        data = token_resp.json() if token_resp.content else {}
        token = str(data.get("token") or "").strip()
        if not token:
            raise Exception("返回 token 为空")
        return email, password, token

    def _fetch_messages_mailtm_compatible(self, api_base: str, mail_token: str):
        session = self._create_mailtm_compatible_session()
        resp = session.get(
            f"{api_base.rstrip('/')}/messages",
            headers={"Authorization": f"Bearer {mail_token}", "Accept": "application/json"},
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code != 200:
            return []
        data = resp.json() if resp.content else {}
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("hydra:member") or data.get("member") or data.get("data") or []
        return []

    def _fetch_email_detail_mailtm_compatible(self, api_base: str, mail_token: str, msg_id: str):
        if isinstance(msg_id, str) and msg_id.startswith("/messages/"):
            msg_id = msg_id.split("/")[-1]
        session = self._create_mailtm_compatible_session()
        resp = session.get(
            f"{api_base.rstrip('/')}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {mail_token}", "Accept": "application/json"},
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code == 200:
            return resp.json() if resp.content else {}
        return None

    def _create_gptmail_email(self):
        if not self._gptmail_api_base or not self._gptmail_api_key:
            raise Exception("GPTMail 凭证缺少 base_url 或 api_key")
        payload = {"domain": self._gptmail_domain} if self._gptmail_domain else {}
        resp = curl_requests.post(
            f"{self._gptmail_api_base.rstrip('/')}/api/generate-email",
            json=payload,
            headers={"X-API-Key": self._gptmail_api_key, "Content-Type": "application/json"},
            proxies=self.proxies,
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"GPTMail 创建失败: {resp.status_code} - {resp.text[:200]}")
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"GPTMail 创建失败: {data}")
        email = data.get("data", {}).get("email", "")
        if not email:
            raise Exception("GPTMail 返回 email 为空")
        self._log(f"[gptmail] 创建邮箱成功: {email}")
        return email, "", email

    def _fetch_emails_gptmail(self, mail_token: str):
        if not self._gptmail_api_base or not self._gptmail_api_key:
            return []
        resp = curl_requests.get(
            f"{self._gptmail_api_base.rstrip('/')}/api/emails",
            params={"email": mail_token},
            headers={"X-API-Key": self._gptmail_api_key},
            proxies=self.proxies,
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data.get("success"):
            return []
        emails = data.get("data", {}).get("emails", [])
        return emails if isinstance(emails, list) else []

    def _create_tempmail_lol_email(self):
        resp = curl_requests.post(
            f"{self._tempmail_lol_api_base.rstrip('/')}/inbox/create",
            json={},
            proxies=self.proxies,
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code not in (200, 201):
            raise Exception(f"TempMail.lol 创建失败: {resp.status_code} - {resp.text[:200]}")
        data = resp.json() if resp.content else {}
        email = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or "").strip()
        if not email or not token:
            raise Exception("TempMail.lol 返回数据不完整（address/email 或 token 为空）")
        self._log(f"[tempmail_lol] 创建邮箱成功: {email}")
        return email, "", token

    def _fetch_emails_tempmail_lol(self, mail_token: str):
        resp = curl_requests.get(
            f"{self._tempmail_lol_api_base.rstrip('/')}/inbox",
            params={"token": mail_token},
            proxies=self.proxies,
            timeout=15,
            impersonate=self.impersonate,
        )
        if resp.status_code != 200:
            return []
        data = resp.json() if resp.content else {}
        emails = data.get("emails") if isinstance(data, dict) else []
        return emails if isinstance(emails, list) else []

    def _create_duckmail_session(self):
        session = curl_requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if self.proxies:
            session.proxies = self.proxies
        return session

    def _create_duckmail_email(self):
        if not self._duckmail_bearer:
            raise Exception("DuckMail 凭证缺少 Bearer Token")
        chars = string.ascii_lowercase + string.digits
        length = random.randint(8, 13)
        email_local = "".join(random.choice(chars) for _ in range(length))
        email = f"{email_local}@duckmail.sbs"
        password = secrets.token_urlsafe(18)
        api_base = self._duckmail_api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {self._duckmail_bearer}"}
        session = self._create_duckmail_session()
        res = session.post(
            f"{api_base}/accounts",
            json={"address": email, "password": password},
            headers=headers,
            timeout=15,
            impersonate=self.impersonate,
        )
        if res.status_code not in (200, 201):
            raise Exception(f"创建邮箱失败: {res.status_code} - {res.text[:200]}")
        token_res = session.post(
            f"{api_base}/token",
            json={"address": email, "password": password},
            timeout=15,
            impersonate=self.impersonate,
        )
        if token_res.status_code != 200:
            raise Exception(f"获取邮件 Token 失败: {token_res.status_code}")
        mail_token = token_res.json().get("token")
        if not mail_token:
            raise Exception("获取邮件 Token 失败: token 为空")
        return email, password, mail_token

    def _create_cfmail_email(self):
        config_path = self._cfmail_config_path or _CFMAIL_CONFIG_PATH
        profile_mode = self._cfmail_profile_mode or "auto"
        if config_path == _CFMAIL_CONFIG_PATH and profile_mode == CFMAIL_PROFILE_MODE:
            reload_cfmail_accounts_if_needed()
            available_accounts = CFMAIL_ACCOUNTS
            account = select_cfmail_account(profile_mode)
        else:
            available_accounts = build_cfmail_accounts(load_cfmail_accounts_from_file(config_path))
            if profile_mode != "auto":
                preferred = [item for item in available_accounts if item.name == profile_mode]
                if preferred:
                    available_accounts = preferred
            account = random.choice(available_accounts) if available_accounts else None
        if not account:
            raise Exception(f"没有可用的 cfmail 配置，请检查 {config_path}；当前已加载配置数: {len(available_accounts)}")

        local = f"oc{secrets.token_hex(5)}"
        try:
            resp = curl_requests.post(
                f"https://{account.worker_domain}/admin/new_address",
                headers={"x-admin-auth": account.admin_password, **cfmail_headers(use_json=True)},
                json={"enablePrefix": True, "name": local, "domain": account.email_domain},
                proxies=self.proxies,
                impersonate=self.impersonate,
                timeout=15,
            )
        except Exception as exc:
            record_cfmail_failure(account.name, f"new_address exception: {exc}")
            raise Exception(f"cfmail 请求异常: {exc}")

        if resp.status_code != 200:
            record_cfmail_failure(account.name, f"new_address status={resp.status_code}")
            raise Exception(f"cfmail 创建失败: {resp.status_code} - {resp.text[:200]}")

        data = resp.json()
        email = str(data.get("address") or "").strip()
        jwt = str(data.get("jwt") or "").strip()
        if not email or not jwt:
            record_cfmail_failure(account.name, "new_address incomplete data")
            raise Exception("cfmail 返回数据不完整（address 或 jwt 为空）")

        self._cfmail_api_base = f"https://{account.worker_domain}"
        self._cfmail_account_name = account.name
        self._cfmail_mail_token = jwt
        self._log(f"[cfmail] 创建邮箱成功: {email} (配置: {account.name})")
        return email, "", jwt

    def _fetch_emails_duckmail(self, mail_token: str):
        api_base = self._duckmail_api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {mail_token}"}
        session = self._create_duckmail_session()
        res = session.get(f"{api_base}/messages", headers=headers, timeout=15, impersonate=self.impersonate)
        if res.status_code == 200:
            data = res.json()
            return data.get("hydra:member") or data.get("member") or data.get("data") or []
        return []

    def fetch_cfmail_messages(self, mail_token: str):
        if not self._cfmail_api_base:
            return []
        try:
            resp = curl_requests.get(
                f"{self._cfmail_api_base}/api/mails",
                params={"limit": 10, "offset": 0},
                headers=cfmail_headers(jwt=mail_token, use_json=True),
                proxies=self.proxies,
                impersonate=self.impersonate,
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            data = resp.json() if resp.content else {}
            messages = data.get("results", []) if isinstance(data, dict) else []
            return messages if isinstance(messages, list) else []
        except Exception:
            return []

    def _fetch_email_detail_duckmail(self, mail_token: str, msg_id: str):
        api_base = self._duckmail_api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {mail_token}"}
        session = self._create_duckmail_session()
        if isinstance(msg_id, str) and msg_id.startswith("/messages/"):
            msg_id = msg_id.split("/")[-1]
        res = session.get(f"{api_base}/messages/{msg_id}", headers=headers, timeout=15, impersonate=self.impersonate)
        if res.status_code == 200:
            return res.json()
        return None

    def get_last_cfmail_account_name(self) -> str:
        return self._cfmail_account_name

    def mark_cfmail_success(self) -> None:
        if self._cfmail_account_name:
            record_cfmail_success(self._cfmail_account_name)

    def _extract_code_for_provider(
        self,
        *,
        provider: str,
        mail_token: str,
        email: str,
        seen_ids: set[str],
        extractor: Callable[[str], str | None],
        message_filter: Callable[[str], bool] | None,
    ) -> str | None:
        normalized = str(provider or "").strip().lower()
        if normalized == "gptmail":
            messages = self._fetch_emails_gptmail(mail_token)
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or msg.get("_id") or msg.get("createdAt") or "").strip()
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)
                content = "\n".join([
                    str(msg.get("subject") or ""),
                    str(msg.get("text") or ""),
                    str(msg.get("html") or ""),
                    str(msg.get("from") or ""),
                ])
                if message_filter and not message_filter(content):
                    continue
                code = extractor(content)
                if code:
                    return code
            return None
        if normalized in {"mail_tm", "mail_gw"}:
            api_base = self._mail_tm_api_base if normalized == "mail_tm" else self._mail_gw_api_base
            messages = self._fetch_messages_mailtm_compatible(api_base, mail_token)
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or msg.get("@id") or "").strip()
                if not msg_id or msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                detail = self._fetch_email_detail_mailtm_compatible(api_base, mail_token, msg_id)
                if not detail:
                    continue
                html = detail.get("html") or ""
                if isinstance(html, list):
                    html = "\n".join(str(item) for item in html)
                content = "\n".join([
                    str(detail.get("subject") or ""),
                    str(detail.get("text") or ""),
                    str(html),
                    str(detail.get("intro") or ""),
                    str(detail.get("from", {}).get("address") if isinstance(detail.get("from"), dict) else detail.get("from") or ""),
                ])
                if message_filter and not message_filter(content):
                    continue
                code = extractor(content)
                if code:
                    return code
            return None
        if normalized == "tempmail_lol":
            messages = self._fetch_emails_tempmail_lol(mail_token)
            for msg in sorted(messages, key=lambda item: item.get("date", 0), reverse=True):
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or msg.get("date") or msg.get("createdAt") or "").strip()
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)
                content = "\n".join([
                    str(msg.get("subject") or ""),
                    str(msg.get("body") or ""),
                    str(msg.get("html") or ""),
                    str(msg.get("from") or ""),
                ])
                if message_filter and not message_filter(content):
                    continue
                code = extractor(content)
                if code:
                    return code
            return None
        if normalized == "duckmail":
            messages = self._fetch_emails_duckmail(mail_token)
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or msg.get("@id") or "").strip()
                if not msg_id or msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                detail = self._fetch_email_detail_duckmail(mail_token, msg_id)
                if not detail:
                    continue
                content = "\n".join([
                    str(detail.get("subject") or ""),
                    str(detail.get("text") or ""),
                    str(detail.get("html") or ""),
                    str(detail.get("from") or ""),
                ])
                if message_filter and not message_filter(content):
                    continue
                code = extractor(content)
                if code:
                    return code
            return None
        if normalized == "cfmail":
            messages = self.fetch_cfmail_messages(mail_token)
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or msg.get("createdAt") or "").strip()
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)
                recipient = str(msg.get("address") or "").strip().lower()
                if recipient and email and recipient != email.strip().lower():
                    continue
                raw = str(msg.get("raw") or "")
                metadata = msg.get("metadata") or {}
                content = "\n".join([recipient, raw, json.dumps(metadata, ensure_ascii=False)])
                if message_filter and not message_filter(content):
                    continue
                code = extractor(content)
                if code:
                    return code
            return None
        raise Exception(f"不支持的邮箱 provider: {provider}")
