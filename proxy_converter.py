# -*- coding: utf-8 -*-
"""代理转换模块 - 将 SS/VLESS 等协议转换为 HTTP 代理"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional


# gost 二进制路径（Docker 容器内）
GOST_BIN = "/usr/local/bin/gost"
# 本地代理端口池起始端口
LOCAL_PORT_START = 20000
# 已启动的代理进程 {node_id: {"process": Popen, "port": int, "url": str}}
_running_proxies: dict[int, dict[str, Any]] = {}


def is_gost_available() -> bool:
    """检查 gost 是否可用"""
    return os.path.exists(GOST_BIN) or os.path.exists("/usr/bin/gost")


def get_gost_bin() -> str:
    """获取 gost 二进制路径"""
    if os.path.exists(GOST_BIN):
        return GOST_BIN
    if os.path.exists("/usr/bin/gost"):
        return "/usr/bin/gost"
    return "gost"


def build_ss_forward_url(config: dict[str, Any]) -> str:
    """构建 SS 转发 URL

    格式: ss://method:password@server:port
    或 ss://base64(method:password)@server:port
    """
    import base64

    method = config.get("method", "")
    password = config.get("password", "")

    server = config.get("server", "")
    port = config.get("port", 443)

    # SS 2022 格式特殊处理
    if method.startswith("2022-"):
        # SS 2022 格式: base64(method:password)
        userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
        return f"ss://{userinfo}@{server}:{port}"

    return f"ss://{method}:{password}@{server}:{port}"


def build_vless_forward_url(config: dict[str, Any]) -> str:
    """构建 VLESS 转发 URL

    格式: vless://uuid@server:port?params
    """
    uuid = config.get("uuid", "")
    server = config.get("server", "")
    port = config.get("port", 443)

    params = []
    if config.get("encryption"):
        params.append(f"encryption={config['encryption']}")
    if config.get("security"):
        params.append(f"security={config['security']}")
    if config.get("sni"):
        params.append(f"sni={config['sni']}")
    if config.get("type"):
        params.append(f"type={config['type']}")
    if config.get("flow"):
        params.append(f"flow={config['flow']}")

    query = "&".join(params) if params else ""
    url = f"vless://{uuid}@{server}:{port}"
    if query:
        url += f"?{query}"

    return url


def build_vmess_forward_url(config: dict[str, Any]) -> str:
    """构建 VMess 转发 URL

    格式: vmess://base64(json)
    """
    import base64

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

    json_str = json.dumps(vmess_config, separators=(",", ":"))
    encoded = base64.b64encode(json_str.encode()).decode()
    return f"vmess://{encoded}"


def build_trojan_forward_url(config: dict[str, Any]) -> str:
    """构建 Trojan 转发 URL

    格式: trojan://password@server:port?params
    """
    password = config.get("password", "")
    server = config.get("server", "")
    port = config.get("port", 443)

    params = []
    if config.get("sni"):
        params.append(f"sni={config['sni']}")
    if config.get("type"):
        params.append(f"type={config['type']}")

    query = "&".join(params) if params else ""
    url = f"trojan://{password}@{server}:{port}"
    if query:
        url += f"?{query}"

    return url


def build_forward_url(protocol: str, config: dict[str, Any], server: str, port: int) -> str:
    """构建转发 URL"""
    # 确保配置中有服务器和端口
    full_config = {**config, "server": server, "port": port}

    if protocol == "ss":
        return build_ss_forward_url(full_config)
    elif protocol == "vless":
        return build_vless_forward_url(full_config)
    elif protocol == "vmess":
        return build_vmess_forward_url(full_config)
    elif protocol == "trojan":
        return build_trojan_forward_url(full_config)
    elif protocol in ("http", "socks5"):
        # HTTP/SOCKS5 直接返回
        auth = ""
        if full_config.get("username") and full_config.get("password"):
            auth = f"{full_config['username']}:{full_config['password']}@"
        return f"{protocol}://{auth}{server}:{port}"

    raise ValueError(f"Unsupported protocol: {protocol}")


def allocate_port() -> int:
    """分配一个本地端口"""
    global LOCAL_PORT_START
    port = LOCAL_PORT_START
    LOCAL_PORT_START += 1
    return port


def start_proxy_for_node(node_id: int, protocol: str, config: dict[str, Any], server: str, port: int) -> Optional[str]:
    """为节点启动一个本地 HTTP 代理

    返回: 代理 URL (如 http://127.0.0.1:20000) 或 None
    """
    if not is_gost_available():
        print(f"[Warning] gost not available, cannot start proxy for node {node_id}")
        return None

    # 如果已经启动，返回现有的
    if node_id in _running_proxies:
        proc_info = _running_proxies[node_id]
        if proc_info["process"].poll() is None:
            return proc_info["url"]
        else:
            # 进程已退出，清理
            del _running_proxies[node_id]

    try:
        forward_url = build_forward_url(protocol, config, server, port)
        local_port = allocate_port()
        gost_bin = get_gost_bin()

        # gost 命令: gost -L=:local_port -F=forward_url
        # -L: 本地监听地址
        # -F: 转发目标
        cmd = [
            gost_bin,
            "-L", f":{local_port}",
            "-F", forward_url,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 等待一下让进程启动
        time.sleep(0.5)

        if proc.poll() is not None:
            print(f"[Error] gost process exited immediately for node {node_id}")
            return None

        proxy_url = f"http://127.0.0.1:{local_port}"
        _running_proxies[node_id] = {
            "process": proc,
            "port": local_port,
            "url": proxy_url,
        }

        print(f"[Info] Started proxy for node {node_id} on {proxy_url}")
        return proxy_url

    except Exception as e:
        print(f"[Error] Failed to start proxy for node {node_id}: {e}")
        return None


def stop_proxy_for_node(node_id: int) -> None:
    """停止节点的代理"""
    if node_id in _running_proxies:
        proc_info = _running_proxies[node_id]
        try:
            proc_info["process"].terminate()
            proc_info["process"].wait(timeout=5)
        except Exception:
            proc_info["process"].kill()
        del _running_proxies[node_id]
        print(f"[Info] Stopped proxy for node {node_id}")


def stop_all_proxies() -> None:
    """停止所有代理"""
    for node_id in list(_running_proxies.keys()):
        stop_proxy_for_node(node_id)


def get_running_proxy_url(node_id: int) -> Optional[str]:
    """获取正在运行的代理 URL"""
    if node_id in _running_proxies:
        proc_info = _running_proxies[node_id]
        if proc_info["process"].poll() is None:
            return proc_info["url"]
    return None


def resolve_node_proxy_url(node_id: int, protocol: str, config: dict[str, Any], server: str, port: int) -> Optional[str]:
    """解析节点的代理 URL

    如果是 HTTP/SOCKS5 协议，直接返回原始地址
    如果是 SS/VLESS 等协议，启动本地代理
    """
    # HTTP 和 SOCKS5 可以直接使用
    if protocol in ("http", "socks5"):
        auth = ""
        if config.get("username") and config.get("password"):
            auth = f"{config['username']}:{config['password']}@"
        return f"{protocol}://{auth}{server}:{port}"

    # 其他协议需要通过 gost 转换
    return start_proxy_for_node(node_id, protocol, config, server, port)
