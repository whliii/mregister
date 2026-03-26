# -*- coding: utf-8 -*-
"""订阅链接解析模块，支持 SS、VLESS、VMess、Trojan 等协议"""

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any
try:
    import yaml
except Exception:
    yaml = None


@dataclass
class ProxyNode:
    """代理节点数据结构"""
    name: str
    server: str
    port: int
    protocol: str  # ss, vless, vmess, trojan, http, socks5
    config: dict[str, Any]  # 完整配置
    country: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "server": self.server,
            "port": self.port,
            "protocol": self.protocol,
            "config": self.config,
            "country": self.country,
        }


def parse_subscription(content: str) -> list[ProxyNode]:
    """解析订阅内容，自动识别格式"""
    content = content.strip()

    decoded = _decode_subscription_text(content)

    nodes = _parse_structured_content(decoded)
    if nodes:
        return nodes

    nodes = []

    # 按行解析
    for line in decoded.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("REMARKS=") or line.startswith("STATUS="):
            continue

        node = parse_uri(line)
        if node:
            nodes.append(node)

    return nodes


def _decode_subscription_text(content: str) -> str:
    if not content:
        return ""
    if content.startswith(("ss://", "vless://", "vmess://", "trojan://", "socks5://", "http://", "https://", "REMARKS=", "{", "[")):
        return content

    raw = re.sub(r"\s+", "", content)
    if not raw:
        return content
    try:
        pad = "=" * ((4 - (len(raw) % 4)) % 4)
        decoded = base64.b64decode(raw + pad).decode("utf-8")
        return decoded.strip() or content
    except Exception:
        return content


def _parse_structured_content(content: str) -> list[ProxyNode]:
    text = (content or "").strip()
    if not text:
        return []

    json_nodes = _parse_json_subscription(text)
    if json_nodes:
        return json_nodes

    yaml_nodes = _parse_yaml_subscription(text)
    if yaml_nodes:
        return yaml_nodes

    return []


def _parse_json_subscription(content: str) -> list[ProxyNode]:
    try:
        data = json.loads(content)
    except Exception:
        return []

    if isinstance(data, dict) and isinstance(data.get("outbounds"), list):
        return _parse_singbox_outbounds(data["outbounds"])
    return []


def _parse_yaml_subscription(content: str) -> list[ProxyNode]:
    if yaml is None:
        return []
    try:
        data = yaml.safe_load(content)
    except Exception:
        return []

    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        return _parse_clash_proxies(data["proxies"])
    return []


def _parse_clash_proxies(items: list[Any]) -> list[ProxyNode]:
    nodes: list[ProxyNode] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        node = _clash_proxy_to_node(item)
        if node is not None:
            nodes.append(node)
    return nodes


def _clash_proxy_to_node(item: dict[str, Any]) -> ProxyNode | None:
    proxy_type = str(item.get("type") or "").strip().lower()
    name = str(item.get("name") or f"{proxy_type.upper()} Node").strip()
    server = str(item.get("server") or "").strip()
    port = int(item.get("port") or 0)
    if not proxy_type or not server or not port:
        return None

    if proxy_type == "ss":
        return ProxyNode(
            name=name,
            server=server,
            port=port,
            protocol="ss",
            config={
                "method": item.get("cipher", ""),
                "password": item.get("password", ""),
                "plugin": item.get("plugin", ""),
                "plugin-opts": item.get("plugin-opts", {}),
                "udp": item.get("udp", True),
            },
            country=extract_country(name),
        )
    if proxy_type == "vmess":
        return ProxyNode(
            name=name,
            server=server,
            port=port,
            protocol="vmess",
            config={
                "uuid": item.get("uuid", ""),
                "alterId": int(item.get("alterId", 0) or 0),
                "security": item.get("cipher", "auto"),
                "net": item.get("network", "tcp"),
                "type": item.get("type", "none"),
                "host": item.get("servername") or item.get("host", ""),
                "path": item.get("path", ""),
                "tls": "tls" if item.get("tls") else "",
                "sni": item.get("servername", ""),
            },
            country=extract_country(name),
        )
    if proxy_type == "vless":
        return ProxyNode(
            name=name,
            server=server,
            port=port,
            protocol="vless",
            config={
                "uuid": item.get("uuid", ""),
                "encryption": item.get("encryption", "none"),
                "flow": item.get("flow", ""),
                "security": "tls" if item.get("tls") else item.get("security", "none"),
                "sni": item.get("servername", ""),
                "type": item.get("network", "tcp"),
                "host": item.get("host", ""),
                "path": item.get("path", ""),
            },
            country=extract_country(name),
        )
    if proxy_type == "trojan":
        return ProxyNode(
            name=name,
            server=server,
            port=port,
            protocol="trojan",
            config={
                "password": item.get("password", ""),
                "sni": item.get("sni") or item.get("servername", server),
                "type": item.get("network", "tcp"),
                "host": item.get("host", ""),
                "path": item.get("path", ""),
            },
            country=extract_country(name),
        )
    if proxy_type in {"http", "https"}:
        config: dict[str, Any] = {"scheme": "http" if proxy_type == "http" else "https"}
        if item.get("username"):
            config["username"] = item.get("username")
        if item.get("password"):
            config["password"] = item.get("password")
        return ProxyNode(name=name, server=server, port=port, protocol="http", config=config, country=extract_country(name))
    if proxy_type == "socks5":
        config = {}
        if item.get("username"):
            config["username"] = item.get("username")
        if item.get("password"):
            config["password"] = item.get("password")
        return ProxyNode(name=name, server=server, port=port, protocol="socks5", config=config, country=extract_country(name))
    return None


def _parse_singbox_outbounds(items: list[Any]) -> list[ProxyNode]:
    nodes: list[ProxyNode] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        node = _singbox_outbound_to_node(item)
        if node is not None:
            nodes.append(node)
    return nodes


def _singbox_outbound_to_node(item: dict[str, Any]) -> ProxyNode | None:
    outbound_type = str(item.get("type") or "").strip().lower()
    tag = str(item.get("tag") or f"{outbound_type.upper()} Node").strip()
    server = str(item.get("server") or "").strip()
    port = int(item.get("server_port") or item.get("port") or 0)

    if outbound_type in {"direct", "block", "dns", "selector", "urltest"}:
        return None
    if not outbound_type or not server or not port:
        return None

    if outbound_type == "shadowsocks":
        return ProxyNode(
            name=tag,
            server=server,
            port=port,
            protocol="ss",
            config={
                "method": item.get("method", ""),
                "password": item.get("password", ""),
                "plugin": item.get("plugin", ""),
            },
            country=extract_country(tag),
        )
    if outbound_type == "vmess":
        transport = item.get("transport") or {}
        tls = item.get("tls") or {}
        return ProxyNode(
            name=tag,
            server=server,
            port=port,
            protocol="vmess",
            config={
                "uuid": item.get("uuid", ""),
                "alterId": int(item.get("alter_id", 0) or 0),
                "security": item.get("security", "auto"),
                "net": transport.get("type", "tcp"),
                "type": transport.get("type", "none"),
                "host": transport.get("host", ""),
                "path": transport.get("path", ""),
                "tls": "tls" if tls.get("enabled") else "",
                "sni": tls.get("server_name", ""),
            },
            country=extract_country(tag),
        )
    if outbound_type == "vless":
        transport = item.get("transport") or {}
        tls = item.get("tls") or {}
        return ProxyNode(
            name=tag,
            server=server,
            port=port,
            protocol="vless",
            config={
                "uuid": item.get("uuid", ""),
                "encryption": item.get("encryption", "none"),
                "flow": item.get("flow", ""),
                "security": "tls" if tls.get("enabled") else "none",
                "sni": tls.get("server_name", ""),
                "type": transport.get("type", "tcp"),
                "host": transport.get("host", ""),
                "path": transport.get("path", ""),
            },
            country=extract_country(tag),
        )
    if outbound_type == "trojan":
        transport = item.get("transport") or {}
        tls = item.get("tls") or {}
        return ProxyNode(
            name=tag,
            server=server,
            port=port,
            protocol="trojan",
            config={
                "password": item.get("password", ""),
                "sni": tls.get("server_name", server),
                "type": transport.get("type", "tcp"),
                "host": transport.get("host", ""),
                "path": transport.get("path", ""),
            },
            country=extract_country(tag),
        )
    if outbound_type == "http":
        config: dict[str, Any] = {"scheme": "http"}
        if item.get("username"):
            config["username"] = item.get("username")
        if item.get("password"):
            config["password"] = item.get("password")
        return ProxyNode(name=tag, server=server, port=port, protocol="http", config=config, country=extract_country(tag))
    if outbound_type == "socks":
        config = {}
        if item.get("username"):
            config["username"] = item.get("username")
        if item.get("password"):
            config["password"] = item.get("password")
        return ProxyNode(name=tag, server=server, port=port, protocol="socks5", config=config, country=extract_country(tag))
    return None


def parse_uri(uri: str) -> ProxyNode | None:
    """解析单个代理 URI"""
    uri = uri.strip()
    if not uri:
        return None

    try:
        if uri.startswith("ss://"):
            return parse_ss(uri)
        elif uri.startswith("vless://"):
            return parse_vless(uri)
        elif uri.startswith("vmess://"):
            return parse_vmess(uri)
        elif uri.startswith("trojan://"):
            return parse_trojan(uri)
        elif uri.startswith("http://") or uri.startswith("https://"):
            return parse_http(uri)
        elif uri.startswith("socks5://"):
            return parse_socks5(uri)
    except Exception:
        pass

    return None


def parse_ss(uri: str) -> ProxyNode | None:
    """解析 Shadowsocks URI
    格式: ss://BASE64(method:password)@server:port#name
    或: ss://BASE64(method:password@server:port)#name
    或: ss://BASE64(method:password)@server:port?params#name (SIP002)
    或: ss://BASE64#name (2022格式)
    """
    # 提取名称
    name = ""
    if "#" in uri:
        uri_part, name_part = uri.rsplit("#", 1)
        name = urllib.parse.unquote(name_part)
        uri = uri_part
    else:
        name = "SS Node"

    # 移除 ss:// 前缀
    uri = uri[5:]

    # 解析参数
    params = {}
    if "?" in uri:
        uri, query = uri.split("?", 1)
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = urllib.parse.unquote(v)

    # 检查是否是 2022 格式 (只有 base64 字符串)
    if "@" not in uri:
        try:
            decoded = base64.b64decode(uri).decode("utf-8")
            # 2022 格式: method:psk:psk2
            parts = decoded.split(":")
            if len(parts) >= 2:
                method = parts[0]
                password = ":".join(parts[1:])
                return ProxyNode(
                    name=name,
                    server=params.get("server", ""),
                    port=int(params.get("port", 443)),
                    protocol="ss",
                    config={
                        "method": method,
                        "password": password,
                    },
                    country=extract_country(name),
                )
        except Exception:
            pass
        return None

    # 标准 SIP002 格式: base64(method:password)@server:port
    if "@" in uri:
        userinfo, server_part = uri.rsplit("@", 1)
        try:
            # 尝试 base64 解码 userinfo
            decoded_userinfo = base64.b64decode(userinfo).decode("utf-8")
            if ":" in decoded_userinfo:
                method, password = decoded_userinfo.split(":", 1)
            else:
                return None
        except Exception:
            # 可能是 URL 编码
            userinfo = urllib.parse.unquote(userinfo)
            if ":" in userinfo:
                method, password = userinfo.split(":", 1)
            else:
                return None

        # 解析 server:port
        if ":" in server_part:
            server, port_str = server_part.rsplit(":", 1)
            port = int(port_str)
        else:
            server = server_part
            port = 443

        return ProxyNode(
            name=name,
            server=server,
            port=port,
            protocol="ss",
            config={
                "method": method,
                "password": password,
                **params,
            },
            country=extract_country(name),
        )

    return None


def parse_vless(uri: str) -> ProxyNode | None:
    """解析 VLESS URI
    格式: vless://uuid@server:port?params#name
    """
    # 提取名称
    name = ""
    if "#" in uri:
        uri_part, name_part = uri.rsplit("#", 1)
        name = urllib.parse.unquote(name_part)
        uri = uri_part
    else:
        name = "VLESS Node"

    uri = uri[8:]  # 移除 vless://

    # 解析参数
    params = {}
    if "?" in uri:
        uri, query = uri.split("?", 1)
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = urllib.parse.unquote(v)

    # 解析 uuid@server:port
    if "@" not in uri:
        return None

    uuid, server_part = uri.rsplit("@", 1)
    if ":" not in server_part:
        return None

    server, port_str = server_part.rsplit(":", 1)
    port = int(port_str)

    return ProxyNode(
        name=name,
        server=server,
        port=port,
        protocol="vless",
        config={
            "uuid": uuid,
            "encryption": params.get("encryption", "none"),
            "flow": params.get("flow", ""),
            "security": params.get("security", "none"),
            "sni": params.get("sni", ""),
            "type": params.get("type", "tcp"),
            **params,
        },
        country=extract_country(name),
    )


def parse_vmess(uri: str) -> ProxyNode | None:
    """解析 VMess URI
    格式: vmess://base64(json)
    """
    # 提取名称
    name = ""
    if "#" in uri:
        uri_part, name_part = uri.rsplit("#", 1)
        name = urllib.parse.unquote(name_part)
        uri = uri_part
    else:
        name = "VMess Node"

    uri = uri[8:]  # 移除 vmess://

    try:
        decoded = base64.b64decode(uri).decode("utf-8")
        config = json.loads(decoded)

        return ProxyNode(
            name=config.get("ps", name),
            server=config.get("add", ""),
            port=int(config.get("port", 443)),
            protocol="vmess",
            config={
                "uuid": config.get("id", ""),
                "alterId": int(config.get("aid", 0)),
                "security": config.get("scy", "auto"),
                "net": config.get("net", "tcp"),
                "type": config.get("type", "none"),
                "host": config.get("host", ""),
                "path": config.get("path", ""),
                "tls": config.get("tls", ""),
                "sni": config.get("sni", ""),
            },
            country=extract_country(config.get("ps", name)),
        )
    except Exception:
        pass

    return None


def parse_trojan(uri: str) -> ProxyNode | None:
    """解析 Trojan URI
    格式: trojan://password@server:port?params#name
    """
    # 提取名称
    name = ""
    if "#" in uri:
        uri_part, name_part = uri.rsplit("#", 1)
        name = urllib.parse.unquote(name_part)
        uri = uri_part
    else:
        name = "Trojan Node"

    uri = uri[9:]  # 移除 trojan://

    # 解析参数
    params = {}
    if "?" in uri:
        uri, query = uri.split("?", 1)
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = urllib.parse.unquote(v)

    # 解析 password@server:port
    if "@" not in uri:
        return None

    password, server_part = uri.rsplit("@", 1)
    if ":" not in server_part:
        return None

    server, port_str = server_part.rsplit(":", 1)
    port = int(port_str)

    return ProxyNode(
        name=name,
        server=server,
        port=port,
        protocol="trojan",
        config={
            "password": password,
            "sni": params.get("sni", server),
            "type": params.get("type", "tcp"),
            **params,
        },
        country=extract_country(name),
    )


def parse_http(uri: str) -> ProxyNode | None:
    """解析 HTTP 代理 URI"""
    parsed = urllib.parse.urlparse(uri)

    # 提取名称
    name = ""
    if parsed.fragment:
        name = parsed.fragment
    else:
        name = f"HTTP {parsed.hostname}"

    config = {
        "scheme": parsed.scheme,
    }
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password

    return ProxyNode(
        name=name,
        server=parsed.hostname or "",
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        protocol="http",
        config=config,
        country=None,
    )


def parse_socks5(uri: str) -> ProxyNode | None:
    """解析 SOCKS5 代理 URI"""
    parsed = urllib.parse.urlparse(uri)

    name = parsed.fragment or f"SOCKS5 {parsed.hostname}"

    config = {}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password

    return ProxyNode(
        name=name,
        server=parsed.hostname or "",
        port=parsed.port or 1080,
        protocol="socks5",
        config=config,
        country=None,
    )


def extract_country(name: str) -> str | None:
    """从节点名称中提取国家/地区"""
    # 常见国旗 emoji 映射
    flag_map = {
        "🇺🇸": "US", "🇺🇲": "US", "美国": "US", "美國": "US", "USA": "US",
        "🇭🇰": "HK", "香港": "HK", "港": "HK", "Hong Kong": "HK",
        "🇯🇵": "JP", "日本": "JP", "日": "JP", "Japan": "JP",
        "🇸🇬": "SG", "新加坡": "SG", "新": "SG", "Singapore": "SG",
        "🇹🇼": "TW", "台湾": "TW", "台": "TW", "Taiwan": "TW",
        "🇰🇷": "KR", "韩国": "KR", "韓國": "KR", "韩": "KR", "Korea": "KR",
        "🇩🇪": "DE", "德国": "DE", "德國": "DE", "德": "DE", "Germany": "DE",
        "🇬🇧": "GB", "英国": "GB", "英國": "GB", "英": "GB", "UK": "GB",
        "🇫🇷": "FR", "法国": "FR", "法國": "FR", "法": "FR", "France": "FR",
        "🇨🇦": "CA", "加拿大": "CA", "Canada": "CA",
        "🇦🇺": "AU", "澳大利亚": "AU", "澳洲": "AU", "Australia": "AU",
        "🇷🇺": "RU", "俄罗斯": "RU", "俄羅斯": "RU", "Russia": "RU",
        "🇮🇳": "IN", "印度": "IN", "India": "IN",
        "🇧🇷": "BR", "巴西": "BR", "Brazil": "BR",
        "🇳🇱": "NL", "荷兰": "NL", "荷蘭": "NL", "Netherlands": "NL",
        "🇦🇷": "AR", "阿根廷": "AR", "Argentina": "AR",
        "🇹🇷": "TR", "土耳其": "TR", "Turkey": "TR",
        "🇻🇳": "VN", "越南": "VN", "Vietnam": "VN",
        "🇹🇭": "TH", "泰国": "TH", "泰國": "TH", "Thailand": "TH",
        "🇲🇾": "MY", "马来西亚": "MY", "馬來西亞": "MY", "Malaysia": "MY",
        "🇵🇭": "PH", "菲律宾": "PH", "菲律賓": "PH", "Philippines": "PH",
        "🇮🇩": "ID", "印尼": "ID", "印度尼西亚": "ID", "Indonesia": "ID",
        "🇦🇪": "AE", "阿联酋": "AE", "阿聯酋": "AE", "UAE": "AE",
        "🇿🇦": "ZA", "南非": "ZA", "South Africa": "ZA",
        "🇲🇽": "MX", "墨西哥": "MX", "Mexico": "MX",
        "🇵🇱": "PL", "波兰": "PL", "波蘭": "PL", "Poland": "PL",
        "🇮🇹": "IT", "意大利": "IT", "Italy": "IT",
        "🇪🇸": "ES", "西班牙": "ES", "Spain": "ES",
        "🇨🇭": "CH", "瑞士": "CH", "Switzerland": "CH",
        "🇸🇪": "SE", "瑞典": "SE", "Sweden": "SE",
        "🇳🇴": "NO", "挪威": "NO", "Norway": "NO",
        "🇫🇮": "FI", "芬兰": "FI", "芬蘭": "FI", "Finland": "FI",
        "🇩🇰": "DK", "丹麦": "DK", "丹麥": "DK", "Denmark": "DK",
        "🇦🇹": "AT", "奥地利": "AT", "奧地利": "AT", "Austria": "AT",
        "🇧🇪": "BE", "比利时": "BE", "比利時": "BE", "Belgium": "BE",
        "🇮🇪": "IE", "爱尔兰": "IE", "愛爾蘭": "IE", "Ireland": "IE",
        "🇵🇹": "PT", "葡萄牙": "PT", "Portugal": "PT",
        "🇨🇿": "CZ", "捷克": "CZ", "Czech": "CZ",
        "🇷🇴": "RO", "罗马尼亚": "RO", "羅馬尼亞": "RO", "Romania": "RO",
        "🇺🇦": "UA", "乌克兰": "UA", "烏克蘭": "UA", "Ukraine": "UA",
        "🇮🇱": "IL", "以色列": "IL", "Israel": "IL",
        "🇪🇬": "EG", "埃及": "EG", "Egypt": "EG",
        "🇳🇬": "NG", "尼日利亚": "NG", "尼日利亞": "NG", "Nigeria": "NG",
        "🇰🇪": "KE", "肯尼亚": "KE", "肯尼亞": "KE", "Kenya": "KE",
        "🇱🇧": "LB", "黎巴嫩": "LB", "Lebanon": "LB",
        "🇮🇶": "IQ", "伊拉克": "IQ", "Iraq": "IQ",
        "🇸🇾": "SY", "叙利亚": "SY", "敘利亞": "SY", "Syria": "SY",
        "🇿🇼": "ZW", "津巴布韦": "ZW", "Zimbabwe": "ZW",
        "🇸🇳": "SN", "塞内加尔": "SN", "Senegal": "SN",
        "🇹🇿": "TZ", "坦桑尼亚": "TZ", "Tanzania": "TZ",
        "🇧🇩": "BD", "孟加拉": "BD", "Bangladesh": "BD",
        "🇵🇰": "PK", "巴基斯坦": "PK", "Pakistan": "PK",
        "🇱🇰": "LK", "斯里兰卡": "LK", "Sri Lanka": "LK",
        "🇲🇲": "MM", "缅甸": "MM", "Myanmar": "MM",
        "🇰🇭": "KH", "柬埔寨": "KH", "Cambodia": "KH",
        "🇱🇦": "LA", "老挝": "LA", "Laos": "LA",
        "🇳🇵": "NP", "尼泊尔": "NP", "Nepal": "NP",
        "🇲🇳": "MN", "蒙古": "MN", "Mongolia": "MN",
        "🇰🇿": "KZ", "哈萨克斯坦": "KZ", "Kazakhstan": "KZ",
        "🇺🇿": "UZ", "乌兹别克斯坦": "UZ", "Uzbekistan": "UZ",
        "🇬🇪": "GE", "格鲁吉亚": "GE", "Georgia": "GE",
        "🇦🇲": "AM", "亚美尼亚": "AM", "Armenia": "AM",
        "🇦🇿": "AZ", "阿塞拜疆": "AZ", "Azerbaijan": "AZ",
        "🇨🇱": "CL", "智利": "CL", "Chile": "CL",
        "🇨🇴": "CO", "哥伦比亚": "CO", "哥倫比亞": "CO", "Colombia": "CO",
        "🇵🇪": "PE", "秘鲁": "PE", "Peru": "PE",
        "🇻🇪": "VE", "委内瑞拉": "VE", "Venezuela": "VE",
        "🇨🇷": "CR", "哥斯达黎加": "CR", "Costa Rica": "CR",
        "🇵🇦": "PA", "巴拿马": "PA", "巴拿馬": "PA", "Panama": "PA",
    }

    for pattern, code in flag_map.items():
        if pattern in name:
            return code

    return None


def get_country_name(code: str | None) -> str:
    """将国家代码转换为名称"""
    if not code:
        return "未知"

    names = {
        "US": "美国", "HK": "香港", "JP": "日本", "SG": "新加坡",
        "TW": "台湾", "KR": "韩国", "DE": "德国", "GB": "英国",
        "FR": "法国", "CA": "加拿大", "AU": "澳大利亚", "RU": "俄罗斯",
        "IN": "印度", "BR": "巴西", "NL": "荷兰", "AR": "阿根廷",
        "TR": "土耳其", "VN": "越南", "TH": "泰国", "MY": "马来西亚",
        "PH": "菲律宾", "ID": "印尼", "AE": "阿联酋", "ZA": "南非",
        "MX": "墨西哥", "PL": "波兰", "IT": "意大利", "ES": "西班牙",
        "CH": "瑞士", "SE": "瑞典", "NO": "挪威", "FI": "芬兰",
        "DK": "丹麦", "AT": "奥地利", "BE": "比利时", "IE": "爱尔兰",
        "PT": "葡萄牙", "CZ": "捷克", "RO": "罗马尼亚", "UA": "乌克兰",
        "IL": "以色列", "EG": "埃及", "NG": "尼日利亚", "KE": "肯尼亚",
        "LB": "黎巴嫩", "IQ": "伊拉克", "SY": "叙利亚", "ZW": "津巴布韦",
        "SN": "塞内加尔", "TZ": "坦桑尼亚", "BD": "孟加拉", "PK": "巴基斯坦",
        "LK": "斯里兰卡", "MM": "缅甸", "KH": "柬埔寨", "LA": "老挝",
        "NP": "尼泊尔", "MN": "蒙古", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
        "GE": "格鲁吉亚", "AM": "亚美尼亚", "AZ": "阿塞拜疆",
        "CL": "智利", "CO": "哥伦比亚", "PE": "秘鲁", "VE": "委内瑞拉",
        "CR": "哥斯达黎加", "PA": "巴拿马",
    }
    return names.get(code, code)
