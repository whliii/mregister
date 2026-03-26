from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from web_console.subscription_parser import parse_subscription, ProxyNode, get_country_name
from web_console.proxy_converter import probe_proxy_url, resolve_node_proxy_url, stop_all_proxies, stop_proxy_for_url


ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = WEB_DIR / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
DB_PATH = Path(os.getenv("WEB_CONSOLE_DB_PATH", str(RUNTIME_DIR / "app.db"))).expanduser()
SESSION_COOKIE = "register_console_session"
SESSION_TTL_HOURS = max(1, int(os.getenv("WEB_CONSOLE_SESSION_TTL_HOURS", "24")))
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("WEB_CONSOLE_MAX_CONCURRENT_TASKS", "2")))
POLL_INTERVAL_SECONDS = max(1.0, float(os.getenv("WEB_CONSOLE_POLL_INTERVAL", "2.0")))
SQLITE_JOURNAL_MODE = (os.getenv("WEB_CONSOLE_SQLITE_JOURNAL_MODE", "OFF") or "OFF").upper()
SQLITE_SYNCHRONOUS = (os.getenv("WEB_CONSOLE_SQLITE_SYNCHRONOUS", "NORMAL") or "NORMAL").upper()
PROXY_PROBE_TIMEOUT_SECONDS = max(3.0, float(os.getenv("WEB_CONSOLE_PROXY_PROBE_TIMEOUT_SECONDS", "10.0")))
PROXY_FAILURE_COOLDOWN_SECONDS = max(60, int(os.getenv("WEB_CONSOLE_PROXY_FAILURE_COOLDOWN_SECONDS", "7200")))
INTERNAL_MANAGER_BASE_URL = (os.getenv("WEB_CONSOLE_INTERNAL_BASE_URL", "http://127.0.0.1:8000") or "http://127.0.0.1:8000").rstrip("/")
EMAIL_DISPATCH_MIN_WEIGHT = min(100.0, max(1.0, float(os.getenv("WEB_CONSOLE_EMAIL_DISPATCH_MIN_WEIGHT", "10"))))
EMAIL_DISPATCH_MAX_WEIGHT = min(100.0, max(EMAIL_DISPATCH_MIN_WEIGHT, float(os.getenv("WEB_CONSOLE_EMAIL_DISPATCH_MAX_WEIGHT", "100"))))
EMAIL_DISPATCH_MIN_SCORE = 0.05
EMAIL_DISPATCH_MAX_SCORE = 0.98
EMAIL_DISPATCH_DEFAULT_SCORE = min(
    EMAIL_DISPATCH_MAX_SCORE,
    max(EMAIL_DISPATCH_MIN_SCORE, float(os.getenv("WEB_CONSOLE_EMAIL_DISPATCH_DEFAULT_SCORE", "0.65"))),
)
EMAIL_DISPATCH_WEIGHT_POWER = max(1.0, float(os.getenv("WEB_CONSOLE_EMAIL_DISPATCH_WEIGHT_POWER", "1.6")))
EMAIL_DISPATCH_SUCCESS_TARGETS = {
    "mailbox_created": 0.72,
    "otp_received": 0.88,
    "account_created": 0.94,
    "oauth_success": 0.97,
    "task_success": 0.98,
}
EMAIL_DISPATCH_SUCCESS_BLEND = {
    "mailbox_created": 0.18,
    "otp_received": 0.24,
    "account_created": 0.18,
    "oauth_success": 0.14,
    "task_success": 0.10,
}
EMAIL_DISPATCH_FAILURE_POLICIES = {
    "mailbox_provider": {"target": 0.12, "blend": 0.30, "counts_against_credential": True},
    "otp_delivery": {"target": 0.18, "blend": 0.26, "counts_against_credential": True},
    "upstream_blocked": {"target": 0.60, "blend": 0.03, "counts_against_credential": False},
    "oauth_flow": {"target": 0.62, "blend": 0.04, "counts_against_credential": False},
    "network": {"target": 0.58, "blend": 0.04, "counts_against_credential": False},
    "unknown": {"target": 0.50, "blend": 0.08, "counts_against_credential": False},
}


def _clamp_email_dispatch_score(value: Any) -> float:
    try:
        numeric = float(value)
    except Exception:
        numeric = EMAIL_DISPATCH_DEFAULT_SCORE
    return min(EMAIL_DISPATCH_MAX_SCORE, max(EMAIL_DISPATCH_MIN_SCORE, numeric))


def _email_dispatch_score_to_weight(score: float) -> float:
    clamped = _clamp_email_dispatch_score(score)
    scaled = clamped ** EMAIL_DISPATCH_WEIGHT_POWER
    return min(
        EMAIL_DISPATCH_MAX_WEIGHT,
        max(EMAIL_DISPATCH_MIN_WEIGHT, EMAIL_DISPATCH_MIN_WEIGHT + (EMAIL_DISPATCH_MAX_WEIGHT - EMAIL_DISPATCH_MIN_WEIGHT) * scaled),
    )


def _email_dispatch_blend(current_score: Any, target_score: float, blend: float) -> float:
    current = _clamp_email_dispatch_score(current_score)
    effective_blend = min(0.95, max(0.01, float(blend or 0.0)))
    target = _clamp_email_dispatch_score(target_score)
    return _clamp_email_dispatch_score(current * (1.0 - effective_blend) + target * effective_blend)


def _email_dispatch_ratio(numerator: Any, denominator: Any) -> float:
    try:
        den = float(denominator or 0)
    except Exception:
        den = 0.0
    if den <= 0:
        return 0.0
    try:
        num = float(numerator or 0)
    except Exception:
        num = 0.0
    return min(1.0, max(0.0, num / den))


def _estimate_email_dispatch_score(row: sqlite3.Row | dict[str, Any]) -> float:
    dispatch_count = int(row["dispatch_count"] or 0)
    if dispatch_count <= 0:
        return EMAIL_DISPATCH_DEFAULT_SCORE

    mailbox_success_count = int(row["mailbox_success_count"] or 0)
    otp_success_count = int(row["otp_success_count"] or 0)
    account_success_count = int(row["account_success_count"] or 0)
    oauth_success_count = int(row["oauth_success_count"] or 0)
    final_success_count = int(row["final_success_count"] or 0)

    mailbox_rate = _email_dispatch_ratio(mailbox_success_count, dispatch_count)
    if otp_success_count <= 0 and account_success_count <= 0 and oauth_success_count <= 0 and final_success_count <= 0:
        otp_rate = mailbox_rate
    else:
        otp_rate = _email_dispatch_ratio(otp_success_count, mailbox_success_count or dispatch_count)
    final_rate = _email_dispatch_ratio(final_success_count, dispatch_count)

    success_signal = 0.50 * mailbox_rate + 0.35 * otp_rate + 0.15 * final_rate
    return _clamp_email_dispatch_score(0.20 + 0.75 * success_signal)


def _classify_email_dispatch_failure(reason: str) -> str:
    text = (reason or "").strip().lower()
    if not text:
        return "unknown"

    if (
        "mailbox_create_failed" in text
        or "建箱失败" in text
        or "创建邮箱失败" in text
        or "返回数据不完整" in text
        or ("429" in text and ("mail" in text or "tempmail" in text or "gptmail" in text or "rate limited" in text))
        or "rate limited" in text
    ):
        return "mailbox_provider"

    if (
        "otp_missing" in text
        or "未能获取验证码" in text
        or "重试后仍未获取验证码" in text
        or "收不到验证码" in text
        or "verification code" in text and "timeout" in text
    ):
        return "otp_delivery"

    if (
        "blocked_403" in text
        or "csrf" in text
        or "registration_disallowed" in text
        or "disallowed" in text
        or "unsupported_email" in text
    ):
        return "upstream_blocked"

    if "oauth_failed" in text or "invalid_auth_step" in text:
        return "oauth_flow"

    if (
        "tls connect error" in text
        or "proxy" in text
        or "connection" in text
        or "timed out" in text
        or "timeout" in text
        or "temporarily unavailable" in text
    ):
        return "network"

    return "unknown"


EMAIL_DISPATCH_DEFAULT_WEIGHT = _email_dispatch_score_to_weight(EMAIL_DISPATCH_DEFAULT_SCORE)

PLATFORMS = {
    "openai-register": {
        "label": "OpenAI Register",
        "requires_email_credential": False,
        "requires_captcha_credential": False,
        "supports_proxy": True,
        "default_concurrency": 3,
        "supports_multiple_email_credentials": True,
        "optional_cpa_credential": True,
        "notes": "Uses unified email credentials and optional CPA upload.",
    },
    "grok-register": {
        "label": "Grok Register",
        "requires_email_credential": True,
        "requires_captcha_credential": True,
        "supports_proxy": True,
        "default_concurrency": 4,
        "supports_multiple_email_credentials": True,
        "notes": "Uses YesCaptcha and unified email credentials. The worker feeds the original CLI with a concurrency value via stdin.",
    },
}

EMAIL_CREDENTIAL_KINDS = {"gptmail", "duckmail", "tempmail_lol", "cfmail", "mail_tm", "mail_gw"}

UI_TRANSLATIONS = {
    "zh-CN": {
        "site_title": "MREGISTER",
        "request_failed": "请求失败",
        "brand_console": "Register Console",
        "brand_name": "MREGISTER",
        "topbar_workspace": "工作区",
        "auth_setup_title": "首次打开先设置管理员密码",
        "auth_setup_desc": "密码会保存为本地哈希值。未设置密码前，任务、凭据、代理和 API 都不会开放。",
        "auth_login_title": "输入管理员密码进入控制台",
        "auth_login_desc": "当前站点已经启用密码保护，登录后才可查看任务、下载压缩包和操作 API Key。",
        "auth_password": "管理员密码",
        "auth_setup_submit": "保存并进入后台",
        "auth_login_submit": "登录",
        "nav_dashboard": "首页",
        "nav_credentials": "凭据",
        "nav_proxies": "代理",
        "nav_create_task": "新建模板",
        "nav_template_center": "模板中心",
        "nav_task_detail": "任务清单",
        "nav_api_keys": "API 接口",
        "nav_docs": "文档",
        "nav_logout": "退出登录",
        "toggle_sidebar": "收起或展开侧边栏",
        "open_sidebar": "打开侧边栏",
        "close_sidebar": "关闭侧边栏",
        "section_overview": "总览与默认配置",
        "panel_defaults_title": "默认设置",
        "panel_defaults_desc": "API 创建任务时会优先使用这里的默认凭据和默认代理。",
        "default_gptmail": "默认 GPTMail",
        "default_yescaptcha": "默认 YesCaptcha",
        "default_proxy": "默认代理",
        "save_defaults": "保存默认设置",
        "panel_recent_tasks_title": "最近任务",
        "panel_recent_tasks_desc": "点任意任务可直接跳到详情页查看控制台输出。",
        "section_credentials": "凭据管理",
        "credentials_create_title": "新增凭据",
        "credentials_create_desc": "统一管理邮箱、验证码和 CPA 凭证；邮箱凭证可在任务里多选。",
        "credentials_saved_title": "已保存凭据",
        "credentials_saved_desc": "支持删除、查看备注；GPTMail 和 YesCaptcha 可设为默认。",
        "credential_dispatch_none": "暂无调度统计",
        "credential_dispatch_platform": "平台",
        "credential_dispatch_weight": "权重",
        "credential_dispatch_dispatches": "派发",
        "credential_dispatch_mailbox": "建箱",
        "credential_dispatch_otp": "OTP",
        "credential_dispatch_account": "注册成功",
        "credential_dispatch_oauth": "OAuth",
        "credential_dispatch_final": "最终成功",
        "credential_dispatch_failures": "失败",
        "credential_dispatch_last_error": "最近错误: {reason}",
        "credential_dispatch_reset": "重置邮箱统计",
        "credential_dispatch_reset_confirm": "重置凭据 {name} 的邮箱调度统计？",
        "dispatch_center_title": "邮箱调度中心",
        "dispatch_center_desc": "按平台观察每个邮箱凭据的质量分、调度权重、建箱稳定性和异常归因。这里展示的是运行态，不是凭据配置本身。",
        "dispatch_center_empty": "当前还没有邮箱调度数据。",
        "dispatch_center_issue": "当前归因",
        "dispatch_center_last_event": "最近事件",
        "dispatch_center_platform_count": "{count} 个平台视图",
        "dispatch_metric_credentials": "邮箱凭据",
        "dispatch_metric_credentials_desc": "已纳入统一调度的邮箱资产数量。",
        "dispatch_metric_platforms": "平台视图",
        "dispatch_metric_platforms_desc": "当前平均质量分 {value}，最高凭据 {name}。",
        "dispatch_metric_platforms_desc_empty": "等待首批调度数据进入后展示。",
        "dispatch_metric_healthy": "稳定条目",
        "dispatch_metric_healthy_desc": "质量稳定，且近期没有邮箱侧硬故障的平台视图。",
        "dispatch_metric_risk": "风险条目",
        "dispatch_metric_risk_desc": "近期存在建箱失败、收码异常或质量分过低的平台视图。",
        "dispatch_quality_score_label": "质量分",
        "dispatch_status_excellent": "优秀",
        "dispatch_status_stable": "稳定",
        "dispatch_status_watch": "观察",
        "dispatch_status_risk": "风险",
        "dispatch_failure_healthy": "运行正常",
        "dispatch_failure_mailbox_provider": "邮箱接口异常",
        "dispatch_failure_otp_delivery": "收码不稳定",
        "dispatch_failure_upstream_blocked": "目标站风控",
        "dispatch_failure_oauth_flow": "OAuth 流程异常",
        "dispatch_failure_network": "网络或代理异常",
        "dispatch_failure_unknown": "待归因",
        "field_name": "名称",
        "field_kind": "类型",
        "field_api_key": "API Key",
        "field_base_url": "Base URL",
        "field_prefix": "邮箱前缀",
        "field_domain": "邮箱域名",
        "field_notes": "备注",
        "save_credential": "保存凭据",
        "section_proxies": "代理管理",
        "proxies_create_title": "新增代理",
        "proxies_create_desc": "支持保存多个代理，并可指定为站点默认代理。",
        "proxies_saved_title": "已保存代理",
        "proxies_saved_desc": "任务可选择默认代理、指定代理或不使用代理。",
        "field_proxy_url": "代理地址",
        "save_proxy": "保存代理",
        "section_tasks": "新建任务模板",
        "section_templates": "任务模板中心",
        "field_task_name": "任务名称",
        "field_platform": "平台",
        "field_quantity": "目标数量",
        "field_concurrency": "并发数",
        "field_email_credential": "邮件凭据",
        "field_captcha_credential": "验证码凭据",
        "field_proxy_mode": "代理模式",
        "field_proxy_select": "指定代理",
        "proxy_mode_none": "不使用代理",
        "proxy_mode_default": "使用默认代理",
        "proxy_mode_custom": "指定代理",
        "proxy_mode_rotate": "轮换代理",
        "save_task": "保存到模板中心",
        "created_template_confirm": "模板 #{id} 已保存。点击“确定”前往模板中心，点击“取消”留在当前页面继续创建。",
        "section_task_detail": "任务清单",
        "task_list_title": "任务清单",
        "task_list_desc": "这里只显示已经入队的任务实例。",
        "template_list_title": "模板列表",
        "template_list_desc": "每次点击“加入队列”都会生成一个新的任务实例并进入任务清单。",
        "task_filter_status": "状态筛选",
        "task_filter_all": "全部状态",
        "console_title": "实时控制台",
        "enqueue_template": "加入队列",
        "delete_template": "删除模板",
        "empty_templates": "暂无模板",
        "template_detail_empty_title": "暂无模板",
        "template_detail_empty_desc": "先创建一个任务模板。",
        "delete_template_confirm": "删除模板 {name}？",
        "template_header_meta": "{platform} | 目标数量 {quantity} | 并发 {concurrency} | 已入队 {queue_count} 次",
        "template_email_credentials": "邮箱凭据",
        "template_captcha_credential": "验证码凭据",
        "template_cpa_credential": "CPA 上传",
        "template_proxy_mode": "代理模式",
        "template_proxy_none": "不使用代理",
        "template_proxy_default": "使用默认代理",
        "template_proxy_custom": "指定代理",
        "template_proxy_rotate": "轮换代理",
        "template_proxy_target": "代理目标",
        "template_last_queued": "最近入队 {value}",
        "template_never_queued": "尚未入队",
        "template_cpa_disabled": "不上传",
        "template_not_set": "未设置",
        "section_api": "API 接口",
        "api_create_title": "创建 API Key",
        "api_create_desc": "新建成功后只会显示一次，请立即保存。",
        "api_saved_title": "已有 API Key",
        "api_saved_desc": "可用于外部程序调用创建任务、查询状态和下载结果。",
        "save_api_key": "生成 API Key",
        "section_docs": "接口文档",
        "docs_intro_title": "接入说明",
        "docs_intro_desc": "外部接口分为两组：`/api/external/*` 适合快速创建和查询任务，`/api/v1/*` 适合做模板管理、任务轮询、控制台日志和制品下载。API 创建的任务 `source` 为 `api`，默认会在创建后 24 小时自动删除。",
        "docs_auth_title": "认证方式",
        "docs_auth_desc": "推荐使用请求头 `Authorization: Bearer YOUR_API_KEY`。同时兼容 `X-API-Key: YOUR_API_KEY`，也兼容查询参数 `?api_key=...`，但查询参数只建议临时调试时使用。",
        "docs_endpoints_title": "快速任务 API",
        "docs_v1_endpoints_title": "完整 v1 API",
        "docs_create_params_title": "POST /api/external/tasks 参数",
        "docs_create_example_title": "快速创建示例",
        "docs_query_example_title": "状态查询示例",
        "docs_download_example_title": "v1 示例",
        "docs_response_title": "返回说明",
        "docs_response_desc": "`completed_count` 是当前真实成功数量，不是尝试次数。`download_url` 会在任务进入终态后返回，包括 `completed`、`partial`、`failed`、`stopped`、`interrupted`。`runtime.progress` 和 `runtime.artifacts` 来自当前任务运行目录与控制台解析结果。",
        "table_method": "方法",
        "table_path": "路径",
        "table_desc": "说明",
        "table_field": "字段",
        "table_type": "类型",
        "table_required": "必填",
        "endpoint_create_desc": "直接创建一个或多个 API 任务，或根据模板批量入队",
        "endpoint_query_desc": "查询 API 任务状态、完成数、可用操作和运行时信息",
        "endpoint_download_desc": "下载 API 任务结果压缩包",
        "required_yes": "是",
        "required_no": "否",
        "required_conditional": "条件必填",
        "endpoint_v1_templates_list_desc": "列出所有任务模板",
        "endpoint_v1_templates_create_desc": "创建任务模板",
        "endpoint_v1_template_detail_desc": "查看单个模板详情",
        "endpoint_v1_template_update_desc": "更新模板配置",
        "endpoint_v1_template_enqueue_desc": "按模板创建一个或多个任务实例",
        "endpoint_v1_tasks_list_desc": "按状态、来源、模板筛选任务列表",
        "endpoint_v1_task_detail_desc": "查看任务详情与当前运行时快照",
        "endpoint_v1_task_console_desc": "增量拉取任务控制台输出",
        "endpoint_v1_task_artifacts_desc": "查看任务输出文件摘要",
        "endpoint_v1_task_retry_desc": "重试已结束任务",
        "endpoint_v1_task_stop_desc": "停止运行中的任务",
        "endpoint_v1_task_delete_desc": "删除已停止或已结束任务",
        "endpoint_v1_task_download_desc": "下载任务目录压缩包",
        "param_template_id_desc": "模板 ID。传入后按模板入队，此时 `platform` 和 `quantity` 不再必填",
        "param_platform_desc": "平台名称，目前支持 `openai-register` 和 `grok-register`",
        "param_quantity_desc": "目标成功数量，系统按真实成功数判断完成，不按尝试次数计算",
        "param_count_desc": "创建多少个任务实例，默认 `1`，最大 `100`",
        "param_use_proxy_desc": "旧兼容字段。仅在未传 `proxy_mode` 时生效；`true` 映射为 `default`，否则为 `none`",
        "param_proxy_mode_desc": "代理模式，可选 `none`、`default`、`specific`",
        "param_proxy_id_desc": "指定代理 ID。通常配合 `proxy_mode=specific` 使用",
        "param_concurrency_desc": "并发数，取值 `1-64`；不传时使用平台默认值或模板默认值",
        "param_name_desc": "自定义任务名，不传则由系统自动生成",
        "param_captcha_desc": "验证码凭据 ID。`grok-register` 这类平台通常需要",
        "param_email_credentials_desc": "邮箱凭据 ID 数组。为空时尝试走站点默认配置；更稳妥的做法是显式传入",
        "param_cpa_desc": "可选的 CPA 凭据 ID，用于成功后上传结果",
        "dashboard_running_tasks": "运行中任务",
        "dashboard_completed_tasks": "已完成任务",
        "dashboard_credential_count": "凭据数量",
        "dashboard_proxy_count": "代理数量",
        "empty_tasks": "暂无任务",
        "empty_credentials": "暂无凭据",
        "empty_proxies": "暂无代理",
        "empty_filtered_tasks": "当前筛选下没有任务",
        "empty_api_keys": "暂无 API Key",
        "default_badge": "默认",
        "created_at": "创建于 {value}",
        "last_used_at": "最近使用时间 {value}",
        "unused": "暂未使用",
        "use_default_gptmail": "使用默认 GPTMail",
        "use_default_yescaptcha": "使用默认 YesCaptcha",
        "choose_proxy": "选择一个代理",
        "no_default_gptmail": "不设置默认 GPTMail",
        "no_default_yescaptcha": "不设置默认 YesCaptcha",
        "no_default_proxy": "不使用默认代理",
        "current_default": "当前默认",
        "set_default": "设为默认",
        "delete": "删除",
        "enable": "启用",
        "disable": "停用",
        "stop_task": "停止任务",
        "download_zip": "下载压缩包",
        "delete_task": "删除任务",
        "save_now": "新建成功，请立即保存",
        "status_queued": "排队中",
        "status_running": "运行中",
        "status_stopping": "停止中",
        "status_completed": "已完成",
        "status_partial": "部分完成",
        "status_failed": "失败",
        "status_stopped": "已停止",
        "status_interrupted": "已中断",
        "task_detail_empty_title": "当前筛选下没有任务",
        "task_detail_empty_desc": "调整左侧状态筛选，或先创建新的任务。",
        "console_wait": "等待选择任务后显示实时控制台输出。",
        "console_empty": "当前还没有控制台输出。",
        "task_header_meta": "{platform} | 目标数量 {quantity} | 执行数量 {executed} | 成功数量 {completed} | 当前状态 {status}",
        "created_task_confirm": "任务 #{id} 已创建。点击“确定”前往任务详情，点击“取消”留在当前页面继续创建。",
        "delete_task_confirm": "删除任务 #{id}？",
        "delete_credential_confirm": "删除凭据 {name}？",
        "delete_proxy_confirm": "删除代理 {name}？",
        "delete_api_key_confirm": "删除这个 API Key？",
        "api_key_meta": "{prefix}... | 创建于 {created_at}",
        "test_proxy": "测试延时",
        "proxy_test_ok": "延时 {latency} ms | 出口 {ip}",
        "proxy_test_fail": "测试失败",
        "proxy_cooldown_until": "冷却至 {time}",
        "proxy_last_probe_success": "最近测试成功 {time}",
        "proxy_last_probe_fail": "最近测试失败 {time}",
        "proxy_not_tested": "尚未测试",
        "proxy_local_snapshot": "已保存本地快照",
        "proxy_snapshot_source": "快照来源 {name} | {protocol} | {server}:{port}",
        "proxy_status_success": "状态 正常",
        "proxy_status_failed": "状态 失败",
        "proxy_status_cooling": "状态 冷却中",
        # 订阅管理
        "proxy_tab_external": "外部代理",
        "proxy_tab_subscription": "订阅节点",
        "subscription_add_title": "添加订阅",
        "subscription_add_desc": "输入订阅链接，自动解析代理节点。",
        "field_subscription_url": "订阅链接",
        "add_subscription": "添加订阅",
        "subscriptions_title": "订阅列表",
        "proxy_nodes_title": "代理节点",
        "proxy_nodes_desc": "从订阅解析的节点，选择一个作为代理。",
        "refresh_subscription": "刷新",
        "delete_subscription_confirm": "删除订阅 {name} 及其所有节点？",
        "empty_subscriptions": "暂无订阅",
        "empty_proxy_nodes": "暂无代理节点",
        "node_protocol": "协议",
        "node_country": "地区",
        "node_use": "使用此节点",
        "node_test_ok": "延时 {latency} ms | 出口 {ip}",
        "node_test_fail": "测试失败",
        "node_latency_unknown": "未测试",
        "subscription_nodes": "{count} 个节点",
        "subscription_last_refresh": "最后刷新: {time}",
    },
    "en": {
        "site_title": "MREGISTER",
        "request_failed": "Request failed",
        "brand_console": "Register Console",
        "brand_name": "MREGISTER",
        "topbar_workspace": "Workspace",
        "auth_setup_title": "Set the admin password on first visit",
        "auth_setup_desc": "The password is stored as a local hash. Tasks, credentials, proxies, and API access stay locked until it is configured.",
        "auth_login_title": "Enter the admin password",
        "auth_login_desc": "This site is password protected. Sign in before viewing tasks, downloading archives, or managing API keys.",
        "auth_password": "Admin password",
        "auth_setup_submit": "Save and enter console",
        "auth_login_submit": "Sign in",
        "nav_dashboard": "Dashboard",
        "nav_credentials": "Credentials",
        "nav_proxies": "Proxies",
        "nav_create_task": "New Template",
        "nav_template_center": "Templates",
        "nav_task_detail": "Task Queue",
        "nav_api_keys": "API",
        "nav_docs": "Docs",
        "nav_logout": "Sign out",
        "toggle_sidebar": "Collapse or expand sidebar",
        "open_sidebar": "Open sidebar",
        "close_sidebar": "Close sidebar",
        "section_overview": "Overview and Defaults",
        "panel_defaults_title": "Default settings",
        "panel_defaults_desc": "API-created tasks will use these default credentials and proxy settings first.",
        "default_gptmail": "Default GPTMail",
        "default_yescaptcha": "Default YesCaptcha",
        "default_proxy": "Default proxy",
        "save_defaults": "Save defaults",
        "panel_recent_tasks_title": "Recent tasks",
        "panel_recent_tasks_desc": "Click any task to jump straight into the detail view and console output.",
        "section_credentials": "Credential Management",
        "credentials_create_title": "Add credential",
        "credentials_create_desc": "Manage email, captcha, and CPA credentials in one place. Email credentials can be selected multiple times per task.",
        "credentials_saved_title": "Saved credentials",
        "credentials_saved_desc": "Delete and review notes here. GPTMail and YesCaptcha credentials can also be set as defaults.",
        "credential_dispatch_none": "No dispatch stats yet",
        "credential_dispatch_platform": "Platform",
        "credential_dispatch_weight": "Weight",
        "credential_dispatch_dispatches": "Dispatches",
        "credential_dispatch_mailbox": "Mailbox",
        "credential_dispatch_otp": "OTP",
        "credential_dispatch_account": "Account",
        "credential_dispatch_oauth": "OAuth",
        "credential_dispatch_final": "Final success",
        "credential_dispatch_failures": "Failures",
        "credential_dispatch_last_error": "Last error: {reason}",
        "credential_dispatch_reset": "Reset email stats",
        "credential_dispatch_reset_confirm": "Reset email dispatch stats for {name}?",
        "dispatch_center_title": "Email Dispatch Center",
        "dispatch_center_desc": "Track quality score, dispatch weight, mailbox stability, and failure attribution for each email credential by platform. This is runtime quality, not raw credential config.",
        "dispatch_center_empty": "No email dispatch data yet.",
        "dispatch_center_issue": "Attribution",
        "dispatch_center_last_event": "Last event",
        "dispatch_center_platform_count": "{count} platform views",
        "dispatch_metric_credentials": "Email credentials",
        "dispatch_metric_credentials_desc": "Credentials currently managed by the unified email dispatch layer.",
        "dispatch_metric_platforms": "Platform views",
        "dispatch_metric_platforms_desc": "Average quality score {value}, current leader {name}.",
        "dispatch_metric_platforms_desc_empty": "This will populate after the first dispatch cycle.",
        "dispatch_metric_healthy": "Healthy views",
        "dispatch_metric_healthy_desc": "Stable platform views without recent mailbox-side hard failures.",
        "dispatch_metric_risk": "Risk views",
        "dispatch_metric_risk_desc": "Platform views with recent mailbox failures, OTP issues, or low quality score.",
        "dispatch_quality_score_label": "Quality",
        "dispatch_status_excellent": "Excellent",
        "dispatch_status_stable": "Stable",
        "dispatch_status_watch": "Watch",
        "dispatch_status_risk": "Risk",
        "dispatch_failure_healthy": "Healthy",
        "dispatch_failure_mailbox_provider": "Mailbox provider issue",
        "dispatch_failure_otp_delivery": "OTP delivery issue",
        "dispatch_failure_upstream_blocked": "Upstream blocking",
        "dispatch_failure_oauth_flow": "OAuth flow issue",
        "dispatch_failure_network": "Network or proxy issue",
        "dispatch_failure_unknown": "Needs review",
        "field_name": "Name",
        "field_kind": "Type",
        "field_api_key": "API Key",
        "field_base_url": "Base URL",
        "field_prefix": "Email prefix",
        "field_domain": "Email domain",
        "field_notes": "Notes",
        "save_credential": "Save credential",
        "section_proxies": "Proxy Management",
        "proxies_create_title": "Add proxy",
        "proxies_create_desc": "Save multiple proxies and promote one as the site-wide default.",
        "proxies_saved_title": "Saved proxies",
        "proxies_saved_desc": "Tasks can use the default proxy, a specific proxy, or no proxy at all.",
        "field_proxy_url": "Proxy URL",
        "save_proxy": "Save proxy",
        "section_tasks": "Create Task Template",
        "section_templates": "Task Template Center",
        "field_task_name": "Task name",
        "field_platform": "Platform",
        "field_quantity": "Target quantity",
        "field_concurrency": "Concurrency",
        "field_email_credential": "Email credential",
        "field_captcha_credential": "Captcha credential",
        "field_proxy_mode": "Proxy mode",
        "field_proxy_select": "Specific proxy",
        "proxy_mode_none": "No proxy",
        "proxy_mode_default": "Use default proxy",
        "proxy_mode_custom": "Use selected proxy",
        "proxy_mode_rotate": "Rotate proxy",
        "save_task": "Save to template center",
        "created_template_confirm": "Template #{id} was saved. Click OK to open the template center, or Cancel to stay here and create another one.",
        "section_task_detail": "Task Queue",
        "task_list_title": "Task queue",
        "task_list_desc": "Only queued task instances are shown here.",
        "template_list_title": "Template list",
        "template_list_desc": "Each click on \"Enqueue\" creates a new task instance and sends it to the task queue.",
        "task_filter_status": "Status filter",
        "task_filter_all": "All statuses",
        "console_title": "Live console",
        "enqueue_template": "Enqueue",
        "delete_template": "Delete template",
        "empty_templates": "No templates yet",
        "template_detail_empty_title": "No templates yet",
        "template_detail_empty_desc": "Create a task template first.",
        "delete_template_confirm": "Delete template {name}?",
        "template_header_meta": "{platform} | Target {quantity} | Concurrency {concurrency} | Enqueued {queue_count} times",
        "template_email_credentials": "Email credentials",
        "template_captcha_credential": "Captcha credential",
        "template_cpa_credential": "CPA upload",
        "template_proxy_mode": "Proxy mode",
        "template_proxy_none": "No proxy",
        "template_proxy_default": "Use default proxy",
        "template_proxy_custom": "Use selected proxy",
        "template_proxy_rotate": "Rotate proxy",
        "template_proxy_target": "Proxy target",
        "template_last_queued": "Last enqueued {value}",
        "template_never_queued": "Never enqueued",
        "template_cpa_disabled": "Disabled",
        "template_not_set": "Not set",
        "section_api": "API",
        "api_create_title": "Create API key",
        "api_create_desc": "A new key is only shown once. Save it immediately.",
        "api_saved_title": "Existing API keys",
        "api_saved_desc": "Use these keys from external services to create tasks, query status, and download results.",
        "save_api_key": "Generate API key",
        "section_docs": "API Docs",
        "docs_intro_title": "Overview",
        "docs_intro_desc": "There are two API groups: `/api/external/*` for quick task creation and polling, and `/api/v1/*` for template management, task lifecycle, console streaming, and artifact downloads. Tasks created through the API use `source=api` and are automatically removed 24 hours after creation.",
        "docs_auth_title": "Authentication",
        "docs_auth_desc": "Use `Authorization: Bearer YOUR_API_KEY` by default. `X-API-Key: YOUR_API_KEY` is also accepted, and `?api_key=...` works for temporary debugging only.",
        "docs_endpoints_title": "Quick Task API",
        "docs_v1_endpoints_title": "Full v1 API",
        "docs_create_params_title": "POST /api/external/tasks parameters",
        "docs_create_example_title": "Quick create examples",
        "docs_query_example_title": "Status query example",
        "docs_download_example_title": "v1 examples",
        "docs_response_title": "Response notes",
        "docs_response_desc": "`completed_count` is the real number of successful results, not the attempt count. `download_url` is returned once the task reaches a terminal state, including `completed`, `partial`, `failed`, `stopped`, and `interrupted`. `runtime.progress` and `runtime.artifacts` are derived from the task directory and parsed console output.",
        "table_method": "Method",
        "table_path": "Path",
        "table_desc": "Description",
        "table_field": "Field",
        "table_type": "Type",
        "table_required": "Required",
        "endpoint_create_desc": "Create one or more API tasks directly, or enqueue them from a template",
        "endpoint_query_desc": "Query API task status, completed count, available actions, and runtime data",
        "endpoint_download_desc": "Download the API task archive",
        "required_yes": "Yes",
        "required_no": "No",
        "required_conditional": "Conditional",
        "endpoint_v1_templates_list_desc": "List saved task templates",
        "endpoint_v1_templates_create_desc": "Create a task template",
        "endpoint_v1_template_detail_desc": "Fetch a single template",
        "endpoint_v1_template_update_desc": "Update template configuration",
        "endpoint_v1_template_enqueue_desc": "Create one or more task instances from a template",
        "endpoint_v1_tasks_list_desc": "List tasks with optional status, source, and template filters",
        "endpoint_v1_task_detail_desc": "Fetch task detail and current runtime snapshot",
        "endpoint_v1_task_console_desc": "Read task console output incrementally",
        "endpoint_v1_task_artifacts_desc": "Inspect generated task artifacts",
        "endpoint_v1_task_retry_desc": "Retry a finished task",
        "endpoint_v1_task_stop_desc": "Stop a running task",
        "endpoint_v1_task_delete_desc": "Delete a stopped or finished task",
        "endpoint_v1_task_download_desc": "Download the task directory archive",
        "param_template_id_desc": "Template ID. When provided, the task is enqueued from that template and `platform` plus `quantity` are no longer required",
        "param_platform_desc": "Platform name. Currently supports `openai-register` and `grok-register`",
        "param_quantity_desc": "Target success count. Completion is based on real successful results, not attempts",
        "param_count_desc": "How many task instances to create. Defaults to `1`, maximum `100`",
        "param_use_proxy_desc": "Legacy compatibility field. Only used when `proxy_mode` is omitted; `true` maps to `default`, otherwise `none`",
        "param_proxy_mode_desc": "Proxy mode: `none`, `default`, or `specific`",
        "param_proxy_id_desc": "Specific proxy ID. Usually used together with `proxy_mode=specific`",
        "param_concurrency_desc": "Concurrency from `1-64`. If omitted, the platform or template default is used",
        "param_name_desc": "Optional custom task name. The system generates one if omitted",
        "param_captcha_desc": "Captcha credential ID. Platforms such as `grok-register` usually require it",
        "param_email_credentials_desc": "Array of email credential IDs. If omitted, the site tries to use defaults; passing explicit IDs is more reliable",
        "param_cpa_desc": "Optional CPA credential ID for uploading successful results",
        "dashboard_running_tasks": "Running tasks",
        "dashboard_completed_tasks": "Completed tasks",
        "dashboard_credential_count": "Credentials",
        "dashboard_proxy_count": "Proxies",
        "empty_tasks": "No tasks yet",
        "empty_credentials": "No credentials yet",
        "empty_proxies": "No proxies yet",
        "empty_filtered_tasks": "No tasks match the current filter",
        "empty_api_keys": "No API keys yet",
        "default_badge": "default",
        "created_at": "Created at {value}",
        "last_used_at": "Last used {value}",
        "unused": "Not used yet",
        "use_default_gptmail": "Use default GPTMail",
        "use_default_yescaptcha": "Use default YesCaptcha",
        "choose_proxy": "Choose a proxy",
        "no_default_gptmail": "No default GPTMail",
        "no_default_yescaptcha": "No default YesCaptcha",
        "no_default_proxy": "No default proxy",
        "current_default": "Current default",
        "set_default": "Set default",
        "delete": "Delete",
        "enable": "Enable",
        "disable": "Disable",
        "stop_task": "Stop task",
        "download_zip": "Download archive",
        "delete_task": "Delete task",
        "save_now": "Created successfully, save it now",
        "status_queued": "Queued",
        "status_running": "Running",
        "status_stopping": "Stopping",
        "status_completed": "Completed",
        "status_partial": "Partially completed",
        "status_failed": "Failed",
        "status_stopped": "Stopped",
        "status_interrupted": "Interrupted",
        "task_detail_empty_title": "No tasks match the current filter",
        "task_detail_empty_desc": "Adjust the status filter on the left, or create a new task first.",
        "console_wait": "Select a task to see live console output.",
        "console_empty": "No console output yet.",
        "task_header_meta": "{platform} | Target {quantity} | Executed {executed} | Success {completed} | Status {status}",
        "created_task_confirm": "Task #{id} was created. Click OK to open task detail, or Cancel to stay here and create another one.",
        "delete_task_confirm": "Delete task #{id}?",
        "delete_credential_confirm": "Delete credential {name}?",
        "delete_proxy_confirm": "Delete proxy {name}?",
        "delete_api_key_confirm": "Delete this API key?",
        "api_key_meta": "{prefix}... | Created at {created_at}",
        "test_proxy": "Test latency",
        "proxy_test_ok": "Latency {latency} ms | Exit {ip}",
        "proxy_test_fail": "Probe failed",
        "proxy_cooldown_until": "Cooling down until {time}",
        "proxy_last_probe_success": "Last success {time}",
        "proxy_last_probe_fail": "Last failure {time}",
        "proxy_not_tested": "Not tested yet",
        "proxy_local_snapshot": "Local snapshot saved",
        "proxy_snapshot_source": "Snapshot from {name} | {protocol} | {server}:{port}",
        "proxy_status_success": "Status healthy",
        "proxy_status_failed": "Status failed",
        "proxy_status_cooling": "Status cooling down",
        # Subscription management
        "proxy_tab_external": "External Proxy",
        "proxy_tab_subscription": "Subscription Nodes",
        "subscription_add_title": "Add Subscription",
        "subscription_add_desc": "Enter subscription URL to auto-parse proxy nodes.",
        "field_subscription_url": "Subscription URL",
        "add_subscription": "Add Subscription",
        "subscriptions_title": "Subscriptions",
        "proxy_nodes_title": "Proxy Nodes",
        "proxy_nodes_desc": "Nodes parsed from subscriptions. Select one to use as proxy.",
        "refresh_subscription": "Refresh",
        "delete_subscription_confirm": "Delete subscription {name} and all its nodes?",
        "empty_subscriptions": "No subscriptions yet",
        "empty_proxy_nodes": "No proxy nodes yet",
        "node_protocol": "Protocol",
        "node_country": "Region",
        "node_use": "Use this node",
        "node_test_ok": "Latency {latency} ms | Exit {ip}",
        "node_test_fail": "Probe failed",
        "node_latency_unknown": "Not tested",
        "subscription_nodes": "{count} nodes",
        "subscription_last_refresh": "Last refresh: {time}",
    },
}

DEFAULT_SETTING_KEYS = {
    "default_yescaptcha_credential_id": None,
    "default_proxy_id": None,
}

db_lock = threading.RLock()
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def date_iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def detect_ui_lang(request: Request) -> str:
    # Force Simplified Chinese UI for all clients.
    return "zh-CN"


def get_ui_translations(lang: str) -> dict[str, str]:
    base = UI_TRANSLATIONS["zh-CN"]
    selected = UI_TRANSLATIONS.get(lang, {})
    return {**base, **selected}


def ensure_runtime_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    # Host-mounted workspaces on Windows can fail when SQLite uses rollback journals.
    conn.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}")
    conn.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS}")
    return conn


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def cleanup_legacy_settings(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", ("default_gptmail_credential_id",))


def cleanup_legacy_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS schedules")


def normalize_email_dispatch_stats(conn: sqlite3.Connection) -> None:
    timestamp = now_iso()
    rows = conn.execute("SELECT * FROM email_credential_dispatch_stats").fetchall()
    for row in rows:
        row_data = row_to_dict(row)
        score = _clamp_email_dispatch_score(row_data.get("quality_score"))
        if int(row_data.get("dispatch_count") or 0) > 0 and abs(score - EMAIL_DISPATCH_DEFAULT_SCORE) < 1e-9:
            score = _estimate_email_dispatch_score(row)

        last_failure_category = str(row_data.get("last_failure_category") or "").strip() or None
        if row_data.get("last_outcome") == "failure":
            inferred_category = _classify_email_dispatch_failure(str(row_data.get("last_error_reason") or ""))
            last_failure_category = inferred_category
        else:
            last_failure_category = None

        policy = EMAIL_DISPATCH_FAILURE_POLICIES.get(last_failure_category or "", EMAIL_DISPATCH_FAILURE_POLICIES["unknown"])
        consecutive_failures = int(row_data.get("consecutive_failures") or 0)
        if not last_failure_category or not bool(policy.get("counts_against_credential")):
            consecutive_failures = 0

        conn.execute(
            """
            UPDATE email_credential_dispatch_stats
            SET cooldown_until = NULL,
                quality_score = ?,
                dynamic_weight = ?,
                consecutive_failures = ?,
                last_failure_category = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                score,
                _email_dispatch_score_to_weight(score),
                consecutive_failures,
                last_failure_category,
                timestamp,
                int(row_data["id"]),
            ),
        )


def migrate_tasks_table(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    legacy_columns = {"email_credential_id", "mail_providers_json"}
    if not legacy_columns.intersection(existing):
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """
        CREATE TABLE tasks_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT NOT NULL,
            captcha_credential_id INTEGER,
            concurrency INTEGER NOT NULL DEFAULT 1,
            proxy TEXT,
            task_dir TEXT NOT NULL,
            console_path TEXT NOT NULL,
            archive_path TEXT,
            requested_config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            exit_code INTEGER,
            pid INTEGER,
            last_error TEXT,
            source TEXT NOT NULL DEFAULT 'ui',
            auto_delete_at TEXT,
            cpa_credential_id INTEGER,
            email_credential_ids_json TEXT,
            template_id INTEGER,
            template_name TEXT,
            queued_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(captcha_credential_id) REFERENCES credentials(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tasks_new (
            id, name, platform, quantity, status, captcha_credential_id, concurrency,
            proxy, task_dir, console_path, archive_path, requested_config_json, created_at,
            started_at, finished_at, exit_code, pid, last_error, source, auto_delete_at,
            cpa_credential_id, email_credential_ids_json, template_id, template_name, queued_at, updated_at
        )
        SELECT
            id, name, platform, quantity, status, captcha_credential_id, concurrency,
            proxy, task_dir, console_path, archive_path, requested_config_json, created_at,
            started_at, finished_at, exit_code, pid, last_error, source, auto_delete_at,
            cpa_credential_id, email_credential_ids_json, NULL, NULL, created_at, created_at
        FROM tasks
        """
    )
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.execute("PRAGMA foreign_keys=ON")


def init_db() -> None:
    ensure_runtime_dirs()
    with db_lock, get_connection() as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                api_key TEXT NOT NULL,
                base_url TEXT,
                prefix TEXT,
                domain TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                proxy_url TEXT NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT NOT NULL,
                captcha_credential_id INTEGER,
                concurrency INTEGER NOT NULL DEFAULT 1,
                proxy TEXT,
                task_dir TEXT NOT NULL,
                console_path TEXT NOT NULL,
                archive_path TEXT,
                requested_config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                pid INTEGER,
                last_error TEXT,
                source TEXT NOT NULL DEFAULT 'ui',
                auto_delete_at TEXT,
                FOREIGN KEY(captcha_credential_id) REFERENCES credentials(id)
            );

            CREATE TABLE IF NOT EXISTS task_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                concurrency INTEGER NOT NULL DEFAULT 1,
                captcha_credential_id INTEGER,
                proxy_mode TEXT NOT NULL DEFAULT 'none',
                proxy_id INTEGER,
                cpa_credential_id INTEGER,
                email_credential_ids_json TEXT,
                requested_config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_queued_at TEXT,
                queue_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(captcha_credential_id) REFERENCES credentials(id),
                FOREIGN KEY(proxy_id) REFERENCES proxies(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                last_refresh TEXT,
                node_count INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proxy_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER,
                name TEXT NOT NULL,
                server TEXT NOT NULL,
                port INTEGER NOT NULL,
                protocol TEXT NOT NULL,
                config TEXT NOT NULL,
                country TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_latency INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS email_credential_dispatch_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                credential_id INTEGER NOT NULL,
                dispatch_count INTEGER NOT NULL DEFAULT 0,
                mailbox_success_count INTEGER NOT NULL DEFAULT 0,
                otp_success_count INTEGER NOT NULL DEFAULT 0,
                account_success_count INTEGER NOT NULL DEFAULT 0,
                oauth_success_count INTEGER NOT NULL DEFAULT 0,
                final_success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                quality_score REAL NOT NULL DEFAULT {EMAIL_DISPATCH_DEFAULT_SCORE},
                dynamic_weight REAL NOT NULL DEFAULT {EMAIL_DISPATCH_DEFAULT_WEIGHT},
                cooldown_until TEXT,
                last_outcome TEXT,
                last_failure_category TEXT,
                last_error_reason TEXT,
                last_dispatched_at TEXT,
                last_reported_at TEXT,
                last_success_at TEXT,
                last_failure_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, credential_id)
            );
            """
        )
        ensure_columns(
            conn,
            "tasks",
            {
                "source": "TEXT NOT NULL DEFAULT 'ui'",
                "auto_delete_at": "TEXT",
                "cpa_credential_id": "INTEGER",
                "email_credential_ids_json": "TEXT",
                "template_id": "INTEGER",
                "template_name": "TEXT",
                "queued_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        ensure_columns(
            conn,
            "task_templates",
            {
                "updated_at": "TEXT",
                "last_queued_at": "TEXT",
                "queue_count": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        ensure_columns(
            conn,
            "proxies",
            {
                "cooldown_until": "TEXT",
                "last_probe_at": "TEXT",
                "last_probe_status": "TEXT",
                "last_probe_error": "TEXT",
                "last_probe_latency": "INTEGER",
                "snapshot_name": "TEXT",
                "snapshot_protocol": "TEXT",
                "snapshot_server": "TEXT",
                "snapshot_port": "INTEGER",
                "snapshot_config": "TEXT",
                "snapshot_country": "TEXT",
            },
        )
        ensure_columns(
            conn,
            "email_credential_dispatch_stats",
            {
                "quality_score": f"REAL NOT NULL DEFAULT {EMAIL_DISPATCH_DEFAULT_SCORE}",
                "last_failure_category": "TEXT",
            },
        )
        cleanup_legacy_settings(conn)
        cleanup_legacy_tables(conn)
        migrate_tasks_table(conn)
        normalize_email_dispatch_stats(conn)
        conn.commit()
    backfill_proxy_snapshots()


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_lock, get_connection() as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, get_connection() as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db_lock, get_connection() as conn:
        cursor = conn.execute(query, params)
        conn.commit()
        return int(cursor.lastrowid)


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, get_connection() as conn:
        conn.execute(query, params)
        conn.commit()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def asset_version(name: str) -> str:
    target = WEB_DIR / "static" / name
    try:
        return str(int(target.stat().st_mtime))
    except Exception:
        return str(int(time.time()))


def read_tail(path: Path, limit: int = 30000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - limit))
        return fh.read().decode("utf-8", errors="replace")


def model_to_dict(model: BaseModel, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return dict(model.model_dump(exclude_unset=exclude_unset))
    return dict(model.dict(exclude_unset=exclude_unset))


def task_available_actions(status: str) -> list[str]:
    if status == "queued":
        return ["stop", "delete"]
    if status == "running":
        return ["stop"]
    if status == "stopping":
        return []
    return ["retry", "delete", "download"]


def content_preview(value: str, limit: int = 280) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def get_setting(key: str) -> str | None:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return None if row is None else str(row["value"])


def set_setting(key: str, value: str | None) -> None:
    if value is None:
        execute_no_return("DELETE FROM settings WHERE key = ?", (key,))
        return
    execute_no_return(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def get_defaults() -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key in DEFAULT_SETTING_KEYS:
        raw = get_setting(key)
        result[key] = int(raw) if raw and raw.isdigit() else None
    return result


def email_dispatch_platforms() -> list[str]:
    items: list[str] = []
    for platform, spec in PLATFORMS.items():
        if spec.get("supports_multiple_email_credentials") or spec.get("requires_email_credential"):
            items.append(platform)
    return items


def get_or_create_internal_email_dispatch_token() -> str:
    token = get_setting("internal_email_dispatch_token")
    if token:
        return token
    token = secrets.token_urlsafe(32)
    set_setting("internal_email_dispatch_token", token)
    return token


def get_internal_request_token(request: Request) -> str:
    bearer = request.headers.get("Authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("X-Internal-Token", "").strip()


def require_internal_email_dispatch(request: Request) -> None:
    provided = get_internal_request_token(request)
    expected = get_or_create_internal_email_dispatch_token()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Internal dispatch token is invalid")


def ensure_email_dispatch_stat(platform: str, credential_id: int) -> sqlite3.Row:
    credential = get_credential(int(credential_id))
    if credential["kind"] not in EMAIL_CREDENTIAL_KINDS:
        raise HTTPException(status_code=400, detail=f"Credential {credential_id} is not an email credential")
    row = fetch_one(
        "SELECT * FROM email_credential_dispatch_stats WHERE platform = ? AND credential_id = ?",
        (platform, credential_id),
    )
    if row is not None:
        return row

    timestamp = now_iso()
    execute(
        """
        INSERT INTO email_credential_dispatch_stats (
            platform, credential_id, quality_score, dynamic_weight, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (platform, credential_id, EMAIL_DISPATCH_DEFAULT_SCORE, EMAIL_DISPATCH_DEFAULT_WEIGHT, timestamp, timestamp),
    )
    row = fetch_one(
        "SELECT * FROM email_credential_dispatch_stats WHERE platform = ? AND credential_id = ?",
        (platform, credential_id),
    )
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to initialize email dispatch stats")
    return row


def serialize_email_dispatch_stat(row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    item["cooldown_until"] = None
    item["cooldown_active"] = False
    item["platform_label"] = str(PLATFORMS.get(str(item["platform"]), {}).get("label") or item["platform"])
    item["quality_score"] = round(_clamp_email_dispatch_score(item.get("quality_score")), 4)
    item["dynamic_weight"] = round(float(item.get("dynamic_weight") or _email_dispatch_score_to_weight(item["quality_score"])), 2)
    return item


def get_email_dispatch_stats() -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    platforms = email_dispatch_platforms()
    if not platforms:
        return stats

    for credential in fetch_all(
        f"SELECT * FROM credentials WHERE kind IN ({','.join('?' for _ in EMAIL_CREDENTIAL_KINDS)}) ORDER BY name",
        tuple(sorted(EMAIL_CREDENTIAL_KINDS)),
    ):
        for platform in platforms:
            stats.append(serialize_email_dispatch_stat(ensure_email_dispatch_stat(platform, int(credential["id"]))))
    return stats


def acquire_email_credential(platform: str, candidate_ids: list[int]) -> tuple[dict[str, Any], dict[str, Any]]:
    platform = platform.strip()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")

    candidates: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    seen_ids: set[int] = set()
    for raw_id in candidate_ids:
        credential = get_credential(int(raw_id))
        credential_id = int(credential["id"])
        if credential_id in seen_ids:
            continue
        if credential["kind"] not in EMAIL_CREDENTIAL_KINDS:
            continue
        stat = ensure_email_dispatch_stat(platform, credential_id)
        candidates.append((credential, stat))
        seen_ids.add(credential_id)

    if not candidates:
        raise HTTPException(status_code=400, detail="No valid email credentials available for dispatch")

    selected_pair = random.choices(
        candidates,
        weights=[max(EMAIL_DISPATCH_MIN_WEIGHT, float(stat["dynamic_weight"] or EMAIL_DISPATCH_DEFAULT_WEIGHT)) for _, stat in candidates],
        k=1,
    )[0]
    credential, stat = selected_pair

    timestamp = now_iso()
    execute_no_return(
        """
        UPDATE email_credential_dispatch_stats
        SET dispatch_count = dispatch_count + 1,
            last_dispatched_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (timestamp, timestamp, int(stat["id"])),
    )
    updated_stat = ensure_email_dispatch_stat(platform, int(credential["id"]))
    meta = {
        "candidate_count": len(candidates),
    }
    return row_to_dict(credential), {"stat": serialize_email_dispatch_stat(updated_stat), "selection": meta}


def report_email_dispatch_event(platform: str, credential_id: int, event: str, reason: str | None = None) -> dict[str, Any]:
    platform = platform.strip()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
    row = ensure_email_dispatch_stat(platform, int(credential_id))
    timestamp = now_iso()
    current_score = _clamp_email_dispatch_score(row["quality_score"])
    event = event.strip()
    reason_text = (reason or "").strip()

    success_columns = {
        "mailbox_created": "mailbox_success_count",
        "otp_received": "otp_success_count",
        "account_created": "account_success_count",
        "oauth_success": "oauth_success_count",
        "task_success": "final_success_count",
    }
    if event in success_columns:
        column = success_columns[event]
        next_score = _email_dispatch_blend(
            current_score,
            EMAIL_DISPATCH_SUCCESS_TARGETS[event],
            EMAIL_DISPATCH_SUCCESS_BLEND[event],
        )
        next_weight = _email_dispatch_score_to_weight(next_score)
        execute_no_return(
            f"""
            UPDATE email_credential_dispatch_stats
            SET {column} = {column} + 1,
                consecutive_failures = 0,
                quality_score = ?,
                dynamic_weight = ?,
                cooldown_until = NULL,
                last_outcome = ?,
                last_failure_category = NULL,
                last_error_reason = NULL,
                last_reported_at = ?,
                last_success_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (next_score, next_weight, event, timestamp, timestamp, timestamp, int(row["id"])),
        )
    elif event == "failure":
        failure_category = _classify_email_dispatch_failure(reason_text)
        policy = EMAIL_DISPATCH_FAILURE_POLICIES.get(failure_category, EMAIL_DISPATCH_FAILURE_POLICIES["unknown"])
        next_score = _email_dispatch_blend(current_score, float(policy["target"]), float(policy["blend"]))
        next_weight = _email_dispatch_score_to_weight(next_score)
        if bool(policy.get("counts_against_credential")):
            consecutive_failures = int(row["consecutive_failures"] or 0) + 1
        else:
            consecutive_failures = 0
        execute_no_return(
            """
            UPDATE email_credential_dispatch_stats
            SET failure_count = failure_count + 1,
                consecutive_failures = ?,
                quality_score = ?,
                dynamic_weight = ?,
                cooldown_until = NULL,
                last_outcome = ?,
                last_failure_category = ?,
                last_error_reason = ?,
                last_reported_at = ?,
                last_failure_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                consecutive_failures,
                next_score,
                next_weight,
                event,
                failure_category,
                reason_text[:2000] or None,
                timestamp,
                timestamp,
                timestamp,
                int(row["id"]),
            ),
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported dispatch event: {event}")

    updated = ensure_email_dispatch_stat(platform, int(credential_id))
    return serialize_email_dispatch_stat(updated)


def hash_password(password: str, salt_hex: str | None = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, expected = stored.split("$", 1)
    actual = hash_password(password, salt_hex).split("$", 1)[1]
    return hmac.compare_digest(actual, expected)


def auth_is_configured() -> bool:
    return bool(get_setting("admin_password_hash"))


def cleanup_expired_sessions() -> None:
    execute_no_return("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))


def create_session_token() -> tuple[str, str]:
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = date_iso(now() + timedelta(hours=SESSION_TTL_HOURS))
    execute_no_return(
        "INSERT INTO sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (token_hash, now_iso(), expires_at),
    )
    return raw_token, expires_at


def delete_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    execute_no_return("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def is_authenticated_request(request: Request) -> bool:
    cleanup_expired_sessions()
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        return False
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = fetch_one("SELECT id FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now_iso()))
    return row is not None


def require_authenticated(request: Request) -> None:
    if not auth_is_configured():
        raise HTTPException(status_code=403, detail="Admin password is not configured yet")
    if not is_authenticated_request(request):
        raise HTTPException(status_code=401, detail="Login required")


def generate_api_key_secret() -> tuple[str, str, str]:
    raw = f"rc_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    prefix = raw[:12]
    return raw, key_hash, prefix


def verify_api_key(raw_key: str) -> sqlite3.Row | None:
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    row = fetch_one("SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1", (key_hash,))
    if row is not None:
        execute_no_return("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), int(row["id"])))
    return row


def get_request_api_key(request: Request) -> str | None:
    bearer = request.headers.get("Authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    header_key = request.headers.get("X-API-Key", "").strip()
    if header_key:
        return header_key
    return request.query_params.get("api_key")


def require_api_key(request: Request) -> sqlite3.Row:
    raw_key = get_request_api_key(request)
    if not raw_key:
        raise HTTPException(status_code=401, detail="API key required")
    row = verify_api_key(raw_key)
    if row is None:
        raise HTTPException(status_code=401, detail="API key is invalid")
    return row


def get_credentials() -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in fetch_all("SELECT * FROM credentials ORDER BY kind, name")]


def get_proxies() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in fetch_all("SELECT * FROM proxies ORDER BY name"):
        item = row_to_dict(row)
        proxy_url = str(item.get("proxy_url") or "")
        item["is_node_proxy"] = proxy_url.startswith("node://")
        item["has_local_snapshot"] = bool(item.get("snapshot_protocol") and item.get("snapshot_server") and item.get("snapshot_port"))
        item["node_id"] = None
        if item["is_node_proxy"]:
            try:
                item["node_id"] = int(proxy_url.split("://", 1)[1])
            except Exception:
                item["node_id"] = None
        items.append(item)
    return items


def get_api_keys() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in fetch_all("SELECT * FROM api_keys ORDER BY id DESC"):
        item = row_to_dict(row)
        item.pop("key_hash", None)
        items.append(item)
    return items


def get_credential(credential_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM credentials WHERE id = ?", (credential_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return row


def get_proxy(proxy_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM proxies WHERE id = ?", (proxy_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    return row


def get_task_template(template_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM task_templates WHERE id = ?", (template_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return row


def resolve_optional_credential(kind: str, credential_id: int | None, detail: str | None = None) -> sqlite3.Row | None:
    if credential_id is None:
        return None
    row = get_credential(int(credential_id))
    if row["kind"] != kind:
        raise HTTPException(status_code=400, detail=detail or f"Credential {credential_id} is not of type {kind}")
    return row


def normalize_proxy_mode(proxy_mode: str | None) -> str:
    mode = (proxy_mode or "none").strip().lower()
    if mode not in {"none", "default", "custom", "rotate"}:
        raise HTTPException(status_code=400, detail="Unsupported proxy mode")
    return mode


def parse_optional_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def proxy_label(proxy_row: sqlite3.Row) -> str:
    return f"{proxy_row['name']}#{proxy_row['id']}"


def proxy_cooldown_until(proxy_row: sqlite3.Row) -> datetime | None:
    return parse_optional_datetime(proxy_row["cooldown_until"] if "cooldown_until" in proxy_row.keys() else None)


def proxy_is_in_cooldown(proxy_row: sqlite3.Row, current_time: datetime | None = None) -> bool:
    cooldown_until = proxy_cooldown_until(proxy_row)
    if cooldown_until is None:
        return False
    return cooldown_until > (current_time or now())


def proxy_has_local_snapshot(proxy_row: sqlite3.Row | dict[str, Any]) -> bool:
    getter = proxy_row.get if isinstance(proxy_row, dict) else lambda key, default=None: proxy_row[key] if key in proxy_row.keys() else default
    return bool(getter("snapshot_protocol") and getter("snapshot_server") and getter("snapshot_port"))


def proxy_snapshot_payload_from_node_row(node_row: sqlite3.Row) -> dict[str, Any]:
    return {
        "snapshot_name": str(node_row["name"]),
        "snapshot_protocol": str(node_row["protocol"]),
        "snapshot_server": str(node_row["server"]),
        "snapshot_port": int(node_row["port"]),
        "snapshot_config": str(node_row["config"] or "{}"),
        "snapshot_country": str(node_row["country"] or ""),
    }


def persist_proxy_snapshot(proxy_id: int, snapshot: dict[str, Any]) -> None:
    execute_no_return(
        """
        UPDATE proxies
        SET snapshot_name = ?, snapshot_protocol = ?, snapshot_server = ?, snapshot_port = ?,
            snapshot_config = ?, snapshot_country = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            snapshot.get("snapshot_name"),
            snapshot.get("snapshot_protocol"),
            snapshot.get("snapshot_server"),
            snapshot.get("snapshot_port"),
            snapshot.get("snapshot_config"),
            snapshot.get("snapshot_country"),
            now_iso(),
            proxy_id,
        ),
    )


def backfill_proxy_snapshots() -> None:
    proxies = fetch_all("SELECT * FROM proxies WHERE proxy_url LIKE 'node://%'")
    for proxy in proxies:
        if proxy_has_local_snapshot(proxy):
            continue
        raw_node_id = str(proxy["proxy_url"]).split("://", 1)[1].strip()
        try:
            node_id = int(raw_node_id)
        except Exception:
            continue
        node = fetch_one("SELECT * FROM proxy_nodes WHERE id = ?", (node_id,))
        if node is None:
            continue
        persist_proxy_snapshot(int(proxy["id"]), proxy_snapshot_payload_from_node_row(node))


def mark_proxy_probe_success(proxy_id: int) -> None:
    timestamp = now_iso()
    execute_no_return(
        """
        UPDATE proxies
        SET cooldown_until = NULL, last_probe_at = ?, last_probe_status = ?, last_probe_error = NULL, last_probe_latency = NULL, updated_at = ?
        WHERE id = ?
        """,
        (timestamp, "success", timestamp, proxy_id),
    )


def record_proxy_probe_success(proxy_id: int, latency_ms: int | None) -> None:
    timestamp = now_iso()
    execute_no_return(
        """
        UPDATE proxies
        SET cooldown_until = NULL, last_probe_at = ?, last_probe_status = ?, last_probe_error = NULL, last_probe_latency = ?, updated_at = ?
        WHERE id = ?
        """,
        (timestamp, "success", latency_ms, timestamp, proxy_id),
    )


def mark_proxy_probe_failure(proxy_id: int, error: Exception | str) -> str:
    timestamp = now()
    cooldown_until = date_iso(timestamp + timedelta(seconds=PROXY_FAILURE_COOLDOWN_SECONDS))
    message = str(error)
    execute_no_return(
        """
        UPDATE proxies
        SET cooldown_until = ?, last_probe_at = ?, last_probe_status = ?, last_probe_error = ?, last_probe_latency = NULL, updated_at = ?
        WHERE id = ?
        """,
        (cooldown_until, date_iso(timestamp), "failed", message[:2000], date_iso(timestamp), proxy_id),
    )
    return cooldown_until


def resolve_proxy_row_url(proxy_row: sqlite3.Row) -> str:
    if proxy_has_local_snapshot(proxy_row):
        try:
            config = json.loads(proxy_row["snapshot_config"]) if proxy_row["snapshot_config"] else {}
        except Exception:
            config = {}
        resolved = resolve_node_proxy_url(
            node_id=1_000_000 + int(proxy_row["id"]),
            protocol=str(proxy_row["snapshot_protocol"]),
            config=config,
            server=str(proxy_row["snapshot_server"]),
            port=int(proxy_row["snapshot_port"]),
        )
        if not resolved:
            raise HTTPException(status_code=400, detail="Failed to start local proxy from saved snapshot")
        return resolved

    proxy_url = str(proxy_row["proxy_url"] or "").strip()
    if not proxy_url.startswith("node://"):
        return proxy_url

    raw_node_id = proxy_url.split("://", 1)[1].strip()
    try:
        node_id = int(raw_node_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Proxy node reference is invalid")

    node = fetch_one("SELECT * FROM proxy_nodes WHERE id = ?", (node_id,))
    if node is None:
        raise HTTPException(status_code=404, detail="Proxy node not found")
    if not int(node["is_active"] or 0):
        raise HTTPException(status_code=400, detail="Proxy node is disabled")

    try:
        config = json.loads(node["config"]) if node["config"] else {}
    except Exception:
        config = {}
    resolved = resolve_node_proxy_url(
        node_id=node_id,
        protocol=str(node["protocol"]),
        config=config,
        server=str(node["server"]),
        port=int(node["port"]),
    )
    if not resolved:
        raise HTTPException(status_code=400, detail="Failed to start local proxy for the selected node")
    return resolved


def resolve_and_probe_proxy_row(proxy_row: sqlite3.Row, *, timeout: float | None = None) -> str:
    resolved = resolve_proxy_row_url(proxy_row)
    try:
        result = probe_proxy_url(resolved, timeout=timeout or PROXY_PROBE_TIMEOUT_SECONDS)
    except Exception:
        stop_proxy_for_url(resolved)
        raise
    record_proxy_probe_success(int(proxy_row["id"]), result.get("latency_ms"))
    return resolved


def resolve_proxy_row_value(proxy_row: sqlite3.Row) -> str:
    proxy_url = str(proxy_row["proxy_url"] or "").strip()
    if not proxy_url.startswith("node://"):
        return proxy_url

    resolved = resolve_proxy_row_url(proxy_row)
    try:
        result = probe_proxy_url(resolved, timeout=PROXY_PROBE_TIMEOUT_SECONDS)
        record_proxy_probe_success(int(proxy_row["id"]), result.get("latency_ms"))
    except Exception as exc:
        stop_proxy_for_url(resolved)
        raise HTTPException(status_code=400, detail=f"Selected proxy node test failed: {exc}")
    return resolved


def test_proxy_row(proxy_row: sqlite3.Row, *, cooldown_on_failure: bool = True, timeout: float | None = None) -> dict[str, Any]:
    resolved = resolve_proxy_row_url(proxy_row)
    try:
        result = probe_proxy_url(resolved, timeout=timeout or PROXY_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        stop_proxy_for_url(resolved)
        if cooldown_on_failure:
            cooldown_until = mark_proxy_probe_failure(int(proxy_row["id"]), exc)
        else:
            timestamp = now_iso()
            execute_no_return(
                """
                UPDATE proxies
                SET last_probe_at = ?, last_probe_status = ?, last_probe_error = ?, last_probe_latency = NULL, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, "failed", str(exc)[:2000], timestamp, int(proxy_row["id"])),
            )
            cooldown_until = None
        raise HTTPException(
            status_code=400,
            detail=f"Proxy test failed: {exc}" + (f" (cooldown until {cooldown_until})" if cooldown_until else ""),
        ) from exc

    record_proxy_probe_success(int(proxy_row["id"]), result.get("latency_ms"))
    payload = result.get("payload") or {}
    return {
        "ok": True,
        "proxy_url": resolved,
        "latency": result.get("latency_ms"),
        "exit_ip": payload.get("ip") if isinstance(payload, dict) else None,
        "message": "Proxy is reachable",
        "cooldown_until": None,
    }


def get_task(task_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def resolve_required_credential(kind: str, credential_id: int | None) -> sqlite3.Row:
    defaults = get_defaults()
    selected_id = credential_id
    if selected_id is None:
        selected_id = defaults["default_yescaptcha_credential_id"] if kind == "yescaptcha" else None
    if selected_id is None:
        raise HTTPException(status_code=400, detail=f"No default {kind} credential is configured")
    credential = get_credential(int(selected_id))
    if credential["kind"] != kind:
        raise HTTPException(status_code=400, detail=f"Credential {selected_id} is not of type {kind}")
    return credential


def parse_json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    return value if isinstance(value, list) else []


def resolve_email_credentials(email_credential_ids: list[int] | None) -> list[sqlite3.Row]:
    resolved: list[sqlite3.Row] = []
    seen_ids: set[int] = set()

    for credential_id in email_credential_ids or []:
        row = get_credential(int(credential_id))
        if row["kind"] not in EMAIL_CREDENTIAL_KINDS:
            raise HTTPException(status_code=400, detail=f"Credential {credential_id} is not an email credential")
        if int(row["id"]) in seen_ids:
            continue
        resolved.append(row)
        seen_ids.add(int(row["id"]))

    return resolved


def build_email_credentials_payload(email_credential_ids: list[int]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for credential_id in email_credential_ids:
        credential = get_credential(int(credential_id))
        if credential["kind"] not in EMAIL_CREDENTIAL_KINDS:
            raise RuntimeError(f"Credential {credential_id} is not an email credential")
        payload.append(
            {
                "id": int(credential["id"]),
                "name": str(credential["name"]),
                "kind": str(credential["kind"]),
                "api_key": str(credential["api_key"] or ""),
                "base_url": str(credential["base_url"] or ""),
                "prefix": str(credential["prefix"] or ""),
                "domain": str(credential["domain"] or ""),
                "notes": str(credential["notes"] or ""),
            }
        )
    return payload


def task_uses_credential(task: sqlite3.Row, credential_id: int) -> bool:
    if task["captcha_credential_id"] == credential_id:
        return True
    if "cpa_credential_id" in task.keys() and task["cpa_credential_id"] == credential_id:
        return True

    for selected_id in parse_json_list(task["email_credential_ids_json"] if "email_credential_ids_json" in task.keys() else None):
        try:
            if int(selected_id) == credential_id:
                return True
        except Exception:
            continue

    return False


# 轮换代理索引（全局变量）
_rotate_proxy_index = 0

def resolve_proxy_value(proxy_mode: str, proxy_id: int | None) -> str | None:
    global _rotate_proxy_index
    mode = proxy_mode or "none"
    defaults = get_defaults()
    if mode == "none":
        return None
    if mode == "default":
        selected = defaults["default_proxy_id"]
        if selected is None:
            raise HTTPException(status_code=400, detail="No default proxy is configured")
        return resolve_proxy_row_value(get_proxy(int(selected)))
    if mode == "custom":
        if proxy_id is None:
            raise HTTPException(status_code=400, detail="A proxy must be selected")
        return resolve_proxy_row_value(get_proxy(proxy_id))
    if mode == "rotate":
        all_proxies = fetch_all("SELECT * FROM proxies ORDER BY id")
        if not all_proxies:
            raise HTTPException(status_code=400, detail="No proxies available for rotation")
        current_time = now()
        available_proxies = [proxy for proxy in all_proxies if not proxy_is_in_cooldown(proxy, current_time)]
        if not available_proxies:
            next_available = min(
                (proxy_cooldown_until(proxy) for proxy in all_proxies if proxy_cooldown_until(proxy) is not None),
                default=None,
            )
            if next_available is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"All rotate proxies are cooling down until {date_iso(next_available)}",
                )
            raise HTTPException(status_code=400, detail="No rotate proxies are currently available")

        start_index = _rotate_proxy_index % len(available_proxies)
        failures: list[str] = []
        for offset in range(len(available_proxies)):
            proxy_row = available_proxies[(start_index + offset) % len(available_proxies)]
            try:
                resolved = resolve_and_probe_proxy_row(proxy_row)
                _rotate_proxy_index += offset + 1
                print(
                    f"[rotate] Selected healthy proxy {proxy_label(proxy_row)} "
                    f"after {offset + 1} attempt(s); next index={_rotate_proxy_index}"
                )
                return resolved
            except Exception as exc:
                cooldown_until = mark_proxy_probe_failure(int(proxy_row["id"]), exc)
                failures.append(f"{proxy_label(proxy_row)} -> cooldown until {cooldown_until}: {exc}")
                print(
                    f"[rotate] Proxy {proxy_label(proxy_row)} failed probe and enters cooldown "
                    f"until {cooldown_until}: {exc}"
                )

        raise HTTPException(
            status_code=400,
            detail=(
                "All rotate proxies failed probe and entered cooldown. "
                + " | ".join(failures[:5])
            ),
        )
    raise HTTPException(status_code=400, detail="Unsupported proxy mode")


def task_paths(task: sqlite3.Row | dict[str, Any]) -> dict[str, Path]:
    task_dir = Path(task["task_dir"])
    if task["platform"] == "openai-register":
        results_file = task_dir / "output" / "registered_accounts.txt"
    else:
        results_file = task_dir / "keys" / "accounts.txt"
    archive_path = Path(task["archive_path"]) if task["archive_path"] else task_dir / "task_result.zip"
    return {
        "task_dir": task_dir,
        "console_path": Path(task["console_path"]),
        "results_file": results_file,
        "archive_path": archive_path,
    }


def count_result_lines(task: sqlite3.Row | dict[str, Any]) -> int:
    results_file = task_paths(task)["results_file"]
    if not results_file.exists():
        return 0
    with results_file.open("r", encoding="utf-8", errors="ignore") as fh:
        return sum(1 for line in fh if line.strip())


def create_archive(task: sqlite3.Row | dict[str, Any]) -> Path:
    paths = task_paths(task)
    archive_path = paths["archive_path"]
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in paths["task_dir"].rglob("*"):
            if file_path.is_dir() or file_path == archive_path:
                continue
            zf.write(file_path, file_path.relative_to(paths["task_dir"]))
    execute_no_return("UPDATE tasks SET archive_path = ?, updated_at = ? WHERE id = ?", (str(archive_path), now_iso(), int(task["id"])))
    return archive_path


def task_artifact_summary(task: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    paths = task_paths(task)
    task_dir = paths["task_dir"]
    token_dir = task_dir / "output" / "codex_tokens"
    known_paths = [
        ("console", paths["console_path"]),
        ("task_config", task_dir / "task.json"),
        ("email_credentials", task_dir / "email_credentials.json"),
        ("registered_accounts", task_dir / "output" / "registered_accounts.txt"),
        ("access_tokens", task_dir / "output" / "ak.txt"),
        ("refresh_tokens", task_dir / "output" / "rk.txt"),
        ("results", paths["results_file"]),
        ("archive", paths["archive_path"]),
    ]
    files: list[dict[str, Any]] = []
    for kind, path in known_paths:
        if path.exists() and path.is_file():
            files.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "relative_path": str(path.relative_to(task_dir)),
                    "size": path.stat().st_size,
                }
            )
    token_json_count = 0
    if token_dir.exists() and token_dir.is_dir():
        token_json_count = len(list(token_dir.glob("*.json")))
    return {
        "task_dir": str(task_dir),
        "files": files,
        "token_json_count": token_json_count,
        "has_archive": paths["archive_path"].exists(),
    }


def _parse_runtime_from_console(console_text: str) -> dict[str, Any]:
    progress_matches = list(
        re.finditer(r"进度:\s*\[.*?\]\s+([0-9.]+)% \[(\d+)/(\d+)\] 成功:(\d+) 失败:(\d+) 速率:([0-9.]+)/s", console_text)
    )
    progress: dict[str, Any] | None = None
    if progress_matches:
        last = progress_matches[-1]
        progress = {
            "percent": float(last.group(1)),
            "done": int(last.group(2)),
            "total": int(last.group(3)),
            "success": int(last.group(4)),
            "failed": int(last.group(5)),
            "rate_per_sec": float(last.group(6)),
        }

    cpa_summaries = re.findall(r"\[CPA\]\s+上传完成: 成功\s+(\d+)\s+个,\s+失败\s+(\d+)\s+个", console_text)
    cpa_uploaded_count = int(cpa_summaries[-1][0]) if cpa_summaries else len(re.findall(r"\[CPA\]\s+✅", console_text))
    cpa_failed_count = int(cpa_summaries[-1][1]) if cpa_summaries else len(re.findall(r"\[CPA\]\s+❌", console_text))

    lines = [line.strip() for line in console_text.splitlines() if line.strip()]
    last_event = None
    current_step = None
    for line in reversed(lines):
        if line.startswith("进度:"):
            continue
        if last_event is None:
            last_event = line
        if any(marker in line for marker in ("[Step]", "[OAuth]", "[OTP]", "[CPA]", "[FAIL]", "[OK]")):
            current_step = line
            break

    return {
        "progress": progress,
        "cpa_uploaded_count": cpa_uploaded_count,
        "cpa_failed_count": cpa_failed_count,
        "last_event": last_event,
        "current_step": current_step,
    }


def task_runtime_payload(row: sqlite3.Row) -> dict[str, Any]:
    console_path = Path(row["console_path"])
    console_text = read_tail(console_path, 200000)
    runtime = _parse_runtime_from_console(console_text)
    runtime["available_actions"] = task_available_actions(str(row["status"]))
    runtime["console_bytes"] = console_path.stat().st_size if console_path.exists() else 0
    runtime["heartbeat_at"] = (
        datetime.fromtimestamp(console_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if console_path.exists()
        else None
    )
    runtime["artifacts"] = task_artifact_summary(row)
    return runtime


def task_result_metrics(task: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    file_results = count_result_lines(task)
    console_tail = read_tail(Path(task["console_path"]), 200000)
    runtime = _parse_runtime_from_console(console_tail)
    progress = runtime.get("progress") or {}
    progress_done = int(progress.get("done") or 0)
    progress_success = int(progress.get("success") or 0)
    progress_failed = int(progress.get("failed") or 0)
    success_count = max(file_results, progress_success)
    return {
        "results_count": success_count,
        "executed_count": max(success_count, progress_done),
        "success_count": success_count,
        "failed_count": progress_failed,
        "console_tail": console_tail,
        "runtime": runtime,
    }


def serialize_task(row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    metrics = task_result_metrics(row)
    item["results_count"] = metrics["results_count"]
    item["console_tail"] = metrics["console_tail"]
    runtime = metrics["runtime"]
    progress = runtime.get("progress") or {}
    item["executed_count"] = metrics["executed_count"]
    item["success_count"] = metrics["success_count"]
    item["failed_count"] = metrics["failed_count"]
    item["progress"] = progress
    try:
        item["requested_config"] = json.loads(item["requested_config_json"])
    except Exception:
        item["requested_config"] = {}
    item["email_credential_ids"] = parse_json_list(item.get("email_credential_ids_json"))
    item["available_actions"] = task_available_actions(str(item["status"]))
    return item


def serialize_task_template(row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    item["email_credential_ids"] = parse_json_list(item.get("email_credential_ids_json"))
    try:
        item["requested_config"] = json.loads(item["requested_config_json"])
    except Exception:
        item["requested_config"] = {}
    item["available_actions"] = ["update", "enqueue", "delete"]
    return item


def get_tasks() -> list[dict[str, Any]]:
    return [serialize_task(row) for row in fetch_all("SELECT * FROM tasks ORDER BY id DESC")]


def get_task_templates() -> list[dict[str, Any]]:
    return [serialize_task_template(row) for row in fetch_all("SELECT * FROM task_templates ORDER BY id DESC")]


def query_tasks(*, status: str | None = None, source: str | None = None, template_id: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if template_id is not None:
        clauses.append("template_id = ?")
        params.append(int(template_id))
    query = "SELECT * FROM tasks"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    return [serialize_task(row) for row in fetch_all(query, tuple(params))]


def dashboard_summary() -> dict[str, Any]:
    tasks = get_tasks()
    credentials = get_credentials()
    proxies = get_proxies()
    return {
        "running_tasks": sum(1 for task in tasks if task["status"] in {"queued", "running", "stopping"}),
        "completed_tasks": sum(1 for task in tasks if task["status"] == "completed"),
        "credential_count": len(credentials),
        "proxy_count": len(proxies),
        "recent_tasks": tasks[:5],
    }


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    api_key: str | None = None
    base_url: str | None = None
    prefix: str | None = None
    domain: str | None = None
    notes: str | None = None


class ProxyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    proxy_url: str = Field(min_length=1, max_length=300)
    notes: str | None = None
    snapshot_name: str | None = None
    snapshot_protocol: str | None = None
    snapshot_server: str | None = None
    snapshot_port: int | None = Field(default=None, ge=1, le=65535)
    snapshot_config: str | None = None
    snapshot_country: str | None = None


class SubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=500)
    notes: str | None = None


class SubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = None


class ProxyNodeCreate(BaseModel):
    subscription_id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    server: str = Field(min_length=1, max_length=120)
    port: int = Field(ge=1, le=65535)
    protocol: str = Field(min_length=1, max_length=20)
    config: str = Field(min_length=1)
    country: str | None = None
    is_active: bool = True


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str
    quantity: int = Field(ge=1, le=100000)
    captcha_credential_id: int | None = None
    concurrency: int = Field(default=1, ge=1, le=64)
    proxy_mode: str = "none"
    proxy_id: int | None = None
    email_credential_ids: list[int]
    cpa_credential_id: int | None = None


class TaskTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str
    quantity: int = Field(ge=1, le=100000)
    captcha_credential_id: int | None = None
    concurrency: int | None = Field(default=None, ge=1, le=64)
    proxy_mode: str = "none"
    proxy_id: int | None = None
    email_credential_ids: list[int]
    cpa_credential_id: int | None = None


class TaskTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    platform: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=100000)
    captcha_credential_id: int | None = None
    concurrency: int | None = Field(default=None, ge=1, le=64)
    proxy_mode: str | None = None
    proxy_id: int | None = None
    email_credential_ids: list[int] | None = None
    cpa_credential_id: int | None = None


class TaskTemplateEnqueueBatch(BaseModel):
    count: int = Field(default=1, ge=1, le=100)


class ExternalTaskCreate(BaseModel):
    platform: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=100000)
    use_proxy: bool | None = None
    concurrency: int | None = Field(default=None, ge=1, le=64)
    name: str | None = None
    proxy_mode: str | None = None
    proxy_id: int | None = None
    captcha_credential_id: int | None = None
    email_credential_ids: list[int] | None = None
    cpa_credential_id: int | None = None
    template_id: int | None = None
    count: int = Field(default=1, ge=1, le=100)


class PasswordPayload(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class DefaultSettingsPayload(BaseModel):
    default_yescaptcha_credential_id: int | None = None
    default_proxy_id: int | None = None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class InternalEmailAcquirePayload(BaseModel):
    platform: str = Field(min_length=1, max_length=120)
    candidate_ids: list[int] = Field(default_factory=list)
    task_id: int | None = None
    worker_index: int | None = None


class InternalEmailReportPayload(BaseModel):
    platform: str = Field(min_length=1, max_length=120)
    credential_id: int
    event: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=2000)
    task_id: int | None = None
    worker_index: int | None = None


@dataclass
class TaskResolvedConfig:
    platform: str
    quantity: int
    concurrency: int
    captcha_credential_id: int | None
    proxy_value: str | None
    proxy_mode: str
    source: str
    auto_delete_at: str | None
    requested_config: dict[str, Any]
    email_credential_ids: list[int]
    cpa_credential_id: int | None = None
    template_id: int | None = None
    template_name: str | None = None


def validate_platform(platform: str) -> dict[str, Any]:
    if platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail="Unsupported platform")
    return PLATFORMS[platform]


def normalize_template_configuration(
    *,
    name: str,
    platform: str,
    quantity: int,
    concurrency: int | None,
    captcha_credential_id: int | None,
    proxy_mode: str,
    proxy_id: int | None,
    email_credential_ids: list[int] | None = None,
    cpa_credential_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    spec = validate_platform(platform)
    resolved_name = name.strip() or f"{platform}-{now().strftime('%Y%m%d-%H%M%S')}"
    resolved_concurrency = concurrency or int(spec["default_concurrency"])
    resolved_concurrency = max(1, resolved_concurrency)
    normalized_proxy_mode = normalize_proxy_mode(proxy_mode)

    if not spec.get("supports_proxy"):
        normalized_proxy_mode = "none"
        proxy_id = None

    if normalized_proxy_mode == "custom":
        if proxy_id is None:
            raise HTTPException(status_code=400, detail="A proxy must be selected")
        get_proxy(int(proxy_id))
    else:
        proxy_id = None

    if spec.get("requires_captcha_credential"):
        captcha_row = resolve_required_credential("yescaptcha", captcha_credential_id)
    else:
        captcha_row = resolve_optional_credential("yescaptcha", captcha_credential_id)
    cpa_row = resolve_optional_credential("cpa", cpa_credential_id)
    selected_email_credentials = resolve_email_credentials(email_credential_ids)
    selected_email_credential_ids = [int(row["id"]) for row in selected_email_credentials]
    if spec.get("requires_email_credential") and not selected_email_credential_ids:
        raise HTTPException(status_code=400, detail="At least one email credential is required")

    payload = {
        "name": resolved_name,
        "platform": platform,
        "quantity": quantity,
        "concurrency": resolved_concurrency,
        "proxy_mode": normalized_proxy_mode,
        "proxy_id": int(proxy_id) if proxy_id is not None else None,
        "captcha_credential_id": int(captcha_row["id"]) if captcha_row else None,
        "email_credential_ids": selected_email_credential_ids,
        "cpa_credential_id": int(cpa_row["id"]) if cpa_row else None,
    }
    return resolved_name, payload


def resolve_task_configuration(
    *,
    name: str,
    platform: str,
    quantity: int,
    concurrency: int | None,
    captcha_credential_id: int | None,
    proxy_mode: str,
    proxy_id: int | None,
    source: str,
    auto_delete_at: str | None,
    email_credential_ids: list[int] | None = None,
    cpa_credential_id: int | None = None,
) -> tuple[str, TaskResolvedConfig]:
    spec = validate_platform(platform)
    resolved_name = name.strip() or f"{platform}-{now().strftime('%Y%m%d-%H%M%S')}"
    resolved_concurrency = concurrency or int(spec["default_concurrency"])
    resolved_concurrency = max(1, resolved_concurrency)
    normalized_proxy_mode = normalize_proxy_mode(proxy_mode)

    if not spec.get("supports_proxy"):
        normalized_proxy_mode = "none"
        proxy_id = None

    captcha_row = None
    if spec.get("requires_captcha_credential"):
        captcha_row = resolve_required_credential("yescaptcha", captcha_credential_id)
    elif captcha_credential_id is not None:
        captcha_row = resolve_optional_credential("yescaptcha", captcha_credential_id)

    proxy_value = None
    if spec.get("supports_proxy"):
        proxy_value = resolve_proxy_value(normalized_proxy_mode, proxy_id)

    cpa_row = resolve_optional_credential("cpa", cpa_credential_id)

    selected_email_credentials = resolve_email_credentials(email_credential_ids)
    selected_email_credential_ids = [int(row["id"]) for row in selected_email_credentials]
    if spec.get("requires_email_credential") and not selected_email_credential_ids:
        raise HTTPException(status_code=400, detail="At least one email credential is required")

    requested_config = {
        "name": resolved_name,
        "platform": platform,
        "quantity": quantity,
        "concurrency": resolved_concurrency,
        "source": source,
        "proxy_mode": normalized_proxy_mode,
        "proxy_id": proxy_id,
        "proxy_value": proxy_value,
        "email_credential_ids": selected_email_credential_ids,
        "captcha_credential_id": int(captcha_row["id"]) if captcha_row else None,
        "auto_delete_at": auto_delete_at,
        "cpa_credential_id": int(cpa_row["id"]) if cpa_row else None,
    }
    return resolved_name, TaskResolvedConfig(
        platform=platform,
        quantity=quantity,
        concurrency=resolved_concurrency,
        captcha_credential_id=int(captcha_row["id"]) if captcha_row else None,
        proxy_value=proxy_value,
        proxy_mode=normalized_proxy_mode,
        source=source,
        auto_delete_at=auto_delete_at,
        requested_config=requested_config,
        email_credential_ids=selected_email_credential_ids,
        cpa_credential_id=int(cpa_row["id"]) if cpa_row else None,
    )


def insert_task(*, name: str, config: TaskResolvedConfig) -> int:
    timestamp = now_iso()
    placeholder_dir = TASKS_DIR / f"pending_{int(time.time() * 1000)}_{secrets.token_hex(3)}"
    placeholder_dir.mkdir(parents=True, exist_ok=True)
    console_path = placeholder_dir / "console.log"
    template_id = config.template_id if config.template_id is not None else config.requested_config.get("template_id")
    template_name = config.template_name if config.template_name is not None else config.requested_config.get("template_name")
    task_id = execute(
        """
        INSERT INTO tasks (
            name, platform, quantity, status, captcha_credential_id, concurrency,
            proxy, task_dir, console_path, archive_path, requested_config_json, created_at, source, auto_delete_at,
            cpa_credential_id, email_credential_ids_json, template_id, template_name, queued_at, updated_at
        )
        VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            config.platform,
            config.quantity,
            config.captcha_credential_id,
            config.concurrency,
            config.proxy_value,
            str(placeholder_dir),
            str(console_path),
            json.dumps(config.requested_config, ensure_ascii=False),
            timestamp,
            config.source,
            config.auto_delete_at,
            config.cpa_credential_id,
            json.dumps(config.email_credential_ids, ensure_ascii=False) if config.email_credential_ids else None,
            template_id,
            template_name,
            timestamp,
            timestamp,
        ),
    )
    final_dir = TASKS_DIR / f"task_{task_id}"
    final_console_path = final_dir / "console.log"
    if placeholder_dir.exists():
        if final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        placeholder_dir.rename(final_dir)
    execute_no_return(
        "UPDATE tasks SET task_dir = ?, console_path = ? WHERE id = ?",
        (str(final_dir), str(final_console_path), task_id),
    )
    write_json(final_dir / "task.json", {"id": task_id, **config.requested_config, "created_at": timestamp})
    return task_id


def insert_task_template(*, payload: dict[str, Any]) -> int:
    timestamp = now_iso()
    return execute(
        """
        INSERT INTO task_templates (
            name, platform, quantity, concurrency, captcha_credential_id,
            proxy_mode, proxy_id, cpa_credential_id, email_credential_ids_json,
            requested_config_json, created_at, updated_at, last_queued_at, queue_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
        """,
        (
            payload["name"],
            payload["platform"],
            payload["quantity"],
            payload["concurrency"],
            payload["captcha_credential_id"],
            payload["proxy_mode"],
            payload["proxy_id"],
            payload["cpa_credential_id"],
            json.dumps(payload["email_credential_ids"], ensure_ascii=False) if payload["email_credential_ids"] else None,
            json.dumps(payload, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )


def create_task_from_snapshot(
    snapshot: dict[str, Any],
    *,
    source: str,
    auto_delete_at: str | None,
    template_id: int | None = None,
    template_name: str | None = None,
) -> int:
    name, config = resolve_task_configuration(
        name=str(snapshot.get("name") or ""),
        platform=str(snapshot.get("platform") or ""),
        quantity=int(snapshot.get("quantity") or 0),
        concurrency=int(snapshot["concurrency"]) if snapshot.get("concurrency") is not None else None,
        captcha_credential_id=int(snapshot["captcha_credential_id"]) if snapshot.get("captcha_credential_id") is not None else None,
        proxy_mode=str(snapshot.get("proxy_mode") or "none"),
        proxy_id=int(snapshot["proxy_id"]) if snapshot.get("proxy_id") is not None else None,
        source=source,
        auto_delete_at=auto_delete_at,
        email_credential_ids=[int(item) for item in (snapshot.get("email_credential_ids") or [])],
        cpa_credential_id=int(snapshot["cpa_credential_id"]) if snapshot.get("cpa_credential_id") is not None else None,
    )
    resolved_template_id = template_id if template_id is not None else snapshot.get("template_id")
    resolved_template_name = template_name if template_name is not None else snapshot.get("template_name")
    if resolved_template_id is not None:
        config.template_id = int(resolved_template_id)
        config.requested_config["template_id"] = int(resolved_template_id)
    if resolved_template_name is not None:
        config.template_name = str(resolved_template_name)
        config.requested_config["template_name"] = str(resolved_template_name)
    return insert_task(name=name, config=config)


@dataclass
class ManagedProcess:
    task_id: int
    process: subprocess.Popen[str]
    log_handle: Any


class TaskSupervisor:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._processes: dict[int, ManagedProcess] = {}
        self._lock = threading.RLock()

    def start(self) -> None:
        self.recover_stale_tasks()
        self._thread = threading.Thread(target=self._run_loop, name="register-supervisor", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            items = list(self._processes.values())
        for item in items:
            self._terminate_process(item.process)
            try:
                item.log_handle.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def recover_stale_tasks(self) -> None:
        for row in fetch_all("SELECT * FROM tasks WHERE status IN ('running', 'stopping')"):
            execute_no_return(
                """
                UPDATE tasks
                SET status = 'interrupted',
                    finished_at = ?,
                    last_error = COALESCE(last_error, 'Process ended while the service was offline.'),
                    pid = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), now_iso(), int(row["id"])),
            )
            try:
                create_archive(get_task(int(row["id"])))
            except Exception:
                pass

    def stop_task(self, task_id: int) -> None:
        row = get_task(task_id)
        if row["status"] == "queued":
            execute_no_return(
                "UPDATE tasks SET status = 'stopped', finished_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (now_iso(), "Task stopped before launch.", now_iso(), task_id),
            )
            create_archive(get_task(task_id))
            return
        with self._lock:
            managed = self._processes.get(task_id)
        if managed is None:
            raise HTTPException(status_code=409, detail="Task is not running")
        execute_no_return("UPDATE tasks SET status = 'stopping', updated_at = ? WHERE id = ?", (now_iso(), task_id))
        self._terminate_process(managed.process)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                cleanup_expired_sessions()
                self._finalize_finished()
                self._enforce_target_counts()
                self._cleanup_expired_tasks()
                self._launch_queued()
            except Exception as exc:
                print(f"[web-console] supervisor error: {exc}")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _launch_queued(self) -> None:
        slots = MAX_CONCURRENT_TASKS - self._running_count()
        if slots <= 0:
            return
        queued = fetch_all("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id ASC LIMIT ?", (slots,))
        for row in queued:
            self._start_task(row)

    def _running_count(self) -> int:
        with self._lock:
            return len(self._processes)

    def _start_task(self, task: sqlite3.Row) -> None:
        paths = task_paths(task)
        task_dir = paths["task_dir"]
        task_dir.mkdir(parents=True, exist_ok=True)
        console_path = paths["console_path"]
        requested = json.loads(task["requested_config_json"])

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        stdin_payload: str | None = None

        if task["platform"] == "openai-register":
            email_credential_ids = [
                int(item)
                for item in parse_json_list(task["email_credential_ids_json"] if "email_credential_ids_json" in task.keys() else None)
                if str(item).strip()
            ]
            if not email_credential_ids:
                raise RuntimeError("No email credentials configured for this task")

            command = [
                sys.executable,
                str(ROOT_DIR / "openai-register" / "ncs_register.py"),
                "--non-interactive",
                "--output-dir",
                str(task_dir / "output"),
                "--count",
                str(task["quantity"]),
                "--concurrency",
                str(task["concurrency"]),
            ]
            if task["proxy"]:
                command.extend(["--proxy", str(task["proxy"])])

            email_credentials_payload = build_email_credentials_payload(email_credential_ids)
            credentials_file = task_dir / "email_credentials.json"
            credentials_file.write_text(
                json.dumps(email_credentials_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            command.extend(["--email-credentials-file", str(credentials_file)])
            env["EMAIL_CREDENTIAL_MANAGER_URL"] = f"{INTERNAL_MANAGER_BASE_URL}/api/internal/email-credentials"
            env["EMAIL_CREDENTIAL_MANAGER_TOKEN"] = get_or_create_internal_email_dispatch_token()
            env["EMAIL_CREDENTIAL_MANAGER_PLATFORM"] = str(task["platform"])
            env["EMAIL_CREDENTIAL_MANAGER_TASK_ID"] = str(task["id"])

            if "cpa_credential_id" in task.keys() and task["cpa_credential_id"]:
                cpa_credential = get_credential(int(task["cpa_credential_id"]))
                command.extend(["--cpa-url", cpa_credential["base_url"], "--cpa-token", cpa_credential["api_key"]])
            cwd = ROOT_DIR / "openai-register"
        elif task["platform"] == "grok-register":
            email_credential_ids = [
                int(item)
                for item in parse_json_list(task["email_credential_ids_json"] if "email_credential_ids_json" in task.keys() else None)
                if str(item).strip()
            ]
            if not email_credential_ids:
                raise RuntimeError("No email credentials configured for this task")
            credential = get_credential(int(task["captcha_credential_id"]))
            env["YESCAPTCHA_KEY"] = credential["api_key"]
            if task["proxy"]:
                env["GROK_PROXY_URL"] = str(task["proxy"])
            env["GROK_OUTPUT_DIR"] = str(task_dir)
            credentials_file = task_dir / "email_credentials.json"
            credentials_file.write_text(
                json.dumps(build_email_credentials_payload(email_credential_ids), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            env["GROK_EMAIL_CREDENTIALS_FILE"] = str(credentials_file)
            env["EMAIL_CREDENTIAL_MANAGER_URL"] = f"{INTERNAL_MANAGER_BASE_URL}/api/internal/email-credentials"
            env["EMAIL_CREDENTIAL_MANAGER_TOKEN"] = get_or_create_internal_email_dispatch_token()
            env["EMAIL_CREDENTIAL_MANAGER_PLATFORM"] = str(task["platform"])
            env["EMAIL_CREDENTIAL_MANAGER_TASK_ID"] = str(task["id"])
            command = [sys.executable, str(ROOT_DIR / "grok-register" / "grok.py")]
            cwd = task_dir
            stdin_payload = f"{int(task['concurrency'])}\n"
        else:
            raise RuntimeError(f"Unsupported platform: {task['platform']}")

        log_handle = console_path.open("a", encoding="utf-8", buffering=1)
        log_handle.write(f"[{now_iso()}] Starting task {task['id']} ({task['platform']})\n")
        log_handle.write(f"[{now_iso()}] Config: {json.dumps(requested, ensure_ascii=False)}\n")
        log_handle.flush()

        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if stdin_payload is not None else None,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        if stdin_payload is not None and process.stdin is not None:
            process.stdin.write(stdin_payload)
            process.stdin.flush()
            process.stdin.close()

        execute_no_return(
            """
            UPDATE tasks
            SET status = 'running',
                started_at = ?,
                pid = ?,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), process.pid, now_iso(), int(task["id"])),
        )
        with self._lock:
            self._processes[int(task["id"])] = ManagedProcess(task_id=int(task["id"]), process=process, log_handle=log_handle)

    def _finalize_finished(self) -> None:
        with self._lock:
            items = list(self._processes.items())
        for task_id, item in items:
            exit_code = item.process.poll()
            if exit_code is None:
                continue
            try:
                item.log_handle.write(f"[{now_iso()}] Process exited with code {exit_code}\n")
                item.log_handle.flush()
            except Exception:
                pass
            try:
                item.log_handle.close()
            except Exception:
                pass
            with self._lock:
                self._processes.pop(task_id, None)
            self._complete_task(task_id, exit_code)

    def _enforce_target_counts(self) -> None:
        with self._lock:
            items = list(self._processes.items())
        for task_id, managed in items:
            row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
            if row is None or row["status"] != "running":
                continue
            if count_result_lines(row) >= int(row["quantity"]):
                execute_no_return("UPDATE tasks SET status = 'stopping', updated_at = ? WHERE id = ?", (now_iso(), task_id))
                self._terminate_process(managed.process)

    def _cleanup_expired_tasks(self) -> None:
        expired = fetch_all(
            """
            SELECT * FROM tasks
            WHERE auto_delete_at IS NOT NULL
              AND auto_delete_at <= ?
              AND status NOT IN ('queued', 'running', 'stopping')
            """,
            (now_iso(),),
        )
        for row in expired:
            paths = task_paths(row)
            try:
                shutil.rmtree(paths["task_dir"], ignore_errors=True)
            except Exception:
                pass
            if paths["archive_path"].exists():
                try:
                    paths["archive_path"].unlink()
                except Exception:
                    pass
            execute_no_return("DELETE FROM tasks WHERE id = ?", (int(row["id"]),))

    def _complete_task(self, task_id: int, exit_code: int) -> None:
        row = get_task(task_id)
        results_count = int(task_result_metrics(row)["results_count"])
        quantity = int(row["quantity"])
        current_status = row["status"]
        if results_count >= quantity:
            status = "completed"
            error = None
        elif current_status == "stopping":
            status = "stopped"
            error = row["last_error"] or "Task stopped by operator."
        elif results_count > 0:
            status = "partial"
            error = f"Task finished with {results_count}/{quantity} successful results."
        else:
            status = "failed"
            error = row["last_error"] or "Task finished without successful results."
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?,
                finished_at = ?,
                exit_code = ?,
                pid = NULL,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, now_iso(), exit_code, error, now_iso(), task_id),
        )
        create_archive(get_task(task_id))

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


supervisor = TaskSupervisor()


def state_payload() -> dict[str, Any]:
    return {
        "platforms": PLATFORMS,
        "defaults": get_defaults(),
        "credentials": get_credentials(),
        "email_credential_stats": get_email_dispatch_stats(),
        "proxies": get_proxies(),
        "task_templates": get_task_templates(),
        "tasks": get_tasks(),
        "api_keys": get_api_keys(),
        "dashboard": dashboard_summary(),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    cleanup_expired_sessions()
    supervisor.start()
    yield
    supervisor.shutdown()
    stop_all_proxies()


app = FastAPI(title="Register Task Console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


def make_session_response(payload: dict[str, Any], raw_token: str | None = None, expires_at: str | None = None) -> JSONResponse:
    response = JSONResponse(payload)
    if raw_token and expires_at:
        response.set_cookie(
            SESSION_COOKIE,
            raw_token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_TTL_HOURS * 3600,
            expires=expires_at,
        )
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    ui_lang = detect_ui_lang(request)
    translations = get_ui_translations(ui_lang)
    auth_view = "app"
    if not auth_is_configured():
        auth_view = "setup"
    elif not is_authenticated_request(request):
        auth_view = "login"
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "auth_view": auth_view,
            "platforms": PLATFORMS,
            "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
            "api_base_url": str(request.base_url).rstrip("/"),
            "ui_lang": ui_lang,
            "t": translations,
            "translations_json": json.dumps(translations, ensure_ascii=False),
            "app_css_version": asset_version("app.css"),
            "app_js_version": asset_version("app.js"),
        },
    )


@app.get("/api/auth/state")
async def auth_state(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "configured": auth_is_configured(),
            "authenticated": is_authenticated_request(request) if auth_is_configured() else False,
        }
    )


@app.post("/api/auth/setup")
async def auth_setup(payload: PasswordPayload) -> JSONResponse:
    if auth_is_configured():
        raise HTTPException(status_code=409, detail="Admin password is already configured")
    set_setting("admin_password_hash", hash_password(payload.password))
    raw_token, expires_at = create_session_token()
    return make_session_response({"ok": True}, raw_token, expires_at)


@app.post("/api/auth/login")
async def auth_login(payload: PasswordPayload) -> JSONResponse:
    if not verify_password(payload.password, get_setting("admin_password_hash")):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    raw_token, expires_at = create_session_token()
    return make_session_response({"ok": True}, raw_token, expires_at)


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    delete_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
    require_authenticated(request)
    return JSONResponse(state_payload())


@app.post("/api/internal/email-credentials/acquire")
async def internal_acquire_email_credential(payload: InternalEmailAcquirePayload, request: Request) -> JSONResponse:
    require_internal_email_dispatch(request)
    credential, meta = acquire_email_credential(payload.platform, payload.candidate_ids)
    return JSONResponse(
        {
            "ok": True,
            "credential": credential,
            "dispatch": meta,
            "task_id": payload.task_id,
            "worker_index": payload.worker_index,
        }
    )


@app.post("/api/internal/email-credentials/report")
async def internal_report_email_credential(payload: InternalEmailReportPayload, request: Request) -> JSONResponse:
    require_internal_email_dispatch(request)
    stat = report_email_dispatch_event(payload.platform, payload.credential_id, payload.event, payload.reason)
    return JSONResponse(
        {
            "ok": True,
            "stat": stat,
            "task_id": payload.task_id,
            "worker_index": payload.worker_index,
        }
    )


@app.post("/api/defaults")
async def update_defaults(payload: DefaultSettingsPayload, request: Request) -> JSONResponse:
    require_authenticated(request)
    if payload.default_yescaptcha_credential_id is not None and get_credential(payload.default_yescaptcha_credential_id)["kind"] != "yescaptcha":
        raise HTTPException(status_code=400, detail="Default YesCaptcha credential is invalid")
    if payload.default_proxy_id is not None:
        get_proxy(payload.default_proxy_id)
    set_setting("default_yescaptcha_credential_id", str(payload.default_yescaptcha_credential_id) if payload.default_yescaptcha_credential_id else None)
    set_setting("default_proxy_id", str(payload.default_proxy_id) if payload.default_proxy_id else None)
    return JSONResponse({"ok": True, "defaults": get_defaults()})


@app.post("/api/credentials")
async def create_credential(payload: CredentialCreate, request: Request) -> JSONResponse:
    require_authenticated(request)
    if payload.kind not in {"gptmail", "duckmail", "tempmail_lol", "cfmail", "mail_tm", "mail_gw", "yescaptcha", "cpa"}:
        raise HTTPException(status_code=400, detail="Unsupported credential kind")
    api_key = (payload.api_key or "").strip()
    if payload.kind in {"gptmail", "duckmail", "yescaptcha", "cpa"} and not api_key:
        raise HTTPException(status_code=400, detail=f"{payload.kind} credential requires an API key or token")
    timestamp = now_iso()
    credential_id = execute(
        """
        INSERT INTO credentials (name, kind, api_key, base_url, prefix, domain, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.kind,
            api_key,
            (payload.base_url or "").strip() or None,
            (payload.prefix or "").strip() or None,
            (payload.domain or "").strip() or None,
            (payload.notes or "").strip() or None,
            timestamp,
            timestamp,
        ),
    )
    return JSONResponse({"ok": True, "id": credential_id})


@app.delete("/api/credentials/{credential_id}")
async def delete_credential(credential_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    active_tasks = fetch_all("SELECT * FROM tasks WHERE status IN ('queued', 'running', 'stopping')")
    if any(task_uses_credential(task, credential_id) for task in active_tasks):
        raise HTTPException(status_code=409, detail="Credential is used by an active task")
    defaults = get_defaults()
    for key, value in defaults.items():
        if value == credential_id:
            set_setting(key, None)
    execute_no_return("DELETE FROM email_credential_dispatch_stats WHERE credential_id = ?", (credential_id,))
    execute_no_return("DELETE FROM credentials WHERE id = ?", (credential_id,))
    return JSONResponse({"ok": True})


@app.post("/api/credentials/{credential_id}/email-stats/reset")
async def reset_credential_email_stats(credential_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    credential = get_credential(credential_id)
    if credential["kind"] not in EMAIL_CREDENTIAL_KINDS:
        raise HTTPException(status_code=400, detail="Credential is not an email credential")

    timestamp = now_iso()
    execute_no_return(
        """
        UPDATE email_credential_dispatch_stats
        SET dispatch_count = 0,
            mailbox_success_count = 0,
            otp_success_count = 0,
            account_success_count = 0,
            oauth_success_count = 0,
            final_success_count = 0,
            failure_count = 0,
            consecutive_failures = 0,
            quality_score = ?,
            dynamic_weight = ?,
            cooldown_until = NULL,
            last_outcome = NULL,
            last_failure_category = NULL,
            last_error_reason = NULL,
            last_dispatched_at = NULL,
            last_reported_at = NULL,
            last_success_at = NULL,
            last_failure_at = NULL,
            updated_at = ?
        WHERE credential_id = ?
        """,
        (EMAIL_DISPATCH_DEFAULT_SCORE, EMAIL_DISPATCH_DEFAULT_WEIGHT, timestamp, credential_id),
    )
    return JSONResponse({"ok": True})


@app.post("/api/credentials/{credential_id}/email-stats/clear-cooldown")
async def clear_credential_email_stats_cooldown(credential_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    credential = get_credential(credential_id)
    if credential["kind"] not in EMAIL_CREDENTIAL_KINDS:
        raise HTTPException(status_code=400, detail="Credential is not an email credential")
    execute_no_return(
        """
        UPDATE email_credential_dispatch_stats
        SET cooldown_until = NULL,
            updated_at = ?
        WHERE credential_id = ?
        """,
        (now_iso(), credential_id),
    )
    return JSONResponse({"ok": True})


@app.post("/api/proxies")
async def create_proxy(payload: ProxyCreate, request: Request) -> JSONResponse:
    require_authenticated(request)
    timestamp = now_iso()
    proxy_id = execute(
        """
        INSERT INTO proxies (
            name, proxy_url, notes, created_at, updated_at,
            snapshot_name, snapshot_protocol, snapshot_server, snapshot_port, snapshot_config, snapshot_country
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.proxy_url.strip(),
            (payload.notes or "").strip() or None,
            timestamp,
            timestamp,
            (payload.snapshot_name or "").strip() or None,
            (payload.snapshot_protocol or "").strip() or None,
            (payload.snapshot_server or "").strip() or None,
            payload.snapshot_port,
            (payload.snapshot_config or "").strip() or None,
            (payload.snapshot_country or "").strip() or None,
        ),
    )
    return JSONResponse({"ok": True, "id": proxy_id})


@app.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    proxy = get_proxy(proxy_id)
    active = fetch_one("SELECT id FROM tasks WHERE proxy = ? AND status IN ('queued', 'running', 'stopping')", (str(proxy["proxy_url"]),))
    if active is None and str(proxy["proxy_url"]).startswith("node://"):
        active = fetch_one(
            "SELECT id FROM tasks WHERE requested_config_json LIKE ? AND status IN ('queued', 'running', 'stopping')",
            (f'%\"proxy_id\": {proxy_id}%',),
        )
    if active is not None:
        raise HTTPException(status_code=409, detail="Proxy is used by an active task")
    if get_defaults()["default_proxy_id"] == proxy_id:
        set_setting("default_proxy_id", None)
    execute_no_return("DELETE FROM proxies WHERE id = ?", (proxy_id,))
    return JSONResponse({"ok": True})


@app.post("/api/proxies/{proxy_id}/test")
async def test_proxy(proxy_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    proxy = get_proxy(proxy_id)
    return JSONResponse(test_proxy_row(proxy, cooldown_on_failure=False))


# ========== 订阅管理 API ==========

@app.get("/api/subscriptions")
async def list_subscriptions(request: Request) -> JSONResponse:
    require_authenticated(request)
    rows = fetch_all("SELECT * FROM subscriptions ORDER BY created_at DESC")
    result = []
    for row in rows:
        # 统计节点数量
        node_count = fetch_one(
            "SELECT COUNT(*) as cnt FROM proxy_nodes WHERE subscription_id = ?",
            (row["id"],)
        )
        result.append({
            "id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "last_refresh": row["last_refresh"],
            "node_count": node_count["cnt"] if node_count else 0,
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return JSONResponse({"subscriptions": result})


@app.post("/api/subscriptions")
async def create_subscription(payload: SubscriptionCreate, request: Request) -> JSONResponse:
    require_authenticated(request)
    timestamp = now_iso()

    # 获取订阅内容
    try:
        req = urllib.request.Request(
            payload.url.strip(),
            headers={"User-Agent": "ClashForWindows/0.20.39"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取订阅失败: {e}")

    # 解析节点
    nodes = parse_subscription(content)
    if not nodes:
        raise HTTPException(status_code=400, detail=f"未能解析出任何代理节点，返回预览：{content_preview(content)}")

    # 保存订阅
    sub_id = execute(
        """
        INSERT INTO subscriptions (name, url, last_refresh, node_count, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.url.strip(),
            timestamp,
            len(nodes),
            (payload.notes or "").strip() or None,
            timestamp,
            timestamp,
        ),
    )

    # 保存节点
    for node in nodes:
        execute(
            """
            INSERT INTO proxy_nodes (subscription_id, name, server, port, protocol, config, country, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                sub_id,
                node.name,
                node.server,
                node.port,
                node.protocol,
                json.dumps(node.config, ensure_ascii=False),
                node.country,
                timestamp,
            ),
        )

    return JSONResponse({"ok": True, "id": sub_id, "node_count": len(nodes)})


@app.delete("/api/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    # 检查是否有活跃任务使用此订阅的节点
    nodes = fetch_all("SELECT * FROM proxy_nodes WHERE subscription_id = ?", (sub_id,))
    for node in nodes:
        active = fetch_one(
            "SELECT id FROM tasks WHERE proxy = ? AND status IN ('queued', 'running', 'stopping')",
            (f"node:{node['id']}",)
        )
        if active:
            raise HTTPException(status_code=409, detail="订阅下的节点正在被活跃任务使用")

    # 为已加入的代理保留节点快照，避免订阅删除后失效
    for node in nodes:
        referenced = fetch_all("SELECT * FROM proxies WHERE proxy_url = ?", (f"node://{node['id']}",))
        for proxy in referenced:
            if not proxy_has_local_snapshot(proxy):
                persist_proxy_snapshot(int(proxy["id"]), proxy_snapshot_payload_from_node_row(node))

    # 删除节点和订阅
    execute_no_return("DELETE FROM proxy_nodes WHERE subscription_id = ?", (sub_id,))
    execute_no_return("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    return JSONResponse({"ok": True})


@app.post("/api/subscriptions/{sub_id}/refresh")
async def refresh_subscription(sub_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    sub = fetch_one("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")

    timestamp = now_iso()

    # 获取订阅内容
    try:
        req = urllib.request.Request(
            sub["url"].strip(),
            headers={"User-Agent": "ClashForWindows/0.20.39"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取订阅失败: {e}")

    # 解析节点
    nodes = parse_subscription(content)
    if not nodes:
        raise HTTPException(status_code=400, detail=f"未能解析出任何代理节点，返回预览：{content_preview(content)}")

    # 在刷新前为已加入代理固化本地快照，避免订阅变更导致代理失效
    old_nodes = fetch_all("SELECT * FROM proxy_nodes WHERE subscription_id = ?", (sub_id,))
    for old_node in old_nodes:
        referenced = fetch_all("SELECT * FROM proxies WHERE proxy_url = ?", (f"node://{old_node['id']}",))
        for proxy in referenced:
            if not proxy_has_local_snapshot(proxy):
                persist_proxy_snapshot(int(proxy["id"]), proxy_snapshot_payload_from_node_row(old_node))

    # 删除旧节点
    execute_no_return("DELETE FROM proxy_nodes WHERE subscription_id = ?", (sub_id,))

    # 保存新节点
    for node in nodes:
        execute(
            """
            INSERT INTO proxy_nodes (subscription_id, name, server, port, protocol, config, country, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                sub_id,
                node.name,
                node.server,
                node.port,
                node.protocol,
                json.dumps(node.config, ensure_ascii=False),
                node.country,
                timestamp,
            ),
        )

    # 更新订阅信息
    execute_no_return(
        "UPDATE subscriptions SET last_refresh = ?, node_count = ?, updated_at = ? WHERE id = ?",
        (timestamp, len(nodes), timestamp, sub_id),
    )

    return JSONResponse({"ok": True, "node_count": len(nodes)})


# ========== 代理节点 API ==========

@app.get("/api/proxy-nodes")
async def list_proxy_nodes(request: Request, subscription_id: int | None = None) -> JSONResponse:
    require_authenticated(request)
    if subscription_id:
        rows = fetch_all(
            "SELECT * FROM proxy_nodes WHERE subscription_id = ? ORDER BY country, name",
            (subscription_id,)
        )
    else:
        rows = fetch_all("SELECT * FROM proxy_nodes ORDER BY country, name")
    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "subscription_id": row["subscription_id"],
            "name": row["name"],
            "server": row["server"],
            "port": row["port"],
            "protocol": row["protocol"],
            "config": json.loads(row["config"]) if row["config"] else {},
            "country": row["country"],
            "country_name": get_country_name(row["country"]),
            "is_active": bool(row["is_active"]),
            "last_latency": row["last_latency"],
            "created_at": row["created_at"],
        })
    return JSONResponse({"nodes": result})


@app.put("/api/proxy-nodes/{node_id}")
async def update_proxy_node(node_id: int, payload: dict, request: Request) -> JSONResponse:
    require_authenticated(request)
    node = fetch_one("SELECT * FROM proxy_nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")

    is_active = payload.get("is_active")
    if is_active is not None:
        execute_no_return(
            "UPDATE proxy_nodes SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, node_id)
        )
    return JSONResponse({"ok": True})


@app.post("/api/proxy-nodes/{node_id}/test-old")
async def test_proxy_node(node_id: int, request: Request) -> JSONResponse:
    """测试节点延迟"""
    require_authenticated(request)
    node = fetch_one("SELECT * FROM proxy_nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")

    # TODO: 实现节点延迟测试（需要 gost 支持）
    # 暂时返回模拟数据
    return JSONResponse({"ok": True, "latency": None, "message": "需要 gost 支持"})


@app.post("/api/proxy-nodes/{node_id}/test")
async def proxy_node_connectivity_test(node_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    node = fetch_one("SELECT * FROM proxy_nodes WHERE id = ?", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="Proxy node not found")
    if not int(node["is_active"] or 0):
        raise HTTPException(status_code=400, detail="Proxy node is disabled")

    try:
        config = json.loads(node["config"]) if node["config"] else {}
    except Exception:
        config = {}

    resolved = resolve_node_proxy_url(
        node_id=node_id,
        protocol=str(node["protocol"]),
        config=config,
        server=str(node["server"]),
        port=int(node["port"]),
    )
    if not resolved:
        raise HTTPException(status_code=400, detail="Failed to start local proxy for the selected node")

    try:
        result = probe_proxy_url(resolved, timeout=12.0)
    except Exception as exc:
        execute_no_return(
            "UPDATE proxy_nodes SET last_latency = NULL WHERE id = ?",
            (node_id,),
        )
        raise HTTPException(status_code=400, detail=f"Node test failed: {exc}")

    execute_no_return(
        "UPDATE proxy_nodes SET last_latency = ? WHERE id = ?",
        (result.get("latency_ms"), node_id),
    )

    payload = result.get("payload") or {}
    return JSONResponse(
        {
            "ok": True,
            "proxy_url": resolved,
            "latency": result.get("latency_ms"),
            "exit_ip": payload.get("ip") if isinstance(payload, dict) else None,
            "message": "Node is reachable",
        }
    )


def template_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": str(row["name"]),
        "platform": str(row["platform"]),
        "quantity": int(row["quantity"]),
        "concurrency": int(row["concurrency"]),
        "captcha_credential_id": int(row["captcha_credential_id"]) if row["captcha_credential_id"] is not None else None,
        "proxy_mode": str(row["proxy_mode"] or "none"),
        "proxy_id": int(row["proxy_id"]) if row["proxy_id"] is not None else None,
        "email_credential_ids": [int(item) for item in parse_json_list(row["email_credential_ids_json"]) if str(item).strip()],
        "cpa_credential_id": int(row["cpa_credential_id"]) if row["cpa_credential_id"] is not None else None,
    }


def queue_template_instances(template: sqlite3.Row, *, count: int, source: str, auto_delete_at: str | None) -> list[int]:
    snapshot = template_snapshot_from_row(template)
    task_ids: list[int] = []
    for _ in range(count):
        task_ids.append(
            create_task_from_snapshot(
                snapshot,
                source=source,
                auto_delete_at=auto_delete_at,
                template_id=int(template["id"]),
                template_name=str(template["name"]),
            )
        )
    execute_no_return(
        "UPDATE task_templates SET last_queued_at = ?, queue_count = queue_count + ?, updated_at = ? WHERE id = ?",
        (now_iso(), count, now_iso(), int(template["id"])),
    )
    return task_ids


def api_task_status_payload(row: sqlite3.Row) -> dict[str, Any]:
    item = serialize_task(row)
    runtime = task_runtime_payload(row)
    payload = {
        "task_id": int(row["id"]),
        "status": item["status"],
        "completed_count": item["results_count"],
        "target_quantity": item["quantity"],
        "auto_delete_at": item["auto_delete_at"],
        "download_url": None,
        "available_actions": item["available_actions"],
        "runtime": runtime,
        "template_id": item.get("template_id"),
        "template_name": item.get("template_name"),
    }
    if item["status"] not in {"queued", "running", "stopping"}:
        payload["download_url"] = f"/api/external/tasks/{int(row['id'])}/download"
    return payload


@app.get("/api/task-templates")
async def list_task_templates(request: Request) -> JSONResponse:
    require_authenticated(request)
    return JSONResponse({"templates": get_task_templates()})


@app.post("/api/task-templates")
async def create_task_template(payload: TaskTemplateCreate, request: Request) -> JSONResponse:
    require_authenticated(request)
    _, normalized = normalize_template_configuration(
        name=payload.name,
        platform=payload.platform,
        quantity=payload.quantity,
        concurrency=payload.concurrency,
        captcha_credential_id=payload.captcha_credential_id,
        proxy_mode=payload.proxy_mode,
        proxy_id=payload.proxy_id,
        email_credential_ids=payload.email_credential_ids,
        cpa_credential_id=payload.cpa_credential_id,
    )
    template_id = insert_task_template(payload=normalized)
    return JSONResponse({"ok": True, "id": template_id})


@app.get("/api/task-templates/{template_id}")
async def task_template_detail(template_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    return JSONResponse({"template": serialize_task_template(get_task_template(template_id))})


@app.patch("/api/task-templates/{template_id}")
async def update_task_template(template_id: int, payload: TaskTemplateUpdate, request: Request) -> JSONResponse:
    require_authenticated(request)
    existing = get_task_template(template_id)
    current = template_snapshot_from_row(existing)
    current["name"] = str(existing["name"])
    updates = model_to_dict(payload, exclude_unset=True)
    merged = {**current, **updates}
    _, normalized = normalize_template_configuration(
        name=str(merged.get("name") or ""),
        platform=str(merged.get("platform") or ""),
        quantity=int(merged.get("quantity") or 0),
        concurrency=int(merged["concurrency"]) if merged.get("concurrency") is not None else None,
        captcha_credential_id=int(merged["captcha_credential_id"]) if merged.get("captcha_credential_id") is not None else None,
        proxy_mode=str(merged.get("proxy_mode") or "none"),
        proxy_id=int(merged["proxy_id"]) if merged.get("proxy_id") is not None else None,
        email_credential_ids=[int(item) for item in (merged.get("email_credential_ids") or [])],
        cpa_credential_id=int(merged["cpa_credential_id"]) if merged.get("cpa_credential_id") is not None else None,
    )
    execute_no_return(
        """
        UPDATE task_templates
        SET name = ?, platform = ?, quantity = ?, concurrency = ?, captcha_credential_id = ?,
            proxy_mode = ?, proxy_id = ?, cpa_credential_id = ?, email_credential_ids_json = ?,
            requested_config_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            normalized["name"],
            normalized["platform"],
            normalized["quantity"],
            normalized["concurrency"],
            normalized["captcha_credential_id"],
            normalized["proxy_mode"],
            normalized["proxy_id"],
            normalized["cpa_credential_id"],
            json.dumps(normalized["email_credential_ids"], ensure_ascii=False) if normalized["email_credential_ids"] else None,
            json.dumps(normalized, ensure_ascii=False),
            now_iso(),
            template_id,
        ),
    )
    return JSONResponse({"ok": True, "template": serialize_task_template(get_task_template(template_id))})


@app.post("/api/task-templates/{template_id}/enqueue")
async def enqueue_task_template(template_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    template = get_task_template(template_id)
    task_ids = queue_template_instances(template, count=1, source="ui", auto_delete_at=None)
    return JSONResponse({"ok": True, "task_id": task_ids[0], "task_ids": task_ids})


@app.post("/api/task-templates/{template_id}/enqueue-batch")
async def enqueue_task_template_batch(template_id: int, payload: TaskTemplateEnqueueBatch, request: Request) -> JSONResponse:
    require_authenticated(request)
    template = get_task_template(template_id)
    task_ids = queue_template_instances(template, count=int(payload.count), source="ui", auto_delete_at=None)
    return JSONResponse({"ok": True, "task_id": task_ids[0], "task_ids": task_ids})


@app.delete("/api/task-templates/{template_id}")
async def delete_task_template(template_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    template = get_task_template(template_id)
    execute_no_return("DELETE FROM task_templates WHERE id = ?", (template_id,))
    return JSONResponse({"ok": True, "id": int(template["id"])})


@app.get("/api/tasks")
async def list_tasks(request: Request, status: str | None = None, source: str | None = None, template_id: int | None = None, limit: int | None = None) -> JSONResponse:
    require_authenticated(request)
    return JSONResponse({"tasks": query_tasks(status=status, source=source, template_id=template_id, limit=limit)})


@app.post("/api/tasks")
async def create_task(payload: TaskCreate, request: Request) -> JSONResponse:
    require_authenticated(request)
    name, config = resolve_task_configuration(
        name=payload.name,
        platform=payload.platform,
        quantity=payload.quantity,
        concurrency=payload.concurrency,
        captcha_credential_id=payload.captcha_credential_id,
        proxy_mode=payload.proxy_mode,
        proxy_id=payload.proxy_id,
        source="ui",
        auto_delete_at=None,
        email_credential_ids=payload.email_credential_ids,
        cpa_credential_id=payload.cpa_credential_id,
    )
    task_id = insert_task(name=name, config=config)
    return JSONResponse({"ok": True, "id": task_id})


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    return JSONResponse({"task": serialize_task(get_task(task_id))})


@app.get("/api/tasks/{task_id}/runtime")
async def task_runtime(task_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    row = get_task(task_id)
    return JSONResponse({"task_id": task_id, "runtime": task_runtime_payload(row)})


@app.get("/api/tasks/{task_id}/artifacts")
async def task_artifacts(task_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    row = get_task(task_id)
    return JSONResponse({"task_id": task_id, "artifacts": task_artifact_summary(row)})


@app.get("/api/tasks/{task_id}/console")
async def task_console(task_id: int, request: Request, cursor: int = 0) -> JSONResponse:
    require_authenticated(request)
    row = get_task(task_id)
    console_path = Path(row["console_path"])
    if cursor > 0 and console_path.exists():
        content = console_path.read_text(encoding="utf-8", errors="ignore")
        return JSONResponse({"task_id": task_id, "console": content[cursor:], "next_cursor": len(content)})
    if console_path.exists():
        content = console_path.read_text(encoding="utf-8", errors="ignore")
        return JSONResponse({"task_id": task_id, "console": read_tail(console_path), "next_cursor": len(content)})
    return JSONResponse({"task_id": task_id, "console": "", "next_cursor": 0})


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    supervisor.stop_task(task_id)
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    row = get_task(task_id)
    if row["status"] in {"queued", "running", "stopping"}:
        raise HTTPException(status_code=409, detail="Active tasks cannot be retried")
    snapshot = json.loads(row["requested_config_json"])
    auto_delete_at = date_iso(now() + timedelta(hours=24)) if row["source"] == "api" else None
    retry_task_id = create_task_from_snapshot(
        snapshot,
        source=str(row["source"]),
        auto_delete_at=auto_delete_at,
        template_id=int(row["template_id"]) if row["template_id"] is not None else None,
        template_name=str(row["template_name"]) if row["template_name"] is not None else None,
    )
    return JSONResponse({"ok": True, "task_id": retry_task_id})


@app.get("/api/tasks/{task_id}/download")
async def download_task(task_id: int, request: Request) -> FileResponse:
    require_authenticated(request)
    row = get_task(task_id)
    archive_path = create_archive(row)
    return FileResponse(path=archive_path, media_type="application/zip", filename=f"task_{task_id}_{row['platform']}.zip")


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    row = get_task(task_id)
    if row["status"] in {"queued", "running", "stopping"}:
        raise HTTPException(status_code=409, detail="Stop the task before deleting it")
    paths = task_paths(row)
    try:
        shutil.rmtree(paths["task_dir"], ignore_errors=True)
    except Exception:
        pass
    if paths["archive_path"].exists():
        try:
            paths["archive_path"].unlink()
        except Exception:
            pass
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return JSONResponse({"ok": True})


@app.post("/api/api-keys")
async def create_api_key(payload: ApiKeyCreate, request: Request) -> JSONResponse:
    require_authenticated(request)
    raw_key, key_hash, prefix = generate_api_key_secret()
    key_id = execute(
        """
        INSERT INTO api_keys (name, key_hash, key_prefix, is_active, created_at, last_used_at)
        VALUES (?, ?, ?, 1, ?, NULL)
        """,
        (payload.name.strip(), key_hash, prefix, now_iso()),
    )
    return JSONResponse({"ok": True, "id": key_id, "api_key": raw_key})


@app.delete("/api/api-keys/{key_id}")
async def delete_api_key(key_id: int, request: Request) -> JSONResponse:
    require_authenticated(request)
    execute_no_return("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return JSONResponse({"ok": True})


@app.post("/api/external/tasks")
async def external_create_task(payload: ExternalTaskCreate, request: Request) -> JSONResponse:
    require_api_key(request)
    auto_delete_at = date_iso(now() + timedelta(hours=24))
    task_ids: list[int] = []
    if payload.template_id is not None:
        template = get_task_template(int(payload.template_id))
        task_ids = queue_template_instances(template, count=int(payload.count), source="api", auto_delete_at=auto_delete_at)
    else:
        if payload.platform is None or payload.quantity is None:
            raise HTTPException(status_code=400, detail="platform and quantity are required when template_id is not provided")
        proxy_mode = payload.proxy_mode
        if proxy_mode is None:
            proxy_mode = "default" if payload.use_proxy else "none"
        snapshot = {
            "name": payload.name or f"api-{payload.platform}-{now().strftime('%Y%m%d-%H%M%S')}",
            "platform": payload.platform,
            "quantity": payload.quantity,
            "concurrency": payload.concurrency,
            "proxy_mode": proxy_mode,
            "proxy_id": payload.proxy_id,
            "captcha_credential_id": payload.captcha_credential_id,
            "email_credential_ids": payload.email_credential_ids or [],
            "cpa_credential_id": payload.cpa_credential_id,
        }
        for _ in range(int(payload.count)):
            task_ids.append(create_task_from_snapshot(snapshot, source="api", auto_delete_at=auto_delete_at))
    return JSONResponse({"ok": True, "task_id": task_ids[0], "task_ids": task_ids, "auto_delete_at": auto_delete_at})


@app.get("/api/external/tasks/{task_id}")
async def external_task_status(task_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    row = get_task(task_id)
    if row["source"] != "api":
        raise HTTPException(status_code=404, detail="API task not found")
    return JSONResponse(api_task_status_payload(row))


@app.get("/api/external/tasks/{task_id}/download")
async def external_download_task(task_id: int, request: Request) -> FileResponse:
    require_api_key(request)
    row = get_task(task_id)
    if row["source"] != "api":
        raise HTTPException(status_code=404, detail="API task not found")
    archive_path = create_archive(row)
    return FileResponse(path=archive_path, media_type="application/zip", filename=f"api_task_{task_id}_{row['platform']}.zip")


@app.get("/api/v1/task-templates")
async def api_v1_list_task_templates(request: Request) -> JSONResponse:
    require_api_key(request)
    return JSONResponse({"templates": get_task_templates()})


@app.get("/api/v1/task-templates/{template_id}")
async def api_v1_task_template_detail(template_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    return JSONResponse({"template": serialize_task_template(get_task_template(template_id))})


@app.post("/api/v1/task-templates")
async def api_v1_create_task_template(payload: TaskTemplateCreate, request: Request) -> JSONResponse:
    require_api_key(request)
    _, normalized = normalize_template_configuration(
        name=payload.name,
        platform=payload.platform,
        quantity=payload.quantity,
        concurrency=payload.concurrency,
        captcha_credential_id=payload.captcha_credential_id,
        proxy_mode=payload.proxy_mode,
        proxy_id=payload.proxy_id,
        email_credential_ids=payload.email_credential_ids,
        cpa_credential_id=payload.cpa_credential_id,
    )
    template_id = insert_task_template(payload=normalized)
    return JSONResponse({"ok": True, "template_id": template_id})


@app.patch("/api/v1/task-templates/{template_id}")
async def api_v1_update_task_template(template_id: int, payload: TaskTemplateUpdate, request: Request) -> JSONResponse:
    require_api_key(request)
    existing = get_task_template(template_id)
    current = template_snapshot_from_row(existing)
    current["name"] = str(existing["name"])
    updates = model_to_dict(payload, exclude_unset=True)
    merged = {**current, **updates}
    _, normalized = normalize_template_configuration(
        name=str(merged.get("name") or ""),
        platform=str(merged.get("platform") or ""),
        quantity=int(merged.get("quantity") or 0),
        concurrency=int(merged["concurrency"]) if merged.get("concurrency") is not None else None,
        captcha_credential_id=int(merged["captcha_credential_id"]) if merged.get("captcha_credential_id") is not None else None,
        proxy_mode=str(merged.get("proxy_mode") or "none"),
        proxy_id=int(merged["proxy_id"]) if merged.get("proxy_id") is not None else None,
        email_credential_ids=[int(item) for item in (merged.get("email_credential_ids") or [])],
        cpa_credential_id=int(merged["cpa_credential_id"]) if merged.get("cpa_credential_id") is not None else None,
    )
    execute_no_return(
        """
        UPDATE task_templates
        SET name = ?, platform = ?, quantity = ?, concurrency = ?, captcha_credential_id = ?,
            proxy_mode = ?, proxy_id = ?, cpa_credential_id = ?, email_credential_ids_json = ?,
            requested_config_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            normalized["name"],
            normalized["platform"],
            normalized["quantity"],
            normalized["concurrency"],
            normalized["captcha_credential_id"],
            normalized["proxy_mode"],
            normalized["proxy_id"],
            normalized["cpa_credential_id"],
            json.dumps(normalized["email_credential_ids"], ensure_ascii=False) if normalized["email_credential_ids"] else None,
            json.dumps(normalized, ensure_ascii=False),
            now_iso(),
            template_id,
        ),
    )
    return JSONResponse({"ok": True, "template": serialize_task_template(get_task_template(template_id))})


@app.post("/api/v1/task-templates/{template_id}/enqueue")
async def api_v1_enqueue_task_template(template_id: int, payload: TaskTemplateEnqueueBatch, request: Request) -> JSONResponse:
    require_api_key(request)
    template = get_task_template(template_id)
    task_ids = queue_template_instances(template, count=int(payload.count), source="api", auto_delete_at=date_iso(now() + timedelta(hours=24)))
    return JSONResponse({"ok": True, "task_id": task_ids[0], "task_ids": task_ids})


@app.delete("/api/v1/task-templates/{template_id}")
async def api_v1_delete_task_template(template_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    template = get_task_template(template_id)
    execute_no_return("DELETE FROM task_templates WHERE id = ?", (template_id,))
    return JSONResponse({"ok": True, "id": int(template["id"])})


@app.get("/api/v1/tasks")
async def api_v1_list_tasks(request: Request, status: str | None = None, source: str | None = None, template_id: int | None = None, limit: int | None = None) -> JSONResponse:
    require_api_key(request)
    return JSONResponse({"tasks": query_tasks(status=status, source=source, template_id=template_id, limit=limit)})


@app.get("/api/v1/tasks/{task_id}")
async def api_v1_task_status(task_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    row = get_task(task_id)
    return JSONResponse({"task": serialize_task(row), "runtime": task_runtime_payload(row)})


@app.get("/api/v1/tasks/{task_id}/console")
async def api_v1_task_console(task_id: int, request: Request, cursor: int = 0) -> JSONResponse:
    require_api_key(request)
    row = get_task(task_id)
    console_path = Path(row["console_path"])
    if cursor > 0 and console_path.exists():
        content = console_path.read_text(encoding="utf-8", errors="ignore")
        return JSONResponse({"task_id": task_id, "console": content[cursor:], "next_cursor": len(content)})
    if console_path.exists():
        content = console_path.read_text(encoding="utf-8", errors="ignore")
        return JSONResponse({"task_id": task_id, "console": read_tail(console_path), "next_cursor": len(content)})
    return JSONResponse({"task_id": task_id, "console": "", "next_cursor": 0})


@app.get("/api/v1/tasks/{task_id}/artifacts")
async def api_v1_task_artifacts(task_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    return JSONResponse({"task_id": task_id, "artifacts": task_artifact_summary(get_task(task_id))})


@app.post("/api/v1/tasks/{task_id}/retry")
async def api_v1_retry_task(task_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    row = get_task(task_id)
    if row["status"] in {"queued", "running", "stopping"}:
        raise HTTPException(status_code=409, detail="Active tasks cannot be retried")
    snapshot = json.loads(row["requested_config_json"])
    retry_task_id = create_task_from_snapshot(
        snapshot,
        source=str(row["source"]),
        auto_delete_at=date_iso(now() + timedelta(hours=24)) if row["source"] == "api" else None,
        template_id=int(row["template_id"]) if row["template_id"] is not None else None,
        template_name=str(row["template_name"]) if row["template_name"] is not None else None,
    )
    return JSONResponse({"ok": True, "task_id": retry_task_id})


@app.post("/api/v1/tasks/{task_id}/stop")
async def api_v1_stop_task(task_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    supervisor.stop_task(task_id)
    return JSONResponse({"ok": True})


@app.delete("/api/v1/tasks/{task_id}")
async def api_v1_delete_task(task_id: int, request: Request) -> JSONResponse:
    require_api_key(request)
    row = get_task(task_id)
    if row["status"] in {"queued", "running", "stopping"}:
        raise HTTPException(status_code=409, detail="Stop the task before deleting it")
    paths = task_paths(row)
    try:
        shutil.rmtree(paths["task_dir"], ignore_errors=True)
    except Exception:
        pass
    if paths["archive_path"].exists():
        try:
            paths["archive_path"].unlink()
        except Exception:
            pass
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return JSONResponse({"ok": True})


@app.get("/api/v1/tasks/{task_id}/download")
async def api_v1_download_task(task_id: int, request: Request) -> FileResponse:
    require_api_key(request)
    row = get_task(task_id)
    archive_path = create_archive(row)
    return FileResponse(path=archive_path, media_type="application/zip", filename=f"task_{task_id}_{row['platform']}.zip")
