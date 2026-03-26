"""
ChatGPT 批量自动注册工具 (并发版) - 支持 DuckMail / TempMail.lol / CF 自建邮箱 / GPTMail
依赖: pip install curl_cffi
"""

import os
import re
import uuid
import json
import random
import string
import time
import sys
import shutil
import builtins
import threading
import traceback
import secrets
import hashlib
import base64
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse, parse_qs, urlencode
from typing import Any, Dict, Optional, List

from curl_cffi import requests as curl_requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from shared.email_adapter import SUPPORTED_EMAIL_KINDS as SHARED_EMAIL_KINDS
from shared.email_adapter import UnifiedEmailAdapter
from shared.email_adapter import build_cfmail_accounts
from shared.email_adapter import configure_cfmail_defaults
from shared.email_adapter import load_cfmail_accounts_from_file
from shared.email_adapter import load_email_credentials_from_file as load_shared_email_credentials_from_file

# ================= 加载配置 =================
def _load_config():
    config = {
        "total_accounts": 3,
        "mail_provider": "gptmail",
        "cfmail_config_path": "zhuce5_cfmail_accounts.json",
        "cfmail_profile": "auto",
        "duckmail_api_base": "https://api.duckmail.sbs",
        "duckmail_bearer": "",
        "mail_tm_api_base": "https://api.mail.tm",
        "mail_tm_domain": "",
        "mail_gw_api_base": "https://api.mail.gw",
        "mail_gw_domain": "",
        "tempmail_lol_api_base": "https://api.tempmail.lol/v2",
        "gptmail_api_base": "http://127.0.0.1:18787",
        "gptmail_api_key": "",
        "gptmail_domain": "",
        "proxy": "http://127.0.0.1:7890",
        "output_file": "registered_accounts.txt",
        "enable_oauth": True,
        "oauth_required": True,
        "oauth_issuer": "https://auth.openai.com",
        "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        "ak_file": "ak.txt",
        "rk_file": "rk.txt",
        "token_json_dir": "codex_tokens",
        "upload_api_url": "",
        "upload_api_token": "",
        "cpa_cleanup_enabled": True,
        "cpa_upload_every_n": 3,
    }

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            print(f"⚠️ 加载 config.json 失败: {e}")

    config["duckmail_api_base"] = os.environ.get("DUCKMAIL_API_BASE", config["duckmail_api_base"])
    config["duckmail_bearer"] = os.environ.get("DUCKMAIL_BEARER", config["duckmail_bearer"])
    config["mail_tm_api_base"] = os.environ.get("MAIL_TM_API_BASE", config["mail_tm_api_base"])
    config["mail_tm_domain"] = os.environ.get("MAIL_TM_DOMAIN", config["mail_tm_domain"])
    config["mail_gw_api_base"] = os.environ.get("MAIL_GW_API_BASE", config["mail_gw_api_base"])
    config["mail_gw_domain"] = os.environ.get("MAIL_GW_DOMAIN", config["mail_gw_domain"])
    config["tempmail_lol_api_base"] = os.environ.get("TEMPMAIL_LOL_API_BASE", config["tempmail_lol_api_base"])
    config["gptmail_api_base"] = os.environ.get("GPTMAIL_API_BASE", config.get("gptmail_api_base", ""))
    config["gptmail_api_key"] = os.environ.get("GPTMAIL_API_KEY", config.get("gptmail_api_key", ""))
    config["gptmail_domain"] = os.environ.get("GPTMAIL_DOMAIN", config.get("gptmail_domain", ""))
    config["proxy"] = os.environ.get("PROXY", config["proxy"])
    config["total_accounts"] = int(os.environ.get("TOTAL_ACCOUNTS", config["total_accounts"]))
    config["enable_oauth"] = os.environ.get("ENABLE_OAUTH", config["enable_oauth"])
    config["oauth_required"] = os.environ.get("OAUTH_REQUIRED", config["oauth_required"])
    config["oauth_issuer"] = os.environ.get("OAUTH_ISSUER", config["oauth_issuer"])
    config["oauth_client_id"] = os.environ.get("OAUTH_CLIENT_ID", config["oauth_client_id"])
    config["oauth_redirect_uri"] = os.environ.get("OAUTH_REDIRECT_URI", config["oauth_redirect_uri"])
    config["ak_file"] = os.environ.get("AK_FILE", config["ak_file"])
    config["rk_file"] = os.environ.get("RK_FILE", config["rk_file"])
    config["token_json_dir"] = os.environ.get("TOKEN_JSON_DIR", config["token_json_dir"])
    config["upload_api_url"] = os.environ.get("UPLOAD_API_URL", config["upload_api_url"])
    config["upload_api_token"] = os.environ.get("UPLOAD_API_TOKEN", config["upload_api_token"])
    config["mail_provider"] = os.environ.get("MAIL_PROVIDER", config["mail_provider"])
    config["cfmail_config_path"] = os.environ.get("CFMAIL_CONFIG_PATH", config["cfmail_config_path"])
    config["cfmail_profile"] = os.environ.get("CFMAIL_PROFILE", config["cfmail_profile"])
    config["cpa_upload_every_n"] = int(os.environ.get("CPA_UPLOAD_EVERY_N", config["cpa_upload_every_n"]))

    return config


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


_CONFIG = _load_config()
DUCKMAIL_API_BASE = _CONFIG["duckmail_api_base"]
DUCKMAIL_BEARER = _CONFIG["duckmail_bearer"]
MAIL_TM_API_BASE = _CONFIG.get("mail_tm_api_base", "https://api.mail.tm").rstrip("/")
MAIL_TM_DOMAIN = _CONFIG.get("mail_tm_domain", "")
MAIL_GW_API_BASE = _CONFIG.get("mail_gw_api_base", "https://api.mail.gw").rstrip("/")
MAIL_GW_DOMAIN = _CONFIG.get("mail_gw_domain", "")
TEMPMAIL_LOL_API_BASE = _CONFIG.get("tempmail_lol_api_base", "https://api.tempmail.lol/v2").rstrip("/")
GPTMAIL_API_BASE = _CONFIG.get("gptmail_api_base", "").rstrip("/")
GPTMAIL_API_KEY = _CONFIG.get("gptmail_api_key", "")
GPTMAIL_DOMAIN = _CONFIG.get("gptmail_domain", "")
EMAIL_CREDENTIAL_MANAGER_URL = (os.environ.get("EMAIL_CREDENTIAL_MANAGER_URL", "") or "").strip().rstrip("/")
EMAIL_CREDENTIAL_MANAGER_TOKEN = (os.environ.get("EMAIL_CREDENTIAL_MANAGER_TOKEN", "") or "").strip()
EMAIL_CREDENTIAL_MANAGER_PLATFORM = (os.environ.get("EMAIL_CREDENTIAL_MANAGER_PLATFORM", "openai-register") or "openai-register").strip()
EMAIL_CREDENTIAL_MANAGER_TASK_ID = (os.environ.get("EMAIL_CREDENTIAL_MANAGER_TASK_ID", "") or "").strip()
DEFAULT_TOTAL_ACCOUNTS = _CONFIG["total_accounts"]
DEFAULT_PROXY = _CONFIG["proxy"]
DEFAULT_OUTPUT_FILE = _CONFIG["output_file"]
ENABLE_OAUTH = _as_bool(_CONFIG.get("enable_oauth", True))
OAUTH_REQUIRED = _as_bool(_CONFIG.get("oauth_required", True))
OAUTH_ISSUER = _CONFIG["oauth_issuer"].rstrip("/")
OAUTH_CLIENT_ID = _CONFIG["oauth_client_id"]
OAUTH_REDIRECT_URI = _CONFIG["oauth_redirect_uri"]
AK_FILE = _CONFIG["ak_file"]
RK_FILE = _CONFIG["rk_file"]
TOKEN_JSON_DIR = _CONFIG["token_json_dir"]
UPLOAD_API_URL = _CONFIG["upload_api_url"]
UPLOAD_API_TOKEN = _CONFIG["upload_api_token"]
CPA_CLEANUP_ENABLED = _as_bool(_CONFIG.get("cpa_cleanup_enabled", True))
CPA_UPLOAD_EVERY_N = max(1, int(_CONFIG.get("cpa_upload_every_n", 3) or 3))
MAIL_PROVIDER = str(_CONFIG.get("mail_provider", "duckmail")).strip().lower()

# 全局线程锁
_print_lock = threading.RLock()
_file_lock = threading.Lock()
_original_print = builtins.print

_progress_state = {
    "active": False,
    "done": 0,
    "total": 1,
    "success": 0,
    "fail": 0,
    "start_time": 0.0,
}


def _clear_progress_line_unlocked():
    cols = shutil.get_terminal_size((110, 20)).columns
    _original_print("\r" + " " * max(10, cols - 1) + "\r", end="", flush=True)


def _render_apt_like_progress(done: int, total: int, success: int, fail: int, start_time: float):
    """在终端底部输出类似 apt install 的单行进度条。"""
    total = max(1, int(total or 1))
    done = max(0, min(int(done or 0), total))
    success = max(0, int(success or 0))
    fail = max(0, int(fail or 0))

    with _print_lock:
        _progress_state.update({
            "active": done < total,
            "done": done,
            "total": total,
            "success": success,
            "fail": fail,
            "start_time": float(start_time or time.time()),
        })

        percent = (done / total) * 100
        cols = shutil.get_terminal_size((110, 20)).columns
        elapsed = max(0.0, time.time() - _progress_state["start_time"])
        speed = (done / elapsed) if elapsed > 0 else 0.0

        right_text = f" {percent:6.2f}% [{done}/{total}] 成功:{success} 失败:{fail} 速率:{speed:.2f}/s"
        bar_width = max(12, min(50, cols - len(right_text) - 8))
        filled = int((done / total) * bar_width)

        if done >= total:
            bar = "=" * bar_width
        elif filled <= 0:
            bar = ">" + " " * (bar_width - 1)
        else:
            bar = "=" * (filled - 1) + ">" + " " * (bar_width - filled)

        line = f"\r进度: [{bar}]" + right_text
        _original_print(line, end="", flush=True)


def _print_with_progress(*args, **kwargs):
    with _print_lock:
        if _progress_state["active"]:
            _clear_progress_line_unlocked()

        _original_print(*args, **kwargs)

        if _progress_state["active"]:
            _render_apt_like_progress(
                _progress_state["done"],
                _progress_state["total"],
                _progress_state["success"],
                _progress_state["fail"],
                _progress_state["start_time"],
            )


# 全局接管 print，保证日志输出后能恢复底部进度条
builtins.print = _print_with_progress


_CFMAIL_CONFIG_PATH = str(_CONFIG.get("cfmail_config_path", "zhuce5_cfmail_accounts.json")).strip()
CFMAIL_PROFILE_MODE = str(_CONFIG.get("cfmail_profile", "auto")).strip() or "auto"
configure_cfmail_defaults(_CFMAIL_CONFIG_PATH, CFMAIL_PROFILE_MODE)


def _current_cfmail_accounts() -> list:
    return build_cfmail_accounts(load_cfmail_accounts_from_file(_CFMAIL_CONFIG_PATH, silent=True))


# ================= Chrome 指纹配置 =================

_CHROME_PROFILES = [
    {
        "major": 131, "impersonate": "chrome131",
        "build": 6778, "patch_range": (69, 205),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    },
    {
        "major": 133, "impersonate": "chrome133a",
        "build": 6943, "patch_range": (33, 153),
        "sec_ch_ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    },
    {
        "major": 136, "impersonate": "chrome136",
        "build": 7103, "patch_range": (48, 175),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    },
    {
        "major": 142, "impersonate": "chrome142",
        "build": 7540, "patch_range": (30, 150),
        "sec_ch_ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    },
]


def _random_chrome_version():
    profile = random.choice(_CHROME_PROFILES)
    major = profile["major"]
    build = profile["build"]
    patch = random.randint(*profile["patch_range"])
    full_ver = f"{major}.0.{build}.{patch}"
    ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full_ver} Safari/537.36"
    return profile["impersonate"], major, full_ver, ua, profile["sec_ch_ua"]


def _random_delay(low=0.3, high=1.0):
    time.sleep(random.uniform(low, high))


def _make_trace_headers():
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    tp = f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01"
    return {
        "traceparent": tp, "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum", "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id), "x-datadog-parent-id": str(parent_id),
    }


def _generate_pkce():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ================= Sentinel Token =================

class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id=None, user_agent=None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str):
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        h &= 0xFFFFFFFF
        return format(h, "08x")

    def _get_config(self):
        now_str = time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
            time.gmtime(),
        )
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice([
            "vendorSub", "productSub", "vendor", "maxTouchPoints",
            "scheduling", "userActivation", "doNotTrack", "geolocation",
            "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
            "webkitTemporaryStorage", "webkitPersistentStorage",
            "hardwareConcurrency", "cookieEnabled", "credentials",
            "mediaDevices", "permissions", "locks", "ink",
        ])
        nav_val = f"{nav_prop}-undefined"
        return [
            "1920x1080", now_str, 4294705152, random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None, None, "en-US", "en-US,en", random.random(), nav_val,
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now, self.sid, "", random.choice([4, 8, 12, 16]), time_origin,
        ]

    @staticmethod
    def _base64_encode(data):
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        diff_len = len(difficulty)
        if hash_hex[:diff_len] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, seed=None, difficulty=None):
        seed = seed if seed is not None else self.requirements_seed
        difficulty = str(difficulty or "0")
        start_time = time.time()
        config = self._get_config()
        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start_time, seed, difficulty, config, i)
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self):
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        data = self._base64_encode(config)
        return "gAAAAAC" + data


def fetch_sentinel_challenge(session, device_id, flow="authorize_continue", user_agent=None,
                             sec_ch_ua=None, impersonate=None):
    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    req_body = {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or "Mozilla/5.0",
        "sec-ch-ua": sec_ch_ua or '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    kwargs = {"data": json.dumps(req_body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        resp = session.post("https://sentinel.openai.com/backend-api/sentinel/req", **kwargs)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def build_sentinel_token(session, device_id, flow="authorize_continue", user_agent=None,
                         sec_ch_ua=None, impersonate=None):
    challenge = fetch_sentinel_challenge(session, device_id, flow=flow,
                                         user_agent=user_agent, sec_ch_ua=sec_ch_ua,
                                         impersonate=impersonate)
    if not challenge:
        return None
    c_value = challenge.get("token", "")
    if not c_value:
        return None
    pow_data = challenge.get("proofofwork") or {}
    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(seed=pow_data.get("seed"),
                                            difficulty=pow_data.get("difficulty", "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps({"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": flow},
                      separators=(",", ":"))


def _extract_code_from_url(url: str):
    if not url or "code=" not in url:
        return None
    try:
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


def _decode_jwt_payload(token: str):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _save_codex_tokens(email: str, tokens: dict):
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    id_token = tokens.get("id_token", "")

    if access_token:
        with _file_lock:
            with open(AK_FILE, "a", encoding="utf-8") as f:
                f.write(f"{access_token}\n")

    if refresh_token:
        with _file_lock:
            with open(RK_FILE, "a", encoding="utf-8") as f:
                f.write(f"{refresh_token}\n")

    if not access_token:
        return

    payload = _decode_jwt_payload(access_token)
    auth_info = payload.get("https://api.openai.com/auth", {})
    account_id = auth_info.get("chatgpt_account_id", "")
    exp_timestamp = payload.get("exp")
    expired_str = ""
    if isinstance(exp_timestamp, int) and exp_timestamp > 0:
        from datetime import datetime, timezone, timedelta
        exp_dt = datetime.fromtimestamp(exp_timestamp, tz=timezone(timedelta(hours=8)))
        expired_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    token_data = {
        "type": "codex", "email": email, "expired": expired_str,
        "id_token": id_token, "account_id": account_id,
        "access_token": access_token,
        "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "refresh_token": refresh_token,
    }

    base_dir = os.path.dirname(os.path.abspath(__file__))
    token_dir = TOKEN_JSON_DIR if os.path.isabs(TOKEN_JSON_DIR) else os.path.join(base_dir, TOKEN_JSON_DIR)
    os.makedirs(token_dir, exist_ok=True)
    token_path = os.path.join(token_dir, f"{email}.json")
    with _file_lock:
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False)


def _remove_matching_lines(path: str, predicate):
    if not path or not os.path.exists(path):
        return
    with _file_lock:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except Exception:
            return
        kept = [line for line in lines if not predicate(line.rstrip("\n"))]
        if kept == lines:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(kept)


def _cleanup_uploaded_account_artifacts(filepath: str, output_file: str | None = None):
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            token_data = json.load(fh)
    except Exception:
        token_data = {}

    email = str(token_data.get("email") or "").strip()
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()

    if output_file and email:
        _remove_matching_lines(output_file, lambda line: line.startswith(f"{email}----"))
    if access_token:
        _remove_matching_lines(AK_FILE, lambda line: line.strip() == access_token)
    if refresh_token:
        _remove_matching_lines(RK_FILE, lambda line: line.strip() == refresh_token)

    try:
        os.remove(filepath)
    except Exception:
        pass
    if email:
        print(f"  [CPA] 已清理本地实体: {email}")


def _upload_token_json(filepath):
    mp = None
    try:
        from curl_cffi import CurlMime
        filename = os.path.basename(filepath)
        mp = CurlMime()
        mp.addpart(name="file", content_type="application/json",
                   filename=filename, local_path=filepath)
        session = curl_requests.Session()
        if DEFAULT_PROXY:
            session.proxies = {"http": DEFAULT_PROXY, "https": DEFAULT_PROXY}
        resp = session.post(UPLOAD_API_URL, multipart=mp,
                            headers={"Authorization": f"Bearer {UPLOAD_API_TOKEN}"},
                            verify=False, timeout=30)
        if resp.status_code == 200:
            print(f"  [CPA] ✅ {filename} 已上传到 CPA 管理平台")
            return True
        else:
            print(f"  [CPA] ❌ {filename} 上传失败: {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  [CPA] ❌ {os.path.basename(filepath)} 上传异常: {e}")
        return False
    finally:
        if mp:
            mp.close()


def _upload_all_tokens_to_cpa(output_file: str | None = None):
    if not UPLOAD_API_URL:
        print("\n[CPA] ⚠️ 未配置 upload_api_url，跳过 CPA 上传")
        return
    base_dir = os.path.dirname(os.path.abspath(__file__))
    token_dir = TOKEN_JSON_DIR if os.path.isabs(TOKEN_JSON_DIR) else os.path.join(base_dir, TOKEN_JSON_DIR)
    if not os.path.isdir(token_dir):
        return
    json_files = [f for f in os.listdir(token_dir) if f.endswith(".json")]
    if not json_files:
        return
    print(f"\n{'='*60}\n  [CPA] 开始上传 {len(json_files)} 个账号到 CPA 管理平台\n{'='*60}")
    uploaded = 0
    failed = 0
    for filename in json_files:
        filepath = os.path.join(token_dir, filename)
        if _upload_token_json(filepath):
            _cleanup_uploaded_account_artifacts(filepath, output_file=output_file)
            uploaded += 1
        else:
            failed += 1
    print(f"\n  [CPA] 上传完成: 成功 {uploaded} 个, 失败 {failed} 个\n{'='*60}")


# ================= CPA Codex 清理引擎 =================

from datetime import datetime as _cpa_datetime
from urllib.parse import urlparse as _cpa_urlparse, urlunparse as _cpa_urlunparse

_CPA_STATUS_KEYWORDS = {"token_invalidated", "token_revoked", "usage_limit_reached"}
_CPA_MESSAGE_KEYWORDS = [
    "额度获取失败：401", '"status":401', '"status": 401',
    "your authentication token has been invalidated.",
    "encountered invalidated oauth token for user",
    "token_invalidated", "token_revoked", "usage_limit_reached",
]
_CPA_PROBE_TARGET_URL = "https://chatgpt.com/backend-api/codex/responses/compact"
_CPA_PROBE_MODEL = "gpt-5.1-codex"


def _cpa_normalize_api_root(raw_url):
    value = (raw_url or "").strip()
    if not value:
        return ""
    parsed = _cpa_urlparse(value)
    path = parsed.path or ""
    if path.endswith("/management.html"):
        path = path[:-len("/management.html")] + "/v0/management"
    for suffix in ("/api-call", "/auth-files"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    normalized = _cpa_urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "", ""))
    return normalized.rstrip("/")


class _CpaCleanupConfig:
    def __init__(self, management_url, management_token, management_timeout=15,
                 active_probe=True, probe_timeout=8, probe_workers=12,
                 delete_workers=8, max_active_probes=120):
        self.management_url = management_url
        self.management_token = management_token
        self.management_timeout = management_timeout
        self.active_probe = active_probe
        self.probe_timeout = probe_timeout
        self.probe_workers = probe_workers
        self.delete_workers = delete_workers
        self.max_active_probes = max_active_probes

    @classmethod
    def from_mapping(cls, data):
        def to_int(name, default, minimum):
            try:
                parsed = int(data.get(name, default))
            except Exception:
                parsed = default
            return max(minimum, parsed)

        def to_bool(name, default):
            raw = data.get(name, default)
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            return default

        return cls(
            management_url=_cpa_normalize_api_root(str(data.get("management_url", "") or "")),
            management_token=str(data.get("management_token", "") or "").strip(),
            management_timeout=to_int("management_timeout", 15, 1),
            active_probe=to_bool("active_probe", True),
            probe_timeout=to_int("probe_timeout", 8, 1),
            probe_workers=to_int("probe_workers", 12, 1),
            delete_workers=to_int("delete_workers", 8, 1),
            max_active_probes=to_int("max_active_probes", 120, 0),
        )

    def validate(self):
        if not self.management_url:
            return False, "management_url 不能为空"
        if not self.management_token:
            return False, "management_token 不能为空"
        if not self.management_url.startswith(("http://", "https://")):
            return False, "management_url 必须以 http:// 或 https:// 开头"
        return True, ""


class _CpaManagementGateway:
    def __init__(self, config):
        self.config = config

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.config.management_token}"}

    @property
    def auth_files_endpoint(self):
        return self.config.management_url.rstrip("/") + "/auth-files"

    @property
    def api_call_endpoint(self):
        return self.config.management_url.rstrip("/") + "/api-call"

    def list_auth_files(self):
        resp = curl_requests.get(self.auth_files_endpoint, headers=self._headers,
                                 timeout=self.config.management_timeout)
        if resp.status_code == 404:
            raise RuntimeError(f"auth-files 接口不存在: {self.auth_files_endpoint}")
        resp.raise_for_status()
        payload = resp.json()
        files = payload.get("files", []) if isinstance(payload, dict) else []
        return files if isinstance(files, list) else []

    def delete_auth_file(self, name):
        resp = curl_requests.delete(
            self.auth_files_endpoint, params={"name": name},
            headers=self._headers, timeout=self.config.management_timeout,
        )
        if 200 <= resp.status_code < 300:
            return True, ""
        detail = ""
        try:
            detail = json.dumps(resp.json(), ensure_ascii=False)
        except Exception:
            detail = resp.text
        return False, f"HTTP {resp.status_code}: {detail}"

    def probe_auth_index(self, auth_index):
        payload = {
            "auth_index": auth_index, "method": "POST",
            "url": _CPA_PROBE_TARGET_URL,
            "header": {
                "Authorization": "Bearer $TOKEN$",
                "Content-Type": "application/json",
                "User-Agent": "codex_cli_rs/0.101.0",
            },
            "data": json.dumps(
                {"model": _CPA_PROBE_MODEL, "input": [{"role": "user", "content": "ping"}]},
                ensure_ascii=False,
            ),
        }
        resp = curl_requests.post(self.api_call_endpoint, headers=self._headers,
                                  json=payload, timeout=self.config.probe_timeout)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            return 0, ""
        return int(body.get("status_code", 0) or 0), str(body.get("body", "") or "")


def _cpa_safe_status_message(file_obj):
    return str(file_obj.get("status_message", "") or "")


def _cpa_reason_from_status(file_obj):
    status_message = _cpa_safe_status_message(file_obj)
    if not status_message:
        return ""
    lower_msg = status_message.lower()
    for keyword in _CPA_MESSAGE_KEYWORDS:
        if keyword in lower_msg:
            return keyword
    try:
        parsed = json.loads(status_message)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        if int(parsed.get("status", 0) or 0) == 401:
            return "status_401"
        err = parsed.get("error", {})
        if isinstance(err, dict):
            code = str(err.get("code", "") or "")
            if code in _CPA_STATUS_KEYWORDS:
                return code
    return ""


def _cpa_looks_401(file_obj):
    try:
        if int(file_obj.get("status", 0) or 0) == 401:
            return True
    except Exception:
        pass
    text = _cpa_safe_status_message(file_obj).lower()
    return "401" in text or "unauthorized" in text


class _CpaCleanupOrchestrator:
    def __init__(self, config, log=None):
        self.config = config
        self.gateway = _CpaManagementGateway(config)
        self.log = log or (lambda msg: None)

    def _log(self, message):
        self.log(message)

    def _probe_one(self, file_obj):
        name = str(file_obj.get("name", "") or "")
        auth_index = str(file_obj.get("auth_index", "") or "").strip()
        if not auth_index:
            return name, ""
        try:
            status_code, body = self.gateway.probe_auth_index(auth_index)
        except Exception as exc:
            return name, f"probe_error:{exc}"
        body_lower = body.lower()
        if status_code == 401:
            return name, "probe_status_401"
        if "401" in body_lower or "unauthorized" in body_lower:
            return name, "probe_body_401"
        for keyword in _CPA_MESSAGE_KEYWORDS:
            if keyword in body_lower:
                return name, f"probe_{keyword}"
        return name, ""

    def _delete_batch(self, hits):
        deleted = 0
        failures = []
        total = len(hits)

        def task(item):
            ok, err = self.gateway.delete_auth_file(item["name"])
            return item["name"], ok, err

        with ThreadPoolExecutor(max_workers=self.config.delete_workers) as pool:
            future_map = {pool.submit(task, item): item for item in hits}
            done = 0
            for future in as_completed(future_map):
                done += 1
                name, ok, err = future.result()
                if ok:
                    deleted += 1
                    self._log(f"[CPA清理] 删除成功: {name} ({done}/{total})")
                else:
                    failures.append({"name": name, "error": err})
                    self._log(f"[CPA清理] 删除失败: {name} -> {err}")
        return deleted, failures

    def _cleanup_401_only(self, exclude_names):
        try:
            files = self.gateway.list_auth_files()
        except Exception as exc:
            self._log(f"[CPA清理] 401补删列表读取失败: {exc}")
            return 0, [{"name": "<list>", "error": str(exc)}]
        targets = []
        for file_obj in files:
            name = str(file_obj.get("name", "") or "")
            if not name or name in exclude_names:
                continue
            if _cpa_looks_401(file_obj):
                targets.append({"name": name, "keyword": "status_401",
                                "status_message": _cpa_safe_status_message(file_obj)})
        if not targets:
            return 0, []
        deleted, failures = self._delete_batch(targets)
        return deleted, failures

    def run(self):
        self._log("[CPA清理] 开始清理")
        files = self.gateway.list_auth_files()
        self._log(f"[CPA清理] 拉取 auth-files 成功，总数: {len(files)}")

        fixed_hits = []
        probe_candidates = []

        for file_obj in files:
            reason = _cpa_reason_from_status(file_obj)
            name = str(file_obj.get("name", "") or "")
            if not name:
                continue
            if reason:
                fixed_hits.append({"name": name, "keyword": reason,
                                   "status_message": _cpa_safe_status_message(file_obj)})
                continue
            provider = str(file_obj.get("provider", "") or "").strip().lower()
            auth_index = str(file_obj.get("auth_index", "") or "").strip()
            if self.config.active_probe and provider == "codex" and auth_index:
                probe_candidates.append(file_obj)

        if (self.config.active_probe and self.config.max_active_probes > 0
                and len(probe_candidates) > self.config.max_active_probes):
            probe_candidates = probe_candidates[:self.config.max_active_probes]

        probed_hits = []
        if self.config.active_probe and self.config.max_active_probes != 0 and probe_candidates:
            self._log(f"[CPA清理] 开始主动探测，候选 {len(probe_candidates)} 个")
            with ThreadPoolExecutor(max_workers=self.config.probe_workers) as pool:
                future_map = {pool.submit(self._probe_one, item): item for item in probe_candidates}
                done = 0
                total = len(probe_candidates)
                for future in as_completed(future_map):
                    done += 1
                    name, reason = future.result()
                    if reason and not reason.startswith("probe_error"):
                        status_message = _cpa_safe_status_message(future_map[future])
                        probed_hits.append({"name": name, "keyword": reason,
                                            "status_message": status_message})

        merged_by_name = {}
        for item in fixed_hits + probed_hits:
            if item["name"] not in merged_by_name:
                merged_by_name[item["name"]] = item

        matched = list(merged_by_name.values())
        deleted_main = 0
        failures = []
        if matched:
            deleted_main, failures = self._delete_batch(matched)

        deleted_401, failures_401 = self._cleanup_401_only(set(merged_by_name.keys()))
        failures.extend(failures_401)

        result = {
            "scanned_total": len(files), "matched_total": len(matched),
            "deleted_main": deleted_main, "deleted_401": deleted_401,
            "deleted_total": deleted_main + deleted_401, "failures": failures,
        }
        self._log(f"[CPA清理] 完成: scanned={result['scanned_total']}, "
                  f"deleted_total={result['deleted_total']}")
        return result


def _cpa_execute_cleanup(payload, log=None):
    config = _CpaCleanupConfig.from_mapping(payload)
    ok, msg = config.validate()
    if not ok:
        raise ValueError(msg)
    orchestrator = _CpaCleanupOrchestrator(config=config, log=log)
    return orchestrator.run()


def _run_cpa_cleanup_before_register():
    print(f"\n{'='*60}\n  [CPA清理] 注册前清理 CPA 无效号...\n{'='*60}")
    try:
        payload = {
            "management_url": UPLOAD_API_URL,
            "management_token": UPLOAD_API_TOKEN,
            "active_probe": True, "probe_workers": 12,
            "delete_workers": 8, "max_active_probes": 120,
        }
        result = _cpa_execute_cleanup(payload, log=lambda msg: print(f"  {msg}"))
        print(f"\n  [CPA清理] 清理完成: 扫描 {result['scanned_total']} 个, "
              f"删除 {result['deleted_total']} 个")
    except Exception as e:
        print(f"  [CPA清理] ⚠️ 清理失败 (不影响注册): {e}")
    print(f"{'='*60}\n")


def _generate_password(length=14):
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%&*"
    pwd = [random.choice(lower), random.choice(upper),
           random.choice(digits), random.choice(special)]
    all_chars = lower + upper + digits + special
    pwd += [random.choice(all_chars) for _ in range(length - 4)]
    random.shuffle(pwd)
    return "".join(pwd)


def _random_name():
    first = random.choice([
        "James", "Emma", "Liam", "Olivia", "Noah", "Ava", "Ethan", "Sophia",
        "Lucas", "Mia", "Mason", "Isabella", "Logan", "Charlotte", "Alexander",
        "Amelia", "Benjamin", "Harper", "William", "Evelyn", "Henry", "Abigail",
        "Sebastian", "Emily", "Jack", "Elizabeth",
    ])
    last = random.choice([
        "Smith", "Johnson", "Brown", "Davis", "Wilson", "Moore", "Taylor",
        "Clark", "Hall", "Young", "Anderson", "Thomas", "Jackson", "White",
        "Harris", "Martin", "Thompson", "Garcia", "Robinson", "Lewis",
        "Walker", "Allen", "King", "Wright", "Scott", "Green",
    ])
    return f"{first} {last}"


def _random_birthdate():
    y = random.randint(1985, 2002)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"


def _quick_preflight(proxy: str = None, provider: str = "duckmail") -> bool:
    """启动前连通性检查，避免跑到中途才发现被拦截。"""
    print("\n[Preflight] 开始连通性检查...")
    sess = curl_requests.Session(impersonate="chrome131")
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}

    checks = []

    def _record(name: str, ok: bool, detail: str):
        checks.append((name, ok, detail))
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}: {detail}")

    # 1) chatgpt 首页
    try:
        r = sess.get("https://chatgpt.com/", timeout=20, allow_redirects=True)
        ok = r.status_code != 403
        _record("chatgpt.com", ok, f"status={r.status_code}")
    except Exception as e:
        _record("chatgpt.com", False, f"异常: {e}")

    # 2) CSRF 接口（必须是 JSON 且有 csrfToken）
    try:
        r = sess.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"Accept": "application/json", "Referer": "https://chatgpt.com/"},
            timeout=20,
        )
        data = r.json()
        token = data.get("csrfToken", "") if isinstance(data, dict) else ""
        ok = bool(token)
        _record("chatgpt csrf", ok, f"status={r.status_code}, token={'yes' if token else 'no'}")
    except Exception as e:
        _record("chatgpt csrf", False, f"非 JSON 或异常: {e}")

    # 3) auth.openai.com 可达性
    try:
        r = sess.get("https://auth.openai.com/", timeout=20, allow_redirects=True)
        ok = r.status_code < 500
        _record("auth.openai.com", ok, f"status={r.status_code}")
    except Exception as e:
        _record("auth.openai.com", False, f"异常: {e}")

    # 4) Mail.tm / Mail.gw 可达性（仅对应 provider 时检查）
    if provider in {"mail_tm", "mail_gw"}:
        api_base = MAIL_TM_API_BASE if provider == "mail_tm" else MAIL_GW_API_BASE
        try:
            r = sess.get(f"{api_base}/domains", timeout=20, allow_redirects=True)
            ok = r.status_code == 200
            _record(f"{provider} api", ok, f"status={r.status_code}")
        except Exception as e:
            _record(f"{provider} api", False, f"异常: {e}")

    # 5) TempMail.lol 可达性（仅 tempmail_lol 时检查）
    if provider == "tempmail_lol":
        try:
            r = sess.get(f"{TEMPMAIL_LOL_API_BASE}/", timeout=20, allow_redirects=True)
            ok = r.status_code < 500
            _record("tempmail.lol api", ok, f"status={r.status_code}")
        except Exception as e:
            _record("tempmail.lol api", False, f"异常: {e}")

    # 6) GPTMail 可达性（仅 gptmail 时检查）
    if provider == "gptmail" and GPTMAIL_API_BASE:
        try:
            r = sess.get(f"{GPTMAIL_API_BASE}/api/domains", headers={"X-API-Key": GPTMAIL_API_KEY}, timeout=20)
            ok = r.status_code == 200
            _record("gptmail api", ok, f"status={r.status_code}")
        except Exception as e:
            _record("gptmail api", False, f"异常: {e}")

    all_ok = all(ok for _, ok, _ in checks)
    if all_ok:
        print("[Preflight] 通过，开始注册。")
    else:
        print("[Preflight] 未通过，建议先更换代理或降低并发后再试。")
    return all_ok


# ================= ChatGPTRegister 主类 =================

class ChatGPTRegister:
    BASE = "https://chatgpt.com"
    AUTH = "https://auth.openai.com"

    def __init__(self, proxy: str = None, tag: str = ""):
        self.tag = tag
        self.device_id = str(uuid.uuid4())
        self.auth_session_logging_id = str(uuid.uuid4())
        self.impersonate, self.chrome_major, self.chrome_full, self.ua, self.sec_ch_ua = _random_chrome_version()

        self.session = curl_requests.Session(impersonate=self.impersonate)
        self.proxy = proxy
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept-Language": random.choice([
                "en-US,en;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8",
                "en,en-US;q=0.9", "en-US,en;q=0.8",
            ]),
            "sec-ch-ua": self.sec_ch_ua, "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"', "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": f'"{self.chrome_full}"',
            "sec-ch-ua-platform-version": f'"{random.randint(10, 15)}.0.0"',
        })
        self.session.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
        self._callback_url = None

        # cfmail 状态（仅在使用 cfmail 时填充）
        self._cfmail_api_base = ""
        self._cfmail_account_name = ""
        self._cfmail_mail_token = ""
        self._gptmail_api_base = GPTMAIL_API_BASE.rstrip("/") if GPTMAIL_API_BASE else ""
        self._gptmail_api_key = GPTMAIL_API_KEY
        self._gptmail_domain = GPTMAIL_DOMAIN
        self._duckmail_api_base = DUCKMAIL_API_BASE.rstrip("/") if DUCKMAIL_API_BASE else ""
        self._duckmail_bearer = DUCKMAIL_BEARER
        self._mail_tm_api_base = MAIL_TM_API_BASE.rstrip("/") if MAIL_TM_API_BASE else ""
        self._mail_tm_domain = MAIL_TM_DOMAIN
        self._mail_gw_api_base = MAIL_GW_API_BASE.rstrip("/") if MAIL_GW_API_BASE else ""
        self._mail_gw_domain = MAIL_GW_DOMAIN
        self._tempmail_lol_api_base = TEMPMAIL_LOL_API_BASE.rstrip("/") if TEMPMAIL_LOL_API_BASE else ""
        self._selected_email_credential: Optional[Dict[str, Any]] = None
        self._otp_received = False
        self._oauth_otp_received = False
        self._shared_email_adapter = UnifiedEmailAdapter(
            proxy=self.proxy or "",
            impersonate=self.impersonate,
            logger=self._print,
            defaults={
                "duckmail_api_base": self._duckmail_api_base,
                "duckmail_bearer": self._duckmail_bearer,
                "mail_tm_api_base": self._mail_tm_api_base,
                "mail_tm_domain": self._mail_tm_domain,
                "mail_gw_api_base": self._mail_gw_api_base,
                "mail_gw_domain": self._mail_gw_domain,
                "tempmail_lol_api_base": self._tempmail_lol_api_base,
                "gptmail_api_base": self._gptmail_api_base,
                "gptmail_api_key": self._gptmail_api_key,
                "gptmail_domain": self._gptmail_domain,
                "cfmail_config_path": _CFMAIL_CONFIG_PATH,
                "cfmail_profile": CFMAIL_PROFILE_MODE,
            },
        )

    def apply_email_credential(self, credential: Optional[Dict[str, Any]]) -> None:
        self._selected_email_credential = dict(credential or {})
        if not credential:
            return
        kind = str(credential.get("kind") or "").strip().lower()
        if kind in SHARED_EMAIL_KINDS:
            self._shared_email_adapter.apply_email_credential(credential)

    def _log(self, step, method, url, status, body=None):
        prefix = f"[{self.tag}] " if self.tag else ""
        lines = [f"\n{'='*60}", f"{prefix}[Step] {step}",
                 f"{prefix}[{method}] {url}", f"{prefix}[Status] {status}"]
        if body:
            try:
                lines.append(f"{prefix}[Response] {json.dumps(body, indent=2, ensure_ascii=False)[:1000]}")
            except Exception:
                lines.append(f"{prefix}[Response] {str(body)[:1000]}")
        lines.append(f"{'='*60}")
        with _print_lock:
            print("\n".join(lines))

    def _print(self, msg):
        prefix = f"[{self.tag}] " if self.tag else ""
        with _print_lock:
            print(f"{prefix}{msg}")

    def _extract_verification_code(self, email_content: str):
        if not email_content:
            return None
        patterns = [
            r"Verification code:?\s*(\d{6})",
            r"code is\s*(\d{6})",
            r"代码为[:：]?\s*(\d{6})",
            r"验证码[:：]?\s*(\d{6})",
            r">\s*(\d{6})\s*<",
            r"(?<![#&])\b(\d{6})\b",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, email_content, re.IGNORECASE)
            for code in matches:
                if code == "177010":
                    continue
                return code
        return None

    def create_email_mailbox(self, provider: str):
        provider = str(provider or "").strip().lower()
        if provider in SHARED_EMAIL_KINDS:
            result = self._shared_email_adapter.create_email_mailbox(provider)
            if provider == "cfmail":
                self._cfmail_account_name = self._shared_email_adapter.get_last_cfmail_account_name()
            return result
        raise Exception(f"不支持的邮箱 provider: {provider}")
    # ==================== 统一等待验证码接口 ====================

    def wait_for_verification_email(self, mail_token: str, timeout: int = 120,
                                     email: str = "", provider: str = ""):
        """等待并提取 OpenAI 验证码，自动根据 provider 选择轮询方式"""
        effective_provider = provider or MAIL_PROVIDER
        if effective_provider in SHARED_EMAIL_KINDS:
            self._print(f"[OTP] 等待验证码邮件 (最多 {timeout}s, provider={effective_provider})...")
            code = self._shared_email_adapter.wait_for_code(
                provider=effective_provider,
                mail_token=mail_token,
                email=email,
                timeout=timeout,
                extractor=self._extract_verification_code,
                message_filter=lambda content: ("openai" in content.lower() or "chatgpt" in content.lower()),
            )
            if code:
                self._print(f"[OTP] 验证码: {code}")
                self._otp_received = True
                return code
            self._print(f"[OTP] 超时 ({timeout}s)")
            return None
        raise Exception(f"不支持的邮箱 provider: {effective_provider}")

    # ==================== 注册流程 ====================

    def visit_homepage(self):
        url = f"{self.BASE}/"
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        self._log("0. Visit homepage", "GET", url, r.status_code,
                   {"cookies_count": len(self.session.cookies)})

    def get_csrf(self) -> str:
        url = f"{self.BASE}/api/auth/csrf"
        headers = {"Accept": "application/json", "Referer": f"{self.BASE}/"}

        for attempt in range(2):
            r = self.session.get(url, headers=headers)
            try:
                data = r.json()
            except Exception:
                body = (r.text or "")[:220].replace("\n", " ")
                ctype = r.headers.get("content-type", "")
                if attempt == 0:
                    self._print(f"[CSRF] 非 JSON 响应，准备重试一次: status={r.status_code}, content-type={ctype}")
                    _random_delay(0.4, 1.0)
                    self.visit_homepage()
                    continue
                raise Exception(
                    f"获取 CSRF 失败: 非 JSON 响应 (status={r.status_code}, content-type={ctype}, body={body})"
                )

            token = data.get("csrfToken", "") if isinstance(data, dict) else ""
            self._log("1. Get CSRF", "GET", url, r.status_code, data)
            if token:
                return token

            if attempt == 0:
                self._print("[CSRF] 响应中缺少 csrfToken，准备重试一次")
                _random_delay(0.4, 1.0)
                self.visit_homepage()
                continue
            raise Exception(f"获取 CSRF 失败: 响应缺少 csrfToken, data={data}")

        raise Exception("获取 CSRF 失败: 未知错误")

    def signin(self, email: str, csrf: str) -> str:
        url = f"{self.BASE}/api/auth/signin/openai"
        params = {
            "prompt": "login", "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.auth_session_logging_id,
            "screen_hint": "login_or_signup", "login_hint": email,
        }
        form_data = {"callbackUrl": f"{self.BASE}/", "csrfToken": csrf, "json": "true"}
        r = self.session.post(url, params=params, data=form_data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "Referer": f"{self.BASE}/", "Origin": self.BASE,
        })
        try:
            data = r.json()
        except Exception:
            body = (r.text or "")[:220].replace("\n", " ")
            ctype = r.headers.get("content-type", "")
            raise Exception(
                f"Signin 返回非 JSON (status={r.status_code}, content-type={ctype}, body={body})"
            )
        authorize_url = data.get("url", "") if isinstance(data, dict) else ""
        self._log("2. Signin", "POST", url, r.status_code, data)
        if not authorize_url:
            raise Exception(f"Failed to get authorize URL: {data}")
        return authorize_url

    def authorize(self, url: str) -> str:
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        final_url = str(r.url)
        self._log("3. Authorize", "GET", url, r.status_code, {"final_url": final_url})
        return final_url

    def register(self, email: str, password: str):
        url = f"{self.AUTH}/api/accounts/user/register"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"{self.AUTH}/create-account/password", "Origin": self.AUTH}
        headers.update(_make_trace_headers())
        r = self.session.post(url, json={"username": email, "password": password}, headers=headers)
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:500]}
        self._log("4. Register", "POST", url, r.status_code, data)
        return r.status_code, data

    def send_otp(self):
        url = f"{self.AUTH}/api/accounts/email-otp/send"
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{self.AUTH}/create-account/password", "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        try:
            data = r.json()
        except Exception:
            data = {"final_url": str(r.url), "status": r.status_code}
        self._log("5. Send OTP", "GET", url, r.status_code, data)
        return r.status_code, data

    def validate_otp(self, code: str):
        url = f"{self.AUTH}/api/accounts/email-otp/validate"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"{self.AUTH}/email-verification", "Origin": self.AUTH}
        headers.update(_make_trace_headers())
        r = self.session.post(url, json={"code": code}, headers=headers)
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:500]}
        self._log("6. Validate OTP", "POST", url, r.status_code, data)
        return r.status_code, data

    def create_account(self, name: str, birthdate: str):
        url = f"{self.AUTH}/api/accounts/create_account"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"{self.AUTH}/about-you", "Origin": self.AUTH}
        headers.update(_make_trace_headers())
        r = self.session.post(url, json={"name": name, "birthdate": birthdate}, headers=headers)
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:500]}
        self._log("7. Create Account", "POST", url, r.status_code, data)
        if isinstance(data, dict):
            cb = data.get("continue_url") or data.get("url") or data.get("redirect_url")
            if cb:
                self._callback_url = cb
        return r.status_code, data

    def callback(self, url: str = None):
        if not url:
            url = self._callback_url
        if not url:
            self._print("[!] No callback URL, skipping.")
            return None, None
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        self._log("8. Callback", "GET", url, r.status_code, {"final_url": str(r.url)})
        return r.status_code, {"final_url": str(r.url)}

    # ==================== 自动注册主流程 ====================

    def run_register(self, email, password, name, birthdate, mail_token, provider="duckmail"):
        """注册流程，provider 决定验证码收取方式"""
        self.visit_homepage()
        _random_delay(0.3, 0.8)
        csrf = self.get_csrf()
        _random_delay(0.2, 0.5)
        auth_url = self.signin(email, csrf)
        _random_delay(0.3, 0.8)
        final_url = self.authorize(auth_url)
        final_path = urlparse(final_url).path
        _random_delay(0.3, 0.8)
        self._print(f"Authorize → {final_path}")

        need_otp = False

        if "create-account/password" in final_path:
            self._print("全新注册流程")
            _random_delay(0.5, 1.0)
            status, data = self.register(email, password)
            if status != 200:
                raise Exception(f"Register 失败 ({status}): {data}")
            _random_delay(0.3, 0.8)
            self.send_otp()
            need_otp = True
        elif "email-verification" in final_path or "email-otp" in final_path:
            self._print("跳到 OTP 验证阶段")
            need_otp = True
        elif "about-you" in final_path:
            self._print("跳到填写信息阶段")
            _random_delay(0.5, 1.0)
            self.create_account(name, birthdate)
            _random_delay(0.3, 0.5)
            self.callback()
            return True
        elif "callback" in final_path or "chatgpt.com" in final_url:
            self._print("账号已完成注册")
            return True
        else:
            self._print(f"未知跳转: {final_url}")
            self.register(email, password)
            self.send_otp()
            need_otp = True

        if need_otp:
            otp_code = self.wait_for_verification_email(
                mail_token, timeout=120, email=email, provider=provider
            )
            if not otp_code:
                raise Exception("未能获取验证码")

            _random_delay(0.3, 0.8)
            status, data = self.validate_otp(otp_code)
            if status != 200:
                self._print("验证码失败，重试...")
                self.send_otp()
                _random_delay(1.0, 2.0)
                otp_code = self.wait_for_verification_email(
                    mail_token, timeout=60, email=email, provider=provider
                )
                if not otp_code:
                    raise Exception("重试后仍未获取验证码")
                _random_delay(0.3, 0.8)
                status, data = self.validate_otp(otp_code)
                if status != 200:
                    raise Exception(f"验证码失败 ({status}): {data}")

            # cfmail 成功标记
            if provider == "cfmail" and self._cfmail_account_name:
                self._shared_email_adapter.mark_cfmail_success()

        _random_delay(0.5, 1.5)
        status, data = self.create_account(name, birthdate)
        if status != 200:
            raise Exception(f"Create account 失败 ({status}): {data}")
        _random_delay(0.2, 0.5)
        self.callback()
        return True

    def _decode_oauth_session_cookie(self):
        jar = getattr(self.session.cookies, "jar", None)
        if jar is not None:
            cookie_items = list(jar)
        else:
            cookie_items = []

        for c in cookie_items:
            name = getattr(c, "name", "") or ""
            if "oai-client-auth-session" not in name:
                continue
            raw_val = (getattr(c, "value", "") or "").strip()
            if not raw_val:
                continue
            candidates = [raw_val]
            try:
                from urllib.parse import unquote
                decoded = unquote(raw_val)
                if decoded != raw_val:
                    candidates.append(decoded)
            except Exception:
                pass
            for val in candidates:
                try:
                    if (val.startswith('"') and val.endswith('"')) or \
                       (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    part = val.split(".")[0] if "." in val else val
                    pad = 4 - len(part) % 4
                    if pad != 4:
                        part += "=" * pad
                    raw = base64.urlsafe_b64decode(part)
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    continue
        return None

    def _oauth_allow_redirect_extract_code(self, url: str, referer: str = None):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1", "User-Agent": self.ua,
        }
        if referer:
            headers["Referer"] = referer
        try:
            resp = self.session.get(url, headers=headers, allow_redirects=True,
                                    timeout=30, impersonate=self.impersonate)
            final_url = str(resp.url)
            code = _extract_code_from_url(final_url)
            if code:
                return code
            for r in getattr(resp, "history", []) or []:
                loc = r.headers.get("Location", "")
                code = _extract_code_from_url(loc)
                if code:
                    return code
                code = _extract_code_from_url(str(r.url))
                if code:
                    return code
        except Exception as e:
            maybe_localhost = re.search(r'(https?://localhost[^\s\'\"]+)', str(e))
            if maybe_localhost:
                code = _extract_code_from_url(maybe_localhost.group(1))
                if code:
                    return code
        return None

    def _oauth_follow_for_code(self, start_url: str, referer: str = None, max_hops: int = 16):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1", "User-Agent": self.ua,
        }
        if referer:
            headers["Referer"] = referer
        current_url = start_url
        last_url = start_url
        for hop in range(max_hops):
            try:
                resp = self.session.get(current_url, headers=headers, allow_redirects=False,
                                        timeout=30, impersonate=self.impersonate)
            except Exception as e:
                maybe_localhost = re.search(r'(https?://localhost[^\s\'\"]+)', str(e))
                if maybe_localhost:
                    code = _extract_code_from_url(maybe_localhost.group(1))
                    if code:
                        return code, maybe_localhost.group(1)
                return None, last_url
            last_url = str(resp.url)
            code = _extract_code_from_url(last_url)
            if code:
                return code, last_url
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", "")
                if not loc:
                    return None, last_url
                if loc.startswith("/"):
                    loc = f"{OAUTH_ISSUER}{loc}"
                code = _extract_code_from_url(loc)
                if code:
                    return code, loc
                current_url = loc
                headers["Referer"] = last_url
                continue
            return None, last_url
        return None, last_url

    def _oauth_submit_workspace_and_org(self, consent_url: str):
        session_data = self._decode_oauth_session_cookie()
        if not session_data:
            return None
        workspaces = session_data.get("workspaces", [])
        if not workspaces:
            return None
        workspace_id = (workspaces[0] or {}).get("id")
        if not workspace_id:
            return None

        h = {
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": OAUTH_ISSUER, "Referer": consent_url,
            "User-Agent": self.ua, "oai-device-id": self.device_id,
        }
        h.update(_make_trace_headers())

        resp = self.session.post(
            f"{OAUTH_ISSUER}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id}, headers=h,
            allow_redirects=False, timeout=30, impersonate=self.impersonate,
        )
        self._print(f"[OAuth] workspace/select -> {resp.status_code}")

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc.startswith("/"):
                loc = f"{OAUTH_ISSUER}{loc}"
            code = _extract_code_from_url(loc)
            if code:
                return code
            code, _ = self._oauth_follow_for_code(loc, referer=consent_url)
            if not code:
                code = self._oauth_allow_redirect_extract_code(loc, referer=consent_url)
            return code

        if resp.status_code != 200:
            return None

        try:
            ws_data = resp.json()
        except Exception:
            return None

        ws_next = ws_data.get("continue_url", "")
        orgs = ws_data.get("data", {}).get("orgs", [])

        org_id = None
        project_id = None
        if orgs:
            org_id = (orgs[0] or {}).get("id")
            projects = (orgs[0] or {}).get("projects", [])
            if projects:
                project_id = (projects[0] or {}).get("id")

        if org_id:
            org_body = {"org_id": org_id}
            if project_id:
                org_body["project_id"] = project_id
            h_org = dict(h)
            if ws_next:
                h_org["Referer"] = ws_next if ws_next.startswith("http") else f"{OAUTH_ISSUER}{ws_next}"
            resp_org = self.session.post(
                f"{OAUTH_ISSUER}/api/accounts/organization/select",
                json=org_body, headers=h_org, allow_redirects=False,
                timeout=30, impersonate=self.impersonate,
            )
            self._print(f"[OAuth] organization/select -> {resp_org.status_code}")
            if resp_org.status_code in (301, 302, 303, 307, 308):
                loc = resp_org.headers.get("Location", "")
                if loc.startswith("/"):
                    loc = f"{OAUTH_ISSUER}{loc}"
                code = _extract_code_from_url(loc)
                if code:
                    return code
                code, _ = self._oauth_follow_for_code(loc, referer=h_org.get("Referer"))
                if not code:
                    code = self._oauth_allow_redirect_extract_code(loc, referer=h_org.get("Referer"))
                return code
            if resp_org.status_code == 200:
                try:
                    org_data = resp_org.json()
                except Exception:
                    return None
                org_next = org_data.get("continue_url", "")
                if org_next:
                    if org_next.startswith("/"):
                        org_next = f"{OAUTH_ISSUER}{org_next}"
                    code, _ = self._oauth_follow_for_code(org_next, referer=h_org.get("Referer"))
                    if not code:
                        code = self._oauth_allow_redirect_extract_code(org_next, referer=h_org.get("Referer"))
                    return code

        if ws_next:
            if ws_next.startswith("/"):
                ws_next = f"{OAUTH_ISSUER}{ws_next}"
            code, _ = self._oauth_follow_for_code(ws_next, referer=consent_url)
            if not code:
                code = self._oauth_allow_redirect_extract_code(ws_next, referer=consent_url)
            return code

        return None

    def perform_codex_oauth_login_http(self, email: str, password: str, mail_token: str = None,
                                        provider: str = "duckmail"):
        self._print("[OAuth] 开始执行 Codex OAuth 纯协议流程...")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

        code_verifier, code_challenge = _generate_pkce()
        state = secrets.token_urlsafe(24)

        authorize_params = {
            "response_type": "code", "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge, "code_challenge_method": "S256",
            "state": state,
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        authorize_url = f"{OAUTH_ISSUER}/oauth/authorize?{urlencode(authorize_params)}"

        def _oauth_json_headers(referer: str):
            h = {
                "Accept": "application/json", "Content-Type": "application/json",
                "Origin": OAUTH_ISSUER, "Referer": referer,
                "User-Agent": self.ua, "oai-device-id": self.device_id,
            }
            h.update(_make_trace_headers())
            return h

        def _bootstrap_oauth_session():
            self._print("[OAuth] 1/7 GET /oauth/authorize")
            try:
                r = self.session.get(authorize_url, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1",
                    "User-Agent": self.ua,
                }, allow_redirects=True, timeout=30, impersonate=self.impersonate)
            except Exception as e:
                self._print(f"[OAuth] /oauth/authorize 异常: {e}")
                return False, ""

            final_url = str(r.url)
            has_login = any(getattr(c, "name", "") == "login_session" for c in self.session.cookies)

            if not has_login:
                oauth2_url = f"{OAUTH_ISSUER}/api/oauth/oauth2/auth"
                try:
                    r2 = self.session.get(oauth2_url, headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": authorize_url, "Upgrade-Insecure-Requests": "1",
                        "User-Agent": self.ua,
                    }, params=authorize_params, allow_redirects=True, timeout=30,
                        impersonate=self.impersonate)
                    final_url = str(r2.url)
                except Exception as e:
                    self._print(f"[OAuth] /api/oauth/oauth2/auth 异常: {e}")
                has_login = any(getattr(c, "name", "") == "login_session" for c in self.session.cookies)

            return has_login, final_url

        def _post_authorize_continue(referer_url: str):
            sentinel_authorize = build_sentinel_token(
                self.session, self.device_id, flow="authorize_continue",
                user_agent=self.ua, sec_ch_ua=self.sec_ch_ua, impersonate=self.impersonate,
            )
            if not sentinel_authorize:
                self._print("[OAuth] sentinel authorize token 生成失败")
                return None
            headers_continue = _oauth_json_headers(referer_url)
            headers_continue["openai-sentinel-token"] = sentinel_authorize
            try:
                return self.session.post(
                    f"{OAUTH_ISSUER}/api/accounts/authorize/continue",
                    json={"username": {"kind": "email", "value": email}},
                    headers=headers_continue, timeout=30,
                    allow_redirects=False, impersonate=self.impersonate,
                )
            except Exception as e:
                self._print(f"[OAuth] authorize/continue 异常: {e}")
                return None

        has_login_session, authorize_final_url = _bootstrap_oauth_session()
        if not authorize_final_url:
            return None

        continue_referer = (authorize_final_url if authorize_final_url.startswith(OAUTH_ISSUER)
                            else f"{OAUTH_ISSUER}/log-in")

        self._print("[OAuth] 2/7 POST /api/accounts/authorize/continue")
        resp_continue = _post_authorize_continue(continue_referer)
        if resp_continue is None:
            self._print("[OAuth] authorize/continue 请求未发出或失败")
            return None

        self._print(f"[OAuth] /authorize/continue -> {resp_continue.status_code}")
        if resp_continue.status_code == 400 and "invalid_auth_step" in (resp_continue.text or ""):
            has_login_session, authorize_final_url = _bootstrap_oauth_session()
            if not authorize_final_url:
                return None
            continue_referer = (authorize_final_url if authorize_final_url.startswith(OAUTH_ISSUER)
                                else f"{OAUTH_ISSUER}/log-in")
            resp_continue = _post_authorize_continue(continue_referer)
            if resp_continue is None:
                self._print("[OAuth] authorize/continue 重试失败")
                return None

        if resp_continue.status_code != 200:
            self._print(f"[OAuth] authorize/continue 非200: {resp_continue.status_code}, body={resp_continue.text[:220]}")
            return None

        try:
            continue_data = resp_continue.json()
        except Exception:
            self._print(f"[OAuth] authorize/continue JSON 解析失败: {resp_continue.text[:220]}")
            return None

        continue_url = continue_data.get("continue_url", "")
        page_type = (continue_data.get("page") or {}).get("type", "")

        self._print("[OAuth] 3/7 POST /api/accounts/password/verify")
        sentinel_pwd = build_sentinel_token(
            self.session, self.device_id, flow="password_verify",
            user_agent=self.ua, sec_ch_ua=self.sec_ch_ua, impersonate=self.impersonate,
        )
        if not sentinel_pwd:
            self._print("[OAuth] sentinel password token 生成失败")
            return None

        headers_verify = _oauth_json_headers(f"{OAUTH_ISSUER}/log-in/password")
        headers_verify["openai-sentinel-token"] = sentinel_pwd

        try:
            resp_verify = self.session.post(
                f"{OAUTH_ISSUER}/api/accounts/password/verify",
                json={"password": password}, headers=headers_verify,
                timeout=30, allow_redirects=False, impersonate=self.impersonate,
            )
        except Exception as e:
            self._print(f"[OAuth] password/verify 异常: {e}")
            return None

        if resp_verify.status_code != 200:
            self._print(f"[OAuth] password/verify 非200: {resp_verify.status_code}, body={resp_verify.text[:220]}")
            return None

        try:
            verify_data = resp_verify.json()
        except Exception:
            self._print(f"[OAuth] password/verify JSON 解析失败: {resp_verify.text[:220]}")
            return None

        continue_url = verify_data.get("continue_url", "") or continue_url
        page_type = (verify_data.get("page") or {}).get("type", "") or page_type

        need_oauth_otp = (
            page_type == "email_otp_verification"
            or "email-verification" in (continue_url or "")
            or "email-otp" in (continue_url or "")
        )

        if need_oauth_otp:
            self._print("[OAuth] 4/7 检测到邮箱 OTP 验证")
            if not mail_token:
                self._print("[OAuth] OAuth 阶段需要邮箱 OTP，但未提供 mail_token")
                return None

            headers_otp = _oauth_json_headers(f"{OAUTH_ISSUER}/email-verification")
            tried_codes: set = set()
            otp_success = False
            otp_deadline = time.time() + 120

            while time.time() < otp_deadline and not otp_success:
                candidate_codes = []
                if provider in SHARED_EMAIL_KINDS:
                    code = self._shared_email_adapter.wait_for_code(
                        provider=provider,
                        mail_token=mail_token,
                        email=email,
                        timeout=3,
                        extractor=self._extract_verification_code,
                        message_filter=lambda content: ("openai" in content.lower() or "chatgpt" in content.lower()),
                        poll_interval=1,
                    )
                    if code and code not in tried_codes:
                        candidate_codes.append(code)

                if not candidate_codes:
                    elapsed = int(120 - max(0, otp_deadline - time.time()))
                    self._print(f"[OAuth] OTP 等待中... ({elapsed}s/120s)")
                    time.sleep(2)
                    continue

                for otp_code in candidate_codes:
                    tried_codes.add(otp_code)
                    self._print(f"[OAuth] 尝试 OTP: {otp_code}")
                    try:
                        resp_otp = self.session.post(
                            f"{OAUTH_ISSUER}/api/accounts/email-otp/validate",
                            json={"code": otp_code}, headers=headers_otp,
                            timeout=30, allow_redirects=False, impersonate=self.impersonate,
                        )
                    except Exception as e:
                        self._print(f"[OAuth] email-otp/validate 异常: {e}")
                        continue
                    if resp_otp.status_code != 200:
                        continue
                    try:
                        otp_data = resp_otp.json()
                    except Exception:
                        continue
                    continue_url = otp_data.get("continue_url", "") or continue_url
                    page_type = (otp_data.get("page") or {}).get("type", "") or page_type
                    self._otp_received = True
                    self._oauth_otp_received = True
                    otp_success = True
                    break

                if not otp_success:
                    time.sleep(2)

            if not otp_success:
                self._print(f"[OAuth] OAuth 阶段 OTP 验证失败")
                return None

        code = None
        consent_url = continue_url
        if consent_url and consent_url.startswith("/"):
            consent_url = f"{OAUTH_ISSUER}{consent_url}"
        if not consent_url and "consent" in page_type:
            consent_url = f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent"
        if consent_url:
            code = _extract_code_from_url(consent_url)

        if not code and consent_url:
            self._print("[OAuth] 5/7 跟随 continue_url 提取 code")
            code, _ = self._oauth_follow_for_code(consent_url, referer=f"{OAUTH_ISSUER}/log-in/password")

        consent_hint = (
                ("consent" in (consent_url or ""))
                or ("sign-in-with-chatgpt" in (consent_url or ""))
                or ("workspace" in (consent_url or ""))
                or ("organization" in (consent_url or ""))
                or ("consent" in page_type)
                or ("organization" in page_type)
        )

        if not code and consent_hint:
            if not consent_url:
                consent_url = f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent"
            self._print("[OAuth] 6/7 执行 workspace/org 选择")
            code = self._oauth_submit_workspace_and_org(consent_url)

        if not code:
            fallback_consent = f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent"
            self._print("[OAuth] 6/7 回退 consent 路径重试")
            code = self._oauth_submit_workspace_and_org(fallback_consent)
            if not code:
                code, _ = self._oauth_follow_for_code(fallback_consent, referer=f"{OAUTH_ISSUER}/log-in/password")

        if not code:
            self._print("[OAuth] 未获取到 authorization code")
            return None

        self._print("[OAuth] 7/7 POST /oauth/token")
        token_resp = self.session.post(
            f"{OAUTH_ISSUER}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.ua},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            timeout=60,
            impersonate=self.impersonate,
        )
        self._print(f"[OAuth] /oauth/token -> {token_resp.status_code}")

        if token_resp.status_code != 200:
            self._print(f"[OAuth] token 交换失败: {token_resp.status_code} {token_resp.text[:200]}")
            return None

        try:
            data = token_resp.json()
        except Exception:
            self._print("[OAuth] token 响应解析失败")
            return None

        if not data.get("access_token"):
            self._print("[OAuth] token 响应缺少 access_token")
            return None

        self._print("[OAuth] Codex Token 获取成功")
        return data

    # ==================== 并发批量注册 ====================

def _register_one(idx, total, proxy, output_file):
    """单个注册任务，根据 MAIL_PROVIDER 选择邮箱服务"""
    reg = None
    email = ""
    selected_credential: Optional[Dict[str, Any]] = None
    otp_reported = False
    try:
        reg = ChatGPTRegister(proxy=proxy, tag=f"{idx}")
        selected_credential, provider, email, email_pwd, mail_token = _acquire_working_email_mailbox(reg, idx)
        provider_name = str((selected_credential or {}).get("name") or provider)

        tag = email.split("@")[0]
        reg.tag = tag

        chatgpt_password = _generate_password()
        name = _random_name()
        birthdate = _random_birthdate()

        with _print_lock:
            print(f"\n{'=' * 60}")
            print(f"  [{idx}/{total}] 注册: {email}")
            print(f"  邮箱账号: {email}")
            print(f"  邮箱服务: {provider}")
            if selected_credential:
                print(f"  邮箱凭证: {provider_name}")
            print(f"  ChatGPT密码: {chatgpt_password}")
            if email_pwd:
                print(f"  邮箱密码: {email_pwd}")
            print(f"  姓名: {name} | 生日: {birthdate}")
            print(f"{'=' * 60}")

        # 执行注册流程
        reg.run_register(email, chatgpt_password, name, birthdate, mail_token, provider=provider)
        if getattr(reg, "_otp_received", False):
            _report_email_credential_event(selected_credential, "otp_received", email=email, worker_index=idx)
            otp_reported = True
        _report_email_credential_event(selected_credential, "account_created", email=email, worker_index=idx)

        # OAuth（可选）
        oauth_ok = True
        if ENABLE_OAUTH:
            reg._print("[OAuth] 开始获取 Codex Token...")
            tokens = reg.perform_codex_oauth_login_http(
                email, chatgpt_password, mail_token=mail_token, provider=provider
            )
            oauth_ok = bool(tokens and tokens.get("access_token"))
            if oauth_ok:
                _save_codex_tokens(email, tokens)
                reg._print("[OAuth] Token 已保存")
                _report_email_credential_event(selected_credential, "oauth_success", email=email, worker_index=idx)
            else:
                msg = "OAuth 获取失败"
                if OAUTH_REQUIRED:
                    raise Exception(f"{msg}（oauth_required=true）")
                reg._print(f"[OAuth] {msg}（按配置继续）")

        # 线程安全写入结果
        with _file_lock:
            with open(output_file, "a", encoding="utf-8") as out:
                line = f"{email}----{chatgpt_password}"
                if email_pwd:
                    line += f"----{email_pwd}"
                line += f"----oauth={'ok' if oauth_ok else 'fail'}\n"
                out.write(line)

        with _print_lock:
            print(f"\n[OK] [{tag}] {email} 注册成功!")
        _report_email_credential_event(selected_credential, "task_success", email=email, worker_index=idx)
        return True, email, None

    except Exception as e:
        error_msg = str(e)
        if selected_credential and reg is not None and getattr(reg, "_otp_received", False) and not otp_reported:
            _report_email_credential_event(selected_credential, "otp_received", email=email, worker_index=idx)
        reason = _classify_email_credential_failure(error_msg)
        detail = error_msg[:300]
        if detail:
            reason = f"{reason}: {detail}"
        _report_email_credential_event(
            selected_credential,
            "failure",
            reason=reason,
            email=email,
            worker_index=idx,
        )
        with _print_lock:
            print(f"\n[FAIL] [{idx}] 注册失败: {error_msg}")
            traceback.print_exc()
        return False, None, error_msg

def run_batch(total_accounts: int = 3, output_file="registered_accounts.txt",
              max_workers=3, proxy=None, cpa_cleanup=None, cpa_upload_every_n: int = 3):
    """并发批量注册"""
    provider = MAIL_PROVIDER
    selected_credentials = list(SELECTED_EMAIL_CREDENTIALS or [])
    cfmail_accounts = _current_cfmail_accounts() if provider == "cfmail" and not selected_credentials else []

    # 确保输出目录存在
    output_dir = os.path.dirname(output_file)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
    if TOKEN_JSON_DIR and not os.path.exists(TOKEN_JSON_DIR):
        os.makedirs(TOKEN_JSON_DIR, exist_ok=True)

    # 检查邮箱服务配置
    if selected_credentials:
        for credential in selected_credentials:
            kind = str(credential.get("kind") or "").strip().lower()
            if kind == "gptmail" and (not credential.get("base_url") or not credential.get("api_key")):
                print(f"❌ 错误: GPTMail 凭证 {credential.get('name') or ''} 缺少 base_url 或 api_key")
                return
            if kind == "duckmail" and not credential.get("api_key"):
                print(f"❌ 错误: DuckMail 凭证 {credential.get('name') or ''} 缺少 Bearer Token")
                return
            if kind == "cfmail" and not credential.get("base_url"):
                print(f"❌ 错误: CFMail 凭证 {credential.get('name') or ''} 缺少配置文件路径")
                return
    else:
        if provider == "cfmail":
            if not cfmail_accounts:
                print(f"❌ 错误: mail_provider=cfmail 但未找到可用的 cfmail 配置")
                print(f"   请检查配置文件: {_CFMAIL_CONFIG_PATH}")
                return
        elif provider == "duckmail":
            if not DUCKMAIL_BEARER:
                print("❌ 错误: mail_provider=duckmail 但未设置 DUCKMAIL_BEARER")
                print("   请在 config.json 中设置 duckmail_bearer，或将 mail_provider 改为 cfmail/tempmail_lol/gptmail")
                return
        elif provider == "gptmail":
            if not GPTMAIL_API_BASE or not GPTMAIL_API_KEY:
                print("❌ 错误: mail_provider=gptmail 但未设置 GPTMAIL_API_BASE 或 GPTMAIL_API_KEY")
                print("   请在 config.json 中设置 gptmail_api_base 和 gptmail_api_key")
                return
        elif provider not in {"tempmail_lol", "mail_tm", "mail_gw"}:
            print(f"❌ 错误: 不支持的 mail_provider={provider}")
            print("   可选值: duckmail / cfmail / tempmail_lol / gptmail / mail_tm / mail_gw")
            return

    actual_workers = min(max_workers, total_accounts)
    print(f"\n{'#' * 60}")
    print(f"  ChatGPT 批量自动注册")
    print(f"  注册数量: {total_accounts} | 并发数: {actual_workers}")
    print(f"  邮箱服务: {provider}")
    if provider == "cfmail":
        cfmail_names = ", ".join(a.name for a in cfmail_accounts) if cfmail_accounts else "(未加载)"
        print(f"  cfmail 配置: {cfmail_names}")
        print(f"  cfmail 模式: {CFMAIL_PROFILE_MODE}")
    elif provider == "duckmail":
        print(f"  DuckMail: {DUCKMAIL_API_BASE}")
    elif provider == "mail_tm":
        print(f"  Mail.tm: {MAIL_TM_API_BASE}")
        print(f"  Mail.tm Domain: {MAIL_TM_DOMAIN or 'auto'}")
    elif provider == "mail_gw":
        print(f"  Mail.gw: {MAIL_GW_API_BASE}")
        print(f"  Mail.gw Domain: {MAIL_GW_DOMAIN or 'auto'}")
    elif provider == "tempmail_lol":
        print(f"  TempMail.lol: {TEMPMAIL_LOL_API_BASE}")
    elif provider == "gptmail":
        print(f"  GPTMail: {GPTMAIL_API_BASE}")
        print(f"  GPTMail Domain: {GPTMAIL_DOMAIN}")
    print(f"  OAuth: {'开启' if ENABLE_OAUTH else '关闭'} | required: {'是' if OAUTH_REQUIRED else '否'}")
    if ENABLE_OAUTH:
        print(f"  Token输出: {TOKEN_JSON_DIR}/, {AK_FILE}, {RK_FILE}")
    print(f"  CPA分批上传: 每 {max(1, int(cpa_upload_every_n))} 个成功账号触发一次")
    print(f"  输出文件: {output_file}")
    print(f"{'#' * 60}\n")

    # 注册前清理 CPA 无效号
    do_cleanup = cpa_cleanup if cpa_cleanup is not None else CPA_CLEANUP_ENABLED
    if do_cleanup and UPLOAD_API_URL:
        _run_cpa_cleanup_before_register()

    success_count = 0
    fail_count = 0
    completed_count = 0
    start_time = time.time()
    upload_every_n = max(1, int(cpa_upload_every_n or 1))
    since_last_upload = 0

    # 初始化底部进度条
    _render_apt_like_progress(completed_count, total_accounts, success_count, fail_count, start_time)

    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {}
        for idx in range(1, total_accounts + 1):
            future = executor.submit(_register_one, idx, total_accounts, proxy, output_file)
            futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            try:
                ok, email, err = future.result()
                if ok:
                    success_count += 1
                    since_last_upload += 1
                    if UPLOAD_API_URL and since_last_upload >= upload_every_n:
                        print(f"\n[CPA] 达到分批上传阈值: {since_last_upload}/{upload_every_n}，开始上传...")
                        _upload_all_tokens_to_cpa(output_file=output_file)
                        since_last_upload = 0
                else:
                    fail_count += 1
                    print(f"  [账号 {idx}] 失败: {err}")
            except Exception as e:
                fail_count += 1
                with _print_lock:
                    print(f"[FAIL] 账号 {idx} 线程异常: {e}")
            finally:
                completed_count += 1
                _render_apt_like_progress(completed_count, total_accounts, success_count, fail_count, start_time)

    # 结束进度条占用行
    with _print_lock:
        print()

    elapsed = time.time() - start_time
    avg = elapsed / total_accounts if total_accounts else 0
    print(f"\n{'#' * 60}")
    print(f"  注册完成! 耗时 {elapsed:.1f} 秒")
    print(f"  总数: {total_accounts} | 成功: {success_count} | 失败: {fail_count}")
    print(f"  平均速度: {avg:.1f} 秒/个")
    if success_count > 0:
        print(f"  结果文件: {output_file}")
    print(f"{'#' * 60}")

    if success_count > 0:
        if UPLOAD_API_URL and since_last_upload > 0:
            print(f"\n[CPA] 收尾上传剩余 {since_last_upload} 个成功账号对应 token...")
        _upload_all_tokens_to_cpa(output_file=output_file)


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="ChatGPT 批量自动注册工具")
    parser.add_argument("--non-interactive", action="store_true",
                        help="非交互模式，所有参数通过命令行传入")
    parser.add_argument("--output-dir", help="输出目录")
    parser.add_argument("--proxy", help="HTTP 代理地址")
    parser.add_argument("--count", type=int, default=10, help="注册账号数量")
    parser.add_argument("--concurrency", type=int, default=3, help="并发数")
    parser.add_argument("--email-credentials-file", help="统一邮箱凭证配置文件(JSON)")
    # GPTMail 参数
    parser.add_argument("--gptmail-api-base", help="GPTMail API 地址")
    parser.add_argument("--gptmail-api-key", help="GPTMail API Key")
    parser.add_argument("--gptmail-domain", help="GPTMail 邮箱域名")
    # DuckMail 参数
    parser.add_argument("--duckmail-api-base", help="DuckMail API 地址")
    parser.add_argument("--duckmail-bearer", help="DuckMail Bearer Token")
    # Mail.tm / Mail.gw 参数
    parser.add_argument("--mail-tm-api-base", help="Mail.tm API 地址")
    parser.add_argument("--mail-tm-domain", help="Mail.tm 指定域名")
    parser.add_argument("--mail-gw-api-base", help="Mail.gw API 地址")
    parser.add_argument("--mail-gw-domain", help="Mail.gw 指定域名")
    # CFMail 参数
    parser.add_argument("--cfmail-config", help="CFMail 配置文件路径")
    # CPA 参数
    parser.add_argument("--cpa-url", help="CPA 平台 API 地址")
    parser.add_argument("--cpa-token", help="CPA 平台 API Token")
    parser.add_argument("--cpa-upload-every-n", type=int, default=3, help="每 N 个成功账号上传 CPA")
    return parser.parse_args()


def apply_args_to_config(args):
    """将命令行参数应用到全局配置"""
    global MAIL_PROVIDER, GPTMAIL_API_BASE, GPTMAIL_API_KEY, GPTMAIL_DOMAIN
    global DUCKMAIL_API_BASE, DUCKMAIL_BEARER
    global MAIL_TM_API_BASE, MAIL_TM_DOMAIN, MAIL_GW_API_BASE, MAIL_GW_DOMAIN
    global TEMPMAIL_LOL_API_BASE
    global UPLOAD_API_URL, UPLOAD_API_TOKEN, CPA_UPLOAD_EVERY_N
    global DEFAULT_PROXY, DEFAULT_TOTAL_ACCOUNTS, DEFAULT_OUTPUT_FILE
    global _CFMAIL_CONFIG_PATH
    global SELECTED_EMAIL_CREDENTIALS, MULTI_MAIL_PROVIDERS

    if args.proxy is not None:
        DEFAULT_PROXY = args.proxy
    if args.count:
        DEFAULT_TOTAL_ACCOUNTS = args.count
    if args.output_dir:
        DEFAULT_OUTPUT_FILE = os.path.join(args.output_dir, "registered_accounts.txt")
        # 同时设置 token 目录
        global TOKEN_JSON_DIR, AK_FILE, RK_FILE
        TOKEN_JSON_DIR = os.path.join(args.output_dir, "codex_tokens")
        AK_FILE = os.path.join(args.output_dir, "ak.txt")
        RK_FILE = os.path.join(args.output_dir, "rk.txt")

    if args.email_credentials_file:
        SELECTED_EMAIL_CREDENTIALS = _load_email_credentials_from_file(args.email_credentials_file)
        if SELECTED_EMAIL_CREDENTIALS:
            MAIL_PROVIDER = str(SELECTED_EMAIL_CREDENTIALS[0].get("kind") or MAIL_PROVIDER).strip().lower()
            MULTI_MAIL_PROVIDERS = [str(item.get("kind") or "").strip().lower() for item in SELECTED_EMAIL_CREDENTIALS]

    # GPTMail
    if args.gptmail_api_base:
        GPTMAIL_API_BASE = args.gptmail_api_base
    if args.gptmail_api_key:
        GPTMAIL_API_KEY = args.gptmail_api_key
    if args.gptmail_domain:
        GPTMAIL_DOMAIN = args.gptmail_domain

    # DuckMail
    if args.duckmail_api_base:
        DUCKMAIL_API_BASE = args.duckmail_api_base
    if args.duckmail_bearer:
        DUCKMAIL_BEARER = args.duckmail_bearer

    if args.mail_tm_api_base:
        MAIL_TM_API_BASE = args.mail_tm_api_base
    if args.mail_tm_domain is not None:
        MAIL_TM_DOMAIN = args.mail_tm_domain
    if args.mail_gw_api_base:
        MAIL_GW_API_BASE = args.mail_gw_api_base
    if args.mail_gw_domain is not None:
        MAIL_GW_DOMAIN = args.mail_gw_domain

    # CFMail
    if args.cfmail_config:
        _CFMAIL_CONFIG_PATH = args.cfmail_config
    configure_cfmail_defaults(_CFMAIL_CONFIG_PATH, CFMAIL_PROFILE_MODE)

    # CPA
    if args.cpa_url:
        UPLOAD_API_URL = args.cpa_url
    if args.cpa_token:
        UPLOAD_API_TOKEN = args.cpa_token
    if args.cpa_upload_every_n:
        CPA_UPLOAD_EVERY_N = args.cpa_upload_every_n


# 多邮箱配置（命令行指定）
MULTI_MAIL_PROVIDERS: List[str] = []
SELECTED_EMAIL_CREDENTIALS: List[Dict[str, Any]] = []


def _load_email_credentials_from_file(path: str) -> List[Dict[str, Any]]:
    return load_shared_email_credentials_from_file(path)


def _pick_email_credential(idx: int, exclude_ids: Optional[set[int]] = None) -> Optional[Dict[str, Any]]:
    selected = _acquire_email_credential_from_manager(idx, exclude_ids=exclude_ids)
    if selected:
        return dict(selected)
    if not SELECTED_EMAIL_CREDENTIALS:
        return None
    blocked = exclude_ids or set()
    start_index = (max(1, idx) - 1) % len(SELECTED_EMAIL_CREDENTIALS)
    for offset in range(len(SELECTED_EMAIL_CREDENTIALS)):
        candidate = dict(SELECTED_EMAIL_CREDENTIALS[(start_index + offset) % len(SELECTED_EMAIL_CREDENTIALS)])
        raw_id = candidate.get("id")
        candidate_id = raw_id if isinstance(raw_id, int) else int(str(raw_id).strip()) if str(raw_id or "").strip().isdigit() else None
        if candidate_id is not None and candidate_id in blocked:
            continue
        return candidate
    return None


def _email_manager_enabled() -> bool:
    return bool(EMAIL_CREDENTIAL_MANAGER_URL and EMAIL_CREDENTIAL_MANAGER_TOKEN)


def _email_manager_task_id() -> Optional[int]:
    return int(EMAIL_CREDENTIAL_MANAGER_TASK_ID) if EMAIL_CREDENTIAL_MANAGER_TASK_ID.isdigit() else None


def _email_manager_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not _email_manager_enabled():
        raise RuntimeError("email manager disabled")
    request = urllib_request.Request(
        f"{EMAIL_CREDENTIAL_MANAGER_URL}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": EMAIL_CREDENTIAL_MANAGER_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"email manager http {exc.code}: {detail[:300]}") from exc
    except Exception as exc:
        raise RuntimeError(f"email manager request failed: {exc}") from exc

    try:
        data = json.loads(body or "{}")
    except Exception as exc:
        raise RuntimeError(f"email manager invalid response: {body[:300]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("email manager response must be an object")
    return data


def _candidate_credential_ids(exclude_ids: Optional[set[int]] = None) -> List[int]:
    blocked = exclude_ids or set()
    ids: List[int] = []
    for item in SELECTED_EMAIL_CREDENTIALS:
        raw_id = item.get("id")
        if isinstance(raw_id, int):
            if raw_id not in blocked:
                ids.append(raw_id)
        elif str(raw_id or "").strip().isdigit():
            credential_id = int(str(raw_id).strip())
            if credential_id not in blocked:
                ids.append(credential_id)
    return ids


def _acquire_email_credential_from_manager(idx: int, exclude_ids: Optional[set[int]] = None) -> Optional[Dict[str, Any]]:
    candidate_ids = _candidate_credential_ids(exclude_ids)
    if not candidate_ids:
        return None
    try:
        data = _email_manager_post(
            "/acquire",
            {
                "platform": EMAIL_CREDENTIAL_MANAGER_PLATFORM,
                "candidate_ids": candidate_ids,
                "task_id": _email_manager_task_id(),
                "worker_index": idx,
            },
        )
    except Exception as exc:
        with _print_lock:
            print(f"[email-manager] 获取邮箱凭证失败，回退本地轮询: {exc}")
        return None
    credential = data.get("credential")
    if not isinstance(credential, dict):
        return None
    item = dict(credential)
    dispatch = data.get("dispatch")
    if isinstance(dispatch, dict):
        stat = dispatch.get("stat")
        selection = dispatch.get("selection")
        if isinstance(stat, dict):
            item["_dispatch_stat"] = dict(stat)
        if isinstance(selection, dict):
            item["_dispatch_selection"] = dict(selection)
    return item


def _acquire_working_email_mailbox(reg: ChatGPTRegister, idx: int) -> tuple[Dict[str, Any] | None, str, str, str, str]:
    tried_ids: set[int] = set()
    last_error = "No email credentials configured"
    while True:
        selected_credential = _pick_email_credential(idx, exclude_ids=tried_ids)
        if not selected_credential:
            raise Exception(f"所有邮箱凭证建箱失败: {last_error}")
        raw_id = selected_credential.get("id")
        credential_id = raw_id if isinstance(raw_id, int) else int(str(raw_id).strip()) if str(raw_id or "").strip().isdigit() else None
        if credential_id is not None:
            tried_ids.add(credential_id)
        reg.apply_email_credential(selected_credential)
        _log_email_credential_selection(selected_credential, worker_index=idx)
        provider = str((selected_credential or {}).get("kind") or MAIL_PROVIDER).strip().lower()
        try:
            email, email_pwd, mail_token = reg.create_email_mailbox(provider)
            _report_email_credential_event(selected_credential, "mailbox_created", email=email, worker_index=idx)
            return selected_credential, provider, email, email_pwd, mail_token
        except Exception as exc:
            last_error = str(exc)
            reason = f"mailbox_create_failed: {last_error[:300]}"
            _report_email_credential_event(selected_credential, "failure", reason=reason, worker_index=idx)
            with _print_lock:
                print(f"[email-manager] [{idx}] 邮箱凭证建箱失败，尝试切换下一个: {provider} - {last_error}")


def _format_dispatch_weight(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def _log_email_credential_selection(credential: Dict[str, Any], *, worker_index: Optional[int] = None) -> None:
    name = str(credential.get("name") or credential.get("kind") or "unknown")
    kind = str(credential.get("kind") or "").strip().lower() or "unknown"
    credential_id = credential.get("id")
    stat = credential.get("_dispatch_stat") if isinstance(credential.get("_dispatch_stat"), dict) else {}
    selection = credential.get("_dispatch_selection") if isinstance(credential.get("_dispatch_selection"), dict) else {}
    current_weight = _format_dispatch_weight(stat.get("dynamic_weight"))
    candidate_count = selection.get("candidate_count")
    worker_label = f"[{worker_index}] " if worker_index is not None else ""
    with _print_lock:
        print(
            f"[email-manager] {worker_label}选中邮箱凭证: {name}"
            f" (id={credential_id}, kind={kind}, 当前权重={current_weight}, 候选={candidate_count or 1})"
        )


def _log_email_credential_event(
    credential: Dict[str, Any],
    event: str,
    *,
    email: str = "",
    previous_stat: Optional[Dict[str, Any]] = None,
    updated_stat: Optional[Dict[str, Any]] = None,
    reason: str = "",
    worker_index: Optional[int] = None,
) -> None:
    name = str(credential.get("name") or credential.get("kind") or "unknown")
    kind = str(credential.get("kind") or "").strip().lower() or "unknown"
    credential_id = credential.get("id")
    before_weight = _format_dispatch_weight((previous_stat or {}).get("dynamic_weight"))
    after_weight = _format_dispatch_weight((updated_stat or {}).get("dynamic_weight"))
    worker_label = f"[{worker_index}] " if worker_index is not None else ""
    parts = [
        f"[email-manager] {worker_label}邮箱凭证事件: {name}",
        f"(id={credential_id}, kind={kind})",
        f"event={event}",
        f"weight {before_weight} -> {after_weight}",
    ]
    if email:
        parts.append(f"email={email}")
    if updated_stat and updated_stat.get("consecutive_failures") is not None:
        parts.append(f"连续失败={updated_stat.get('consecutive_failures')}")
    if reason:
        parts.append(f"reason={reason[:200]}")
    with _print_lock:
        print(" ".join(parts))


def _report_email_credential_event(
    credential: Optional[Dict[str, Any]],
    event: str,
    *,
    reason: str = "",
    email: str = "",
    worker_index: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if not credential or not _email_manager_enabled():
        return None
    raw_id = credential.get("id")
    if isinstance(raw_id, int):
        credential_id = raw_id
    elif str(raw_id or "").strip().isdigit():
        credential_id = int(str(raw_id).strip())
    else:
        return None
    previous_stat = credential.get("_dispatch_stat") if isinstance(credential.get("_dispatch_stat"), dict) else None
    try:
        data = _email_manager_post(
            "/report",
            {
                "platform": EMAIL_CREDENTIAL_MANAGER_PLATFORM,
                "credential_id": credential_id,
                "event": event,
                "reason": reason or None,
                "task_id": _email_manager_task_id(),
                "worker_index": worker_index,
            },
        )
        stat = data.get("stat")
        if isinstance(stat, dict):
            credential["_dispatch_stat"] = dict(stat)
            _log_email_credential_event(
                credential,
                event,
                email=email,
                previous_stat=previous_stat,
                updated_stat=stat,
                reason=reason,
                worker_index=worker_index,
            )
            return dict(stat)
    except Exception as exc:
        with _print_lock:
            print(f"[email-manager] 回报邮箱凭证事件失败({event}): {exc}")
    return None


def _classify_email_credential_failure(error_message: str) -> str:
    text = (error_message or "").strip().lower()
    if not text:
        return "unknown_error"
    if "unsupported email" in text or "unsupported_email" in text:
        return "unsupported_email"
    if "registration disallowed" in text or "registration_disallowed" in text or "disallowed" in text:
        return "registration_disallowed"
    if "oauth" in text:
        return "oauth_failed"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    if "403" in text or "forbidden" in text or "blocked" in text or "block" in text:
        return "blocked_403"
    if "otp" in text or "验证码" in text:
        return "otp_failed"
    if "proxy" in text:
        return "proxy_failed"
    return "unknown_error"


def main():
    args = parse_args()

    if args.non_interactive:
        # 非交互模式：直接使用命令行参数
        apply_args_to_config(args)
        cpa_enabled = bool(UPLOAD_API_URL and UPLOAD_API_TOKEN)

        print("=" * 60)
        print("  ChatGPT 批量自动注册工具 [非交互模式]")
        print(f"  邮箱服务: {MAIL_PROVIDER}")
        if MULTI_MAIL_PROVIDERS and len(MULTI_MAIL_PROVIDERS) > 1:
            print(f"  多邮箱轮询: {', '.join(MULTI_MAIL_PROVIDERS)}")
        if SELECTED_EMAIL_CREDENTIALS:
            print(f"  邮箱凭证数: {len(SELECTED_EMAIL_CREDENTIALS)}")
        if cpa_enabled:
            print(f"  CPA: 已启用，注册前先清理失效号，成功后分批上传")
        print("=" * 60)

        # 检查必要配置
        if SELECTED_EMAIL_CREDENTIALS:
            for credential in SELECTED_EMAIL_CREDENTIALS:
                kind = str(credential.get("kind") or "").strip().lower()
                if kind == "gptmail" and (not credential.get("base_url") or not credential.get("api_key")):
                    print(f"❌ 错误: GPTMail 凭证 {credential.get('name') or ''} 缺少 base_url 或 api_key")
                    sys.exit(1)
                if kind == "duckmail" and not credential.get("api_key"):
                    print(f"❌ 错误: DuckMail 凭证 {credential.get('name') or ''} 缺少 Bearer Token")
                    sys.exit(1)
        else:
            if MAIL_PROVIDER == "gptmail" and (not GPTMAIL_API_BASE or not GPTMAIL_API_KEY):
                print("❌ 错误: GPTMail 需要设置 --gptmail-api-base 和 --gptmail-api-key")
                sys.exit(1)
            elif MAIL_PROVIDER == "duckmail" and not DUCKMAIL_BEARER:
                print("❌ 错误: DuckMail 需要设置 --duckmail-bearer")
                sys.exit(1)

        # 直接运行
        run_batch(
            total_accounts=DEFAULT_TOTAL_ACCOUNTS,
            output_file=DEFAULT_OUTPUT_FILE,
            max_workers=args.concurrency,
            proxy=DEFAULT_PROXY if DEFAULT_PROXY else None,
            cpa_cleanup=cpa_enabled,
            cpa_upload_every_n=CPA_UPLOAD_EVERY_N
        )
        return

    # 交互模式：原有逻辑
    print("=" * 60)
    print("  ChatGPT 批量自动注册工具")
    print(f"  邮箱服务: {MAIL_PROVIDER}")
    print("=" * 60)

    provider = MAIL_PROVIDER
    cfmail_accounts = _current_cfmail_accounts() if provider == "cfmail" else []

    # 检查配置
    if provider == "cfmail":
        if not cfmail_accounts:
            print(f"\n⚠️  警告: mail_provider=cfmail 但未找到可用配置")
            print(f"   配置文件: {_CFMAIL_CONFIG_PATH}")
            print(f"   请参考 zhuce5_cfmail_accounts.json 格式补充配置")
            print("\n   按 Enter 继续尝试运行 (可能会失败)...")
            input()
        else:
            cfmail_names = ", ".join(a.name for a in cfmail_accounts)
            print(f"\n[Info] cfmail 配置已加载: {cfmail_names}")
    elif provider == "duckmail":
        if not DUCKMAIL_BEARER:
            print("\n⚠️  警告: 未设置 DUCKMAIL_BEARER")
            print("   请编辑 config.json 设置 duckmail_bearer，或将 mail_provider 改为 cfmail/tempmail_lol/gptmail/mail_tm/mail_gw")
            print("\n   按 Enter 继续尝试运行 (可能会失败)...")
            input()
    elif provider == "mail_tm":
        print(f"\n[Info] Mail.tm 已启用: {MAIL_TM_API_BASE}")
        print(f"[Info] Mail.tm Domain: {MAIL_TM_DOMAIN or 'auto'}")
    elif provider == "mail_gw":
        print(f"\n[Info] Mail.gw 已启用: {MAIL_GW_API_BASE}")
        print(f"[Info] Mail.gw Domain: {MAIL_GW_DOMAIN or 'auto'}")
    elif provider == "tempmail_lol":
        print(f"\n[Info] TempMail.lol 已启用: {TEMPMAIL_LOL_API_BASE}")
    elif provider == "gptmail":
        if not GPTMAIL_API_BASE or not GPTMAIL_API_KEY:
            print("\n⚠️  警告: 未设置 GPTMAIL_API_BASE 或 GPTMAIL_API_KEY")
            print("   请编辑 config.json 设置 gptmail_api_base 和 gptmail_api_key")
            print("\n   按 Enter 继续尝试运行 (可能会失败)...")
            input()
        else:
            print(f"\n[Info] GPTMail 已启用: {GPTMAIL_API_BASE}")
            print(f"[Info] GPTMail Domain: {GPTMAIL_DOMAIN}")
    else:
        print(f"\n❌ 错误: 不支持的 mail_provider={provider}")
        print("   可选值: duckmail / cfmail / tempmail_lol / gptmail / mail_tm / mail_gw")
        return

    # 代理配置
    proxy = DEFAULT_PROXY
    if proxy:
        print(f"[Info] 检测到默认代理: {proxy}")
        use_default = input("使用此代理? (Y/n): ").strip().lower()
        if use_default == "n":
            proxy = input("输入代理地址 (留空=不使用代理): ").strip() or None
    else:
        env_proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                     or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))
        if env_proxy:
            print(f"[Info] 检测到环境变量代理: {env_proxy}")
            use_env = input("使用此代理? (Y/n): ").strip().lower()
            proxy = None if use_env == "n" else env_proxy
            if use_env == "n":
                proxy = input("输入代理地址 (留空=不使用代理): ").strip() or None
        else:
            proxy = input("输入代理地址 (如 http://127.0.0.1:7890，留空=不使用代理): ").strip() or None

    print(f"[Info] {'使用代理: ' + proxy if proxy else '不使用代理'}")

    # 启动前网络预检（可选）
    preflight_input = input("\n执行启动前连通性预检? (Y/n): ").strip().lower()
    if preflight_input != "n":
        if not _quick_preflight(proxy=proxy, provider=provider):
            print("\n⚠️  预检失败，按 Enter 退出；输入 c 可继续强制运行")
            action = input("继续? (c/Enter): ").strip().lower()
            if action != "c":
                return

    # CPA 清理
    cpa_cleanup = False
    if UPLOAD_API_URL:
        cleanup_input = input("\n注册前清理 CPA 无效号? (Y/n): ").strip().lower()
        cpa_cleanup = cleanup_input != "n"

    # 注册数量
    count_input = input(f"\n注册账号数量 (默认 {DEFAULT_TOTAL_ACCOUNTS}): ").strip()
    total_accounts = (int(count_input) if count_input.isdigit() and int(count_input) > 0
                      else DEFAULT_TOTAL_ACCOUNTS)

    workers_input = input("并发数 (默认 3): ").strip()
    max_workers = int(workers_input) if workers_input.isdigit() and int(workers_input) > 0 else 3

    upload_n_input = input(f"每成功多少个账号触发 CPA 上传 (默认 {CPA_UPLOAD_EVERY_N}): ").strip()
    cpa_upload_every_n = (int(upload_n_input)
                          if upload_n_input.isdigit() and int(upload_n_input) > 0
                          else CPA_UPLOAD_EVERY_N)

    run_batch(total_accounts=total_accounts, output_file=DEFAULT_OUTPUT_FILE,
              max_workers=max_workers, proxy=proxy, cpa_cleanup=cpa_cleanup,
              cpa_upload_every_n=cpa_upload_every_n)

if __name__ == "__main__":
    main()
