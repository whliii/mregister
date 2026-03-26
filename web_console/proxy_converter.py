# -*- coding: utf-8 -*-
"""Proxy bridge helpers for subscription nodes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests


GOST_BIN = "/usr/local/bin/gost"
SING_BOX_BIN = "/usr/local/bin/sing-box"
LOCAL_PORT_START = 20000
RUNTIME_PROXY_DIR = Path(os.getenv("MREGISTER_PROXY_RUNTIME_DIR", "/tmp/mregister-proxies"))
_running_proxies: dict[str, dict[str, Any]] = {}
_node_proxy_keys: dict[int, str] = {}


def is_gost_available() -> bool:
    return os.path.exists(GOST_BIN) or os.path.exists("/usr/bin/gost")


def get_gost_bin() -> str:
    if os.path.exists(GOST_BIN):
        return GOST_BIN
    if os.path.exists("/usr/bin/gost"):
        return "/usr/bin/gost"
    return "gost"


def is_sing_box_available() -> bool:
    return os.path.exists(SING_BOX_BIN) or os.path.exists("/usr/bin/sing-box")


def get_sing_box_bin() -> str:
    if os.path.exists(SING_BOX_BIN):
        return SING_BOX_BIN
    if os.path.exists("/usr/bin/sing-box"):
        return "/usr/bin/sing-box"
    return "sing-box"


def build_ss_forward_url(config: dict[str, Any]) -> str:
    method = config.get("method", "")
    password = config.get("password", "")
    server = config.get("server", "")
    port = config.get("port", 443)

    if method.startswith("2022-"):
        userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
        return f"ss://{userinfo}@{server}:{port}"

    return f"ss://{method}:{password}@{server}:{port}"


def build_vless_forward_url(config: dict[str, Any]) -> str:
    uuid = config.get("uuid", "")
    server = config.get("server", "")
    port = config.get("port", 443)
    params = []
    for key in ("encryption", "security", "sni", "type", "flow", "fp", "allowInsecure", "host", "path"):
        value = config.get(key)
        if value not in (None, "", False):
            params.append(f"{key}={value}")
    query = "&".join(params)
    url = f"vless://{uuid}@{server}:{port}"
    if query:
        url += f"?{query}"
    return url


def build_vmess_forward_url(config: dict[str, Any]) -> str:
    vmess_config = {
        "v": "2",
        "add": config.get("server", ""),
        "port": str(config.get("port", 443)),
        "id": config.get("uuid", ""),
        "aid": str(config.get("alterId", 0)),
        "net": config.get("net", "tcp"),
        "type": config.get("type", "none"),
        "host": config.get("host", ""),
        "path": config.get("path", ""),
        "tls": config.get("tls", ""),
        "sni": config.get("sni", ""),
    }
    encoded = base64.b64encode(json.dumps(vmess_config, separators=(",", ":")).encode()).decode()
    return f"vmess://{encoded}"


def build_trojan_forward_url(config: dict[str, Any]) -> str:
    password = config.get("password", "")
    server = config.get("server", "")
    port = config.get("port", 443)
    params = []
    for key in ("sni", "type", "host", "path"):
        value = config.get(key)
        if value not in (None, ""):
            params.append(f"{key}={value}")
    query = "&".join(params)
    url = f"trojan://{password}@{server}:{port}"
    if query:
        url += f"?{query}"
    return url


def build_forward_url(protocol: str, config: dict[str, Any], server: str, port: int) -> str:
    full_config = {**config, "server": server, "port": port}
    if protocol == "ss":
        return build_ss_forward_url(full_config)
    if protocol == "vless":
        return build_vless_forward_url(full_config)
    if protocol == "vmess":
        return build_vmess_forward_url(full_config)
    if protocol == "trojan":
        return build_trojan_forward_url(full_config)
    if protocol in ("http", "socks5"):
        auth = ""
        if full_config.get("username") and full_config.get("password"):
            auth = f"{full_config['username']}:{full_config['password']}@"
        return f"{protocol}://{auth}{server}:{port}"
    raise ValueError(f"Unsupported protocol: {protocol}")


def allocate_port() -> int:
    global LOCAL_PORT_START
    port = LOCAL_PORT_START
    LOCAL_PORT_START += 1
    return port


def _build_proxy_key(protocol: str, config: dict[str, Any], server: str, port: int) -> str:
    normalized_config = json.dumps(config or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{protocol.lower()}|{server}|{port}|{normalized_config}"


def _config_filename_for_key(prefix: str, proxy_key: str) -> str:
    digest = hashlib.sha256(proxy_key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}.json"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _build_tls_config(config: dict[str, Any], default_enabled: bool = False, default_server_name: str = "") -> dict[str, Any] | None:
    enabled = default_enabled or str(config.get("security") or "").lower() == "tls" or str(config.get("tls") or "").lower() == "tls"
    if not enabled:
        return None
    tls: dict[str, Any] = {"enabled": True}
    server_name = str(config.get("sni") or config.get("host") or default_server_name or "").strip()
    if server_name:
        tls["server_name"] = server_name
    if _as_bool(config.get("allowInsecure")):
        tls["insecure"] = True
    fingerprint = str(config.get("fp") or config.get("fingerprint") or "").strip()
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    return tls


def _build_transport_config(config: dict[str, Any]) -> dict[str, Any] | None:
    transport_type = str(config.get("type") or config.get("net") or "").strip().lower()
    if not transport_type or transport_type in {"tcp", "none"}:
        return None
    transport: dict[str, Any] = {"type": transport_type}
    path = str(config.get("path") or "").strip()
    host = str(config.get("host") or "").strip()
    if transport_type == "ws":
        if path:
            transport["path"] = path
        if host:
            transport["headers"] = {"Host": [host]}
    elif transport_type in {"httpupgrade", "http"}:
        if host:
            transport["host"] = [host]
        if path:
            transport["path"] = path
    elif transport_type == "grpc":
        if path:
            transport["service_name"] = path.lstrip("/")
    return transport


def _build_sing_box_outbound(protocol: str, config: dict[str, Any], server: str, port: int) -> dict[str, Any]:
    protocol = protocol.lower()
    if protocol == "ss":
        outbound: dict[str, Any] = {
            "type": "shadowsocks",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "method": config.get("method", ""),
            "password": config.get("password", ""),
        }
        plugin = str(config.get("plugin") or "").strip()
        if plugin:
            outbound["plugin"] = plugin
            if config.get("plugin-opts"):
                outbound["plugin_opts"] = config.get("plugin-opts")
        return outbound

    if protocol == "vless":
        outbound = {
            "type": "vless",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "uuid": config.get("uuid", ""),
        }
        flow = str(config.get("flow") or "").strip()
        if flow:
            outbound["flow"] = flow
        tls = _build_tls_config(config, default_enabled=False, default_server_name=server)
        if tls:
            outbound["tls"] = tls
        transport = _build_transport_config(config)
        if transport:
            outbound["transport"] = transport
        return outbound

    if protocol == "vmess":
        outbound = {
            "type": "vmess",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "uuid": config.get("uuid", ""),
            "security": config.get("security", "auto"),
            "alter_id": int(config.get("alterId", 0) or 0),
        }
        tls = _build_tls_config(config, default_enabled=False, default_server_name=server)
        if tls:
            outbound["tls"] = tls
        transport = _build_transport_config(config)
        if transport:
            outbound["transport"] = transport
        return outbound

    if protocol == "trojan":
        outbound = {
            "type": "trojan",
            "tag": "proxy-out",
            "server": server,
            "server_port": port,
            "password": config.get("password", ""),
        }
        tls = _build_tls_config(config, default_enabled=True, default_server_name=server)
        if tls:
            outbound["tls"] = tls
        transport = _build_transport_config(config)
        if transport:
            outbound["transport"] = transport
        return outbound

    raise ValueError(f"Unsupported sing-box outbound protocol: {protocol}")


def _build_sing_box_config(protocol: str, config: dict[str, Any], server: str, port: int, local_port: int) -> dict[str, Any]:
    return {
        "log": {"level": "error"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": local_port,
            }
        ],
        "outbounds": [
            _build_sing_box_outbound(protocol, config, server, port),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "final": "proxy-out",
            "auto_detect_interface": True,
        },
    }


def _ensure_runtime_dir() -> None:
    RUNTIME_PROXY_DIR.mkdir(parents=True, exist_ok=True)


def _stop_proxy_by_key(proxy_key: str) -> None:
    if proxy_key not in _running_proxies:
        return
    proc_info = _running_proxies[proxy_key]
    if proc_info["process"].poll() is None:
        try:
            proc_info["process"].terminate()
            proc_info["process"].wait(timeout=5)
        except Exception:
            proc_info["process"].kill()
    config_path = proc_info.get("config_path")
    if config_path:
        try:
            Path(config_path).unlink(missing_ok=True)
        except Exception:
            pass
    for alias_node_id in list(proc_info.get("aliases") or set()):
        _node_proxy_keys.pop(int(alias_node_id), None)
    del _running_proxies[proxy_key]


def _stop_existing_proxy(node_id: int) -> None:
    proxy_key = _node_proxy_keys.get(node_id)
    if not proxy_key:
        return
    _stop_proxy_by_key(proxy_key)


def _start_sing_box_proxy(node_id: int, proxy_key: str, protocol: str, config: dict[str, Any], server: str, port: int) -> Optional[str]:
    if not is_sing_box_available():
        return None
    _ensure_runtime_dir()
    local_port = allocate_port()
    proxy_url = f"http://127.0.0.1:{local_port}"
    config_path = RUNTIME_PROXY_DIR / _config_filename_for_key("node", proxy_key)
    config_path.write_text(
        json.dumps(_build_sing_box_config(protocol, config, server, port, local_port), ensure_ascii=False),
        encoding="utf-8",
    )
    cmd = [get_sing_box_bin(), "run", "-c", str(config_path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    if proc.poll() is not None:
        try:
            config_path.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"[Error] sing-box process exited immediately for node {node_id}")
        return None
    _running_proxies[proxy_key] = {
        "process": proc,
        "port": local_port,
        "url": proxy_url,
        "backend": "sing-box",
        "config_path": str(config_path),
        "aliases": {int(node_id)},
    }
    _node_proxy_keys[int(node_id)] = proxy_key
    print(f"[Info] Started sing-box proxy for node {node_id} on {proxy_url}")
    return proxy_url


def _start_gost_proxy(node_id: int, proxy_key: str, protocol: str, config: dict[str, Any], server: str, port: int) -> Optional[str]:
    if not is_gost_available():
        return None
    forward_url = build_forward_url(protocol, config, server, port)
    local_port = allocate_port()
    proxy_url = f"http://127.0.0.1:{local_port}"
    cmd = [get_gost_bin(), "-L", proxy_url, "-F", forward_url]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"[Error] gost process exited immediately for node {node_id}")
        return None
    _running_proxies[proxy_key] = {
        "process": proc,
        "port": local_port,
        "url": proxy_url,
        "backend": "gost",
        "aliases": {int(node_id)},
    }
    _node_proxy_keys[int(node_id)] = proxy_key
    print(f"[Info] Started gost proxy for node {node_id} on {proxy_url}")
    return proxy_url


def start_proxy_for_node(node_id: int, protocol: str, config: dict[str, Any], server: str, port: int) -> Optional[str]:
    proxy_key = _build_proxy_key(protocol, config, server, port)
    if proxy_key in _running_proxies:
        proc_info = _running_proxies[proxy_key]
        if proc_info["process"].poll() is None:
            proc_info.setdefault("aliases", set()).add(int(node_id))
            _node_proxy_keys[int(node_id)] = proxy_key
            return proc_info["url"]
        _stop_proxy_by_key(proxy_key)
    _stop_existing_proxy(node_id)

    protocol = protocol.lower()
    if protocol in {"ss", "vless", "vmess", "trojan"}:
        proxy_url = _start_sing_box_proxy(node_id, proxy_key, protocol, config, server, port)
        if proxy_url:
            return proxy_url
    return _start_gost_proxy(node_id, proxy_key, protocol, config, server, port)


def stop_proxy_for_node(node_id: int) -> None:
    proxy_key = _node_proxy_keys.get(node_id)
    if not proxy_key:
        return
    _stop_proxy_by_key(proxy_key)
    print(f"[Info] Stopped proxy for node {node_id}")


def stop_all_proxies() -> None:
    for proxy_key in list(_running_proxies.keys()):
        _stop_proxy_by_key(proxy_key)


def get_running_proxy_url(node_id: int) -> Optional[str]:
    proxy_key = _node_proxy_keys.get(node_id)
    if proxy_key in _running_proxies:
        proc_info = _running_proxies[proxy_key]
        if proc_info["process"].poll() is None:
            return proc_info["url"]
    return None


def stop_proxy_for_url(proxy_url: str) -> None:
    target = str(proxy_url or "").strip()
    if not target:
        return
    for proxy_key, proc_info in list(_running_proxies.items()):
        if str(proc_info.get("url") or "").strip() == target:
            _stop_proxy_by_key(proxy_key)
            break


def resolve_node_proxy_url(node_id: int, protocol: str, config: dict[str, Any], server: str, port: int) -> Optional[str]:
    if protocol in ("http", "socks5"):
        auth = ""
        if config.get("username") and config.get("password"):
            auth = f"{config['username']}:{config['password']}@"
        return f"{protocol}://{auth}{server}:{port}"
    return start_proxy_for_node(node_id, protocol, config, server, port)


def probe_proxy_url(proxy_url: str, timeout: float = 12.0) -> dict[str, Any]:
    started = time.perf_counter()
    response = requests.get(
        "https://api.ipify.org?format=json",
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=timeout,
        impersonate="chrome124",
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    payload = response.json() if "application/json" in (response.headers.get("content-type") or "") else {"raw": response.text}
    if response.status_code != 200:
        raise RuntimeError(f"Proxy probe returned HTTP {response.status_code}")
    return {
        "status_code": response.status_code,
        "latency_ms": latency_ms,
        "payload": payload,
    }
