"""本机 / 局域网鉴权。不信任 X-Forwarded-For；token 用恒定时间比较。"""
from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from starlette.requests import Request

from _contracts import ErrorBody, ErrorCode
from _v3.limits import canonical_request_path
from _v3.pages import is_lan_public_path

SERVER_PORT = 17945
LOCALHOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# 明确允许的局域网：RFC1918、IPv6 ULA、必要的链路本地。
# 不使用 ipaddress.is_private（其包含 198.18/15、TEST-NET、文档网段等，那些不是局域网）。
_LAN_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


@dataclass(frozen=True)
class AuthSettings:
    token: str
    port: int = SERVER_PORT


def parse_ip(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """解析对端或 Host 中的地址；IPv4-mapped IPv6 还原为 IPv4。无法解析返回 None。"""
    if not host:
        return None
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_loopback_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(ip.is_loopback)


def is_allowed_lan_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if is_loopback_ip(ip):
        return False
    return any(ip in network for network in _LAN_NETWORKS)


def is_local_client(request: Request) -> bool:
    """只看 socket 对端是否 loopback，忽略 X-Forwarded-For / Forwarded。"""
    client = request.client
    if client is None or not client.host:
        return False
    ip = parse_ip(client.host)
    return bool(ip and is_loopback_ip(ip))


def is_lan_client(request: Request) -> bool:
    """socket 对端是否为允许的局域网地址。公网、保留、文档、198.18、无法解析都不是。"""
    client = request.client
    if client is None or not client.host:
        return False
    ip = parse_ip(client.host)
    return bool(ip and is_allowed_lan_ip(ip))


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        value = auth[7:].strip()
        return value or None
    header = request.headers.get("x-voice-token")
    if header and header.strip():
        return header.strip()
    query = request.query_params.get("token")
    if query and query.strip():
        return query.strip()
    return None


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    left = provided.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        secrets.compare_digest(right, right)
        return False
    return secrets.compare_digest(left, right)


def _split_host_port(netloc: str) -> tuple[str, str | None]:
    netloc = netloc.strip()
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            return netloc, None
        host = netloc[1:end]
        rest = netloc[end + 1 :]
        port = rest[1:] if rest.startswith(":") and rest[1:] else None
        return host, port
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        return host, port or None
    return netloc, None


def parse_host_header(request_host: str | None, port: int) -> tuple[str, str] | None:
    """要求 Host 显式带本服务端口。返回 (hostname, port) 或 None。"""
    if not request_host:
        return None
    host, header_port = _split_host_port(request_host)
    if not host or header_port != str(port):
        return None
    return host, header_port


def host_matches_peer(*, host: str, is_local: bool) -> bool:
    """本机请求 Host 只能是 localhost 名；局域网请求 Host 必须是允许的局域网地址。"""
    name = host.strip().lower().lstrip("[").rstrip("]")
    if is_local:
        return name in LOCALHOST_NAMES
    ip = parse_ip(host)
    return bool(ip and is_allowed_lan_ip(ip))


def request_host_allowed(request: Request, *, is_local: bool, port: int) -> bool:
    parsed = parse_host_header(request.headers.get("host"), port)
    if parsed is None:
        return False
    return host_matches_peer(host=parsed[0], is_local=is_local)


def origin_allowed(
    origin: str,
    *,
    is_local: bool,
    request_host: str | None,
    port: int,
) -> bool:
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host, origin_port = _split_host_port(parsed.netloc)
    if origin_port != str(port):
        return False
    if not host_matches_peer(host=host, is_local=is_local):
        return False
    header = parse_host_header(request_host, port)
    if header is None:
        return False
    req_host, _req_port = header
    if req_host.lower().lstrip("[").rstrip("]") != host.lower().lstrip("[").rstrip("]"):
        return False
    if not host_matches_peer(host=req_host, is_local=is_local):
        return False
    return True


def access_mode(method: str, path: str) -> str:
    """local = 仅本机；lan_token = 本机或局域网+token；lan_public = 本机或局域网对端的只读界面资源。

    默认仅本机。先规范化路径再分类，`/claim/` 与 `/claim` 同属 local，不依赖重定向。
    """
    canon = canonical_request_path(path)
    if canon is None:
        return "local"
    path = canon
    if method.upper() in {"GET", "HEAD"} and is_lan_public_path(path):
        return "lan_public"
    if path.startswith("/api/setup/"):
        return "local"
    if path in {"/api/v1/status", "/api/v1/config", "/api/v1/events", "/api/v1/events/platform"}:
        return "local"
    if path.startswith("/api/v1/recognize/"):
        return "local"
    if path == "/api/v1/utterances":
        return "local"
    if path in {"/api/v1/utterances/from-audio", "/api/v1/utterances/resend"}:
        return "lan_token"
    if path == "/api/v1/jobs/claim":
        return "local"
    if path.startswith("/api/v1/jobs/") and path.endswith("/ack"):
        return "local"
    if path.startswith("/api/v1/jobs/"):
        return "lan_token"
    if path in {"/api/v1/stt", "/api/v1/polish", "/api/v1/translate"}:
        return "lan_token"
    if path == "/api/v1/ocr-translate":
        return "local"
    if path == "/api/v1/history":
        return "lan_token"
    return "local"


def forbidden(message: str, *, code: ErrorCode = ErrorCode.forbidden) -> dict:
    return ErrorBody(error=code, message=message).model_dump()
