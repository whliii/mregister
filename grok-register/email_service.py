import json
import os
import sys
import threading
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.email_adapter import SUPPORTED_EMAIL_KINDS
from shared.email_adapter import UnifiedEmailAdapter
from shared.email_adapter import extract_generic_code
from shared.email_adapter import load_email_credentials_from_file


EMAIL_CREDENTIALS_FILE = (os.getenv("GROK_EMAIL_CREDENTIALS_FILE", "") or "").strip()
EMAIL_CREDENTIAL_MANAGER_URL = (os.getenv("EMAIL_CREDENTIAL_MANAGER_URL", "") or "").strip().rstrip("/")
EMAIL_CREDENTIAL_MANAGER_TOKEN = (os.getenv("EMAIL_CREDENTIAL_MANAGER_TOKEN", "") or "").strip()
EMAIL_CREDENTIAL_MANAGER_PLATFORM = (os.getenv("EMAIL_CREDENTIAL_MANAGER_PLATFORM", "grok-register") or "grok-register").strip()
EMAIL_CREDENTIAL_MANAGER_TASK_ID = (os.getenv("EMAIL_CREDENTIAL_MANAGER_TASK_ID", "") or "").strip()
_selection_lock = threading.Lock()


class EmailService:
    def __init__(self, proxies: Any = None):
        proxy_url = ""
        if isinstance(proxies, dict):
            proxy_url = str(proxies.get("http") or proxies.get("https") or "").strip()
        elif proxies:
            proxy_url = str(proxies).strip()
        self._adapter = UnifiedEmailAdapter(
            proxy=proxy_url,
            impersonate="chrome",
            defaults={
                "mail_tm_api_base": "https://api.mail.tm",
                "mail_gw_api_base": "https://api.mail.gw",
                "tempmail_lol_api_base": "https://api.tempmail.lol/v2",
            },
        )
        self._selected_credentials = load_email_credentials_from_file(EMAIL_CREDENTIALS_FILE) if EMAIL_CREDENTIALS_FILE else []
        self._round_robin_index = 0

    def _email_manager_enabled(self) -> bool:
        return bool(EMAIL_CREDENTIAL_MANAGER_URL and EMAIL_CREDENTIAL_MANAGER_TOKEN)

    def _email_manager_task_id(self) -> int | None:
        return int(EMAIL_CREDENTIAL_MANAGER_TASK_ID) if EMAIL_CREDENTIAL_MANAGER_TASK_ID.isdigit() else None

    def _candidate_credential_ids(self, exclude_ids: set[int] | None = None) -> list[int]:
        blocked = exclude_ids or set()
        ids: list[int] = []
        for item in self._selected_credentials:
            raw_id = item.get("id")
            if isinstance(raw_id, int):
                if raw_id not in blocked:
                    ids.append(raw_id)
            elif str(raw_id or "").strip().isdigit():
                credential_id = int(str(raw_id).strip())
                if credential_id not in blocked:
                    ids.append(credential_id)
        return ids

    def _email_manager_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        data = json.loads(body or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("email manager response must be an object")
        return data

    def _acquire_email_credential(self, worker_index: int, exclude_ids: set[int] | None = None) -> dict[str, Any] | None:
        if self._email_manager_enabled():
            candidate_ids = self._candidate_credential_ids(exclude_ids)
            if candidate_ids:
                try:
                    data = self._email_manager_post(
                        "/acquire",
                        {
                            "platform": EMAIL_CREDENTIAL_MANAGER_PLATFORM,
                            "candidate_ids": candidate_ids,
                            "task_id": self._email_manager_task_id(),
                            "worker_index": worker_index,
                        },
                    )
                    credential = data.get("credential")
                    if isinstance(credential, dict):
                        return dict(credential)
                except Exception as exc:
                    print(f"[email-manager] 获取邮箱凭证失败，回退本地轮询: {exc}")
        if not self._selected_credentials:
            return None
        with _selection_lock:
            total = len(self._selected_credentials)
            for offset in range(total):
                selected = dict(self._selected_credentials[(self._round_robin_index + offset) % total])
                raw_id = selected.get("id")
                selected_id = raw_id if isinstance(raw_id, int) else int(str(raw_id).strip()) if str(raw_id or "").strip().isdigit() else None
                if selected_id is not None and exclude_ids and selected_id in exclude_ids:
                    continue
                self._round_robin_index = (self._round_robin_index + offset + 1) % total
                return selected
        return None

    def _report_credential_event(
        self,
        credential: dict[str, Any] | None,
        event: str,
        *,
        reason: str = "",
        worker_index: int | None = None,
    ) -> None:
        if not credential or not self._email_manager_enabled():
            return
        raw_id = credential.get("id")
        if isinstance(raw_id, int):
            credential_id = raw_id
        elif str(raw_id or "").strip().isdigit():
            credential_id = int(str(raw_id).strip())
        else:
            return
        try:
            self._email_manager_post(
                "/report",
                {
                    "platform": EMAIL_CREDENTIAL_MANAGER_PLATFORM,
                    "credential_id": credential_id,
                    "event": event,
                    "reason": reason or None,
                    "task_id": self._email_manager_task_id(),
                    "worker_index": worker_index,
                },
            )
        except Exception as exc:
            print(f"[email-manager] 上报邮箱凭证事件失败({event}): {exc}")

    def acquire_mailbox(self, worker_index: int = 1) -> dict[str, Any] | None:
        tried_ids: set[int] = set()
        while True:
            credential = self._acquire_email_credential(worker_index, tried_ids)
            if not credential:
                return None
            raw_id = credential.get("id")
            credential_id = raw_id if isinstance(raw_id, int) else int(str(raw_id).strip()) if str(raw_id or "").strip().isdigit() else None
            if credential_id is not None:
                tried_ids.add(credential_id)
            provider = str(credential.get("kind") or "").strip().lower()
            if provider not in SUPPORTED_EMAIL_KINDS:
                self._report_credential_event(credential, "failure", reason=f"unsupported_provider:{provider}", worker_index=worker_index)
                print(f"[-] 不支持的邮箱 provider: {provider}")
                continue
            try:
                self._adapter.apply_email_credential(credential)
                email, _, mail_token = self._adapter.create_email_mailbox(provider)
            except Exception as exc:
                self._report_credential_event(credential, "failure", reason=f"mailbox_create_failed: {exc}", worker_index=worker_index)
                print(f"[-] 建箱失败 ({provider}): {exc}")
                continue
            self._report_credential_event(credential, "mailbox_created", worker_index=worker_index)
            return {
                "provider": provider,
                "credential": credential,
                "email": email,
                "mail_token": mail_token,
            }

    def wait_for_verification_code(self, mailbox: dict[str, Any], timeout: int = 120) -> str | None:
        provider = str(mailbox.get("provider") or "").strip().lower()
        email = str(mailbox.get("email") or "")
        mail_token = str(mailbox.get("mail_token") or "")
        return self._adapter.wait_for_code(
            provider=provider,
            mail_token=mail_token,
            email=email,
            timeout=timeout,
            extractor=extract_generic_code,
        )

    def report_failure(self, mailbox: dict[str, Any] | None, reason: str, worker_index: int | None = None) -> None:
        credential = mailbox.get("credential") if isinstance(mailbox, dict) else None
        self._report_credential_event(credential, "failure", reason=reason[:500], worker_index=worker_index)

    def report_otp_received(self, mailbox: dict[str, Any] | None, worker_index: int | None = None) -> None:
        credential = mailbox.get("credential") if isinstance(mailbox, dict) else None
        self._report_credential_event(credential, "otp_received", worker_index=worker_index)

    def report_account_success(self, mailbox: dict[str, Any] | None, worker_index: int | None = None) -> None:
        credential = mailbox.get("credential") if isinstance(mailbox, dict) else None
        self._report_credential_event(credential, "account_created", worker_index=worker_index)
        self._report_credential_event(credential, "task_success", worker_index=worker_index)
