"""手机局域网入口：本机签发录音页 URL、二维码与访问令牌。

令牌只有一个来源：Runtime.auth.token（生产入口用 load_or_create_token(明确路径) 读一次，测试给假值）。
本模块不在 import 时读写任何文件。
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from _contracts import ErrorCode, LanAccessEntry, LanAccessResponse, LogicError
from _v3.auth import is_allowed_lan_ip, parse_ip

VOICE_PAGE_PATH = "/voice"


def load_or_create_token(path: Path) -> str:
    """生产入口专用：从显式路径读取令牌；不存在时以 0600 原子创建。读写失败可见，不吞错。

    测试与沙箱不得调用本函数指向 live 文件，应直接给假令牌。
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise LogicError(ErrorCode.config_io, f"无法读取访问令牌文件 {path}: {exc}") from exc
    if existing:
        return existing
    value = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_or_create_token(path)
    except OSError as exc:
        raise LogicError(ErrorCode.config_io, f"无法创建访问令牌文件 {path}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
        fh.flush()
        os.fsync(fh.fileno())
    return value


def mask_token(token: str) -> str:
    return f"{token[:4]}…（共 {len(token)} 位）"


Runner = Callable[..., subprocess.CompletedProcess]


def list_physical_lan_ipv4(
    *,
    run: Runner = subprocess.run,
    sys_class_net: Path = Path("/sys/class/net"),
) -> list[str]:
    """枚举有物理设备的网卡上的局域网 IPv4（排除 docker/virbr 等虚拟网桥与回环）。"""
    try:
        proc = run(["ip", "-j", "-4", "addr", "show", "up"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogicError(ErrorCode.dependency_failed, f"无法枚举网卡地址: {exc}") from exc
    if proc.returncode != 0:
        raise LogicError(ErrorCode.dependency_failed, f"ip addr 失败: {(proc.stderr or '').strip()}")
    try:
        interfaces = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise LogicError(ErrorCode.dependency_failed, f"ip addr 输出无法解析: {exc}") from exc
    found: list[str] = []
    for iface in interfaces:
        name = str(iface.get("ifname") or "")
        if not name or not (sys_class_net / name / "device").exists():
            continue
        for addr in iface.get("addr_info") or []:
            ip = parse_ip(str(addr.get("local") or ""))
            if ip is None or not is_allowed_lan_ip(ip) or ip.is_link_local:
                continue
            text = str(ip)
            if text not in found:
                found.append(text)
    return found


def qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as exc:
        raise LogicError(ErrorCode.dependency_failed, f"二维码依赖 qrcode 不可用: {exc}") from exc
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage)
    data = image.to_string()
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


class LanAccessService:
    def __init__(
        self,
        *,
        token: str,
        list_ips: Callable[[], list[str]],
        port: int,
        make_qr: Callable[[str], str] = qr_svg,
    ) -> None:
        if not token.strip():
            raise ValueError("局域网入口需要非空令牌")
        self._token = token
        self._list_ips = list_ips
        self._port = port
        self._make_qr = make_qr

    def _url(self, ip: str, token: str) -> str:
        return f"https://{ip}:{self._port}{VOICE_PAGE_PATH}?token={quote(token, safe='')}"

    def describe(self) -> LanAccessResponse:
        token = self._token
        masked = mask_token(token)
        ips = self._list_ips()
        entries = [
            LanAccessEntry(
                ip=ip,
                url=self._url(ip, token),
                url_masked=f"https://{ip}:{self._port}{VOICE_PAGE_PATH}?token={masked}",
                qr_svg=self._make_qr(self._url(ip, token)),
            )
            for ip in ips
        ]
        message = "" if entries else "未找到可用的局域网地址；请确认电脑已连接局域网。"
        return LanAccessResponse(
            port=self._port,
            token=token,
            token_masked=masked,
            entries=entries,
            message=message,
        )
