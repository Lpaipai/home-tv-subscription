"""流地址网络类型识别。只识别 URL 字面量，不做可能误判的 DNS 猜测。"""
import ipaddress
from urllib.parse import urlsplit


def network_type(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return "other_protocol"
        host = parsed.hostname
        if not host:
            return "invalid"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "hostname_unresolved"
        return "ipv6_literal" if address.version == 6 else "ipv4_literal"
    except ValueError:
        return "invalid"


def is_ipv6_literal(entry):
    return network_type(entry["url"]) == "ipv6_literal"


def is_public_ipv6_url(url):
    """只允许公网 IPv6 字面量 HTTP(S) 播放 URL 进入免云端检测候选。

    不对域名猜测解析结果；拒绝组播、私网、链路本地、回环、映射地址与 URL 注入。
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if "|" in url or any(char in url for char in "\r\n\t"):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if parsed.username or parsed.password or parsed.fragment:
            return False
        host = parsed.hostname
        if not host or ":" not in host or "%" in host:
            return False
        address = ipaddress.ip_address(host)
        # is_global 不保证是单播：ff00::/8 组播（包括 ff02::1）
        # 在某些 Python 版本上同样被标为 is_global=True。
        # IPv4-mapped IPv6 也不能绕过公网 IPv6 单播限制。
        if (address.version != 6 or address.ipv4_mapped is not None
                or address.is_multicast or not address.is_global):
            return False
        _ = parsed.port  # 对非法端口进行校验
        return True
    except ValueError:
        return False
