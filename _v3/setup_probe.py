"""设置页的真实状态探测（逻辑层）：只读，不启动、不下载、不改系统。

每个探测返回结构化事实与可见错误文本；不把探测失败当成就绪。
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from _contracts import SetupCapability

if TYPE_CHECKING:  # 运行时不导入：setup_actions 反向依赖本模块
    from _linux.conflicts import ConflictScan
    from _v3.setup_actions import UnitState


@dataclass(frozen=True)
class OllamaProbe:
    installed: bool
    running: bool
    version: str | None
    has_model: bool
    model_size: int | None
    error: str | None


def probe_ollama(*, binary: Path, client: httpx.Client, model: str) -> OllamaProbe:
    """binary 是产品使用的用户级官方 Ollama 可执行文件；client 指向它的回环地址。"""
    installed = binary.is_file() and os.access(binary, os.X_OK)
    try:
        version_resp = client.get("/api/version")
        version_resp.raise_for_status()
        version = version_resp.json().get("version")
        tags_resp = client.get("/api/tags")
        tags_resp.raise_for_status()
        models = tags_resp.json().get("models") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return OllamaProbe(installed, False, None, False, None, f"Ollama 未响应: {exc}")
    match = next((m for m in models if model in {m.get("name"), m.get("model")}), None)
    size = match.get("size") if isinstance(match, dict) else None
    return OllamaProbe(installed, True, version, match is not None, size, None)


@dataclass(frozen=True)
class AsrWeightsProbe:
    complete: bool
    bytes_on_disk: int
    missing: tuple[str, ...]


def _dir_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def probe_asr_weights(model_dir: Path) -> AsrWeightsProbe:
    """权重完整 = config.json 存在，且 safetensors 索引引用的全部分片存在，且无未完成的下载片段。"""
    if not model_dir.is_dir():
        return AsrWeightsProbe(False, 0, ("模型目录不存在",))
    missing: list[str] = []
    if not (model_dir / "config.json").is_file():
        missing.append("config.json")
    index = model_dir / "model.safetensors.index.json"
    if index.is_file():
        try:
            shards = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
        except (OSError, ValueError, KeyError) as exc:
            missing.append(f"索引无法解析: {exc}")
            shards = set()
        missing.extend(sorted(name for name in shards if not (model_dir / name).is_file()))
    elif not any(model_dir.glob("*.safetensors")):
        missing.append("*.safetensors")
    if any(model_dir.rglob("*.incomplete")):
        missing.append("存在未完成的下载片段")
    return AsrWeightsProbe(not missing, _dir_bytes(model_dir), tuple(missing))


@dataclass(frozen=True)
class SidecarProbe:
    running: bool
    loaded: bool
    error: str | None


def probe_asr_sidecar(client: httpx.Client) -> SidecarProbe:
    try:
        resp = client.get("/health")
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return SidecarProbe(False, False, f"识别服务未响应: {exc}")
    return SidecarProbe(bool(payload.get("ok")), bool(payload.get("loaded")), None)


PortalVersionFn = Callable[[str], tuple[int | None, str | None]]
PORTAL_SCREENSHOT = "org.freedesktop.portal.Screenshot"
PORTAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"


def dbus_portal_version(interface: str) -> tuple[int | None, str | None]:
    """xdg-desktop-portal 接口版本（沿用工作区 portal 检测语义）。失败返回原因，不吞成 False。"""
    try:
        import dbus

        bus = dbus.SessionBus()
        obj = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return int(props.Get(interface, "version")), None
    except Exception as exc:  # dbus 的异常类型随绑定而异，原因原样返回给设置页
        return None, f"{type(exc).__name__}: {exc}"


def session_type(environ: Mapping[str, str]) -> str:
    value = (environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    return value or "unknown"


def _tool(which: Callable[[str], str | None], name: str, label: str, *, required: bool) -> SetupCapability:
    path = which(name)
    return SetupCapability(
        id=name,
        label=label,
        available=path is not None,
        required=required,
        detail=path or f"未找到 {name}",
    )


def _portal(portal_version: PortalVersionFn, interface: str, cap_id: str, label: str, *, required: bool) -> SetupCapability:
    version, error = portal_version(interface)
    return SetupCapability(
        id=cap_id,
        label=label,
        available=version is not None,
        required=required,
        detail=f"version {version}" if version is not None else error,
    )


def probe_capabilities(
    *,
    session: str,
    which: Callable[[str], str | None],
    portal_version: PortalVersionFn,
    ocr_languages: Callable[[], list[str]],
) -> list[SetupCapability]:
    caps = [
        _tool(which, "xclip", "剪贴板 xclip", required=True),
        _tool(which, "notify-send", "桌面通知 notify-send", required=False),
    ]
    wayland = session == "wayland"
    x11 = session == "x11"
    if session not in {"wayland", "x11"}:
        caps.append(
            SetupCapability(
                id="session_type",
                label="桌面会话类型",
                available=False,
                required=True,
                detail=f"无法判断会话类型（XDG_SESSION_TYPE={session}）",
            )
        )
    if not x11:
        caps.append(_portal(portal_version, PORTAL_SCREENSHOT, "portal_screenshot", "Wayland 截图 portal", required=wayland))
        caps.append(_portal(portal_version, PORTAL_SHORTCUTS, "portal_shortcuts", "Wayland 全局快捷键 portal", required=wayland))
    if not wayland:
        caps.append(_tool(which, "maim", "X11 截图 maim", required=x11))
    try:
        langs = set(ocr_languages())
        lang_error = None
    except Exception as exc:  # pytesseract/tesseract 缺失时原因直接展示
        langs, lang_error = set(), f"{type(exc).__name__}: {exc}"
    for code, label in (("chi_sim", "OCR 简体中文"), ("eng", "OCR 英文")):
        caps.append(
            SetupCapability(
                id=f"ocr_{code}",
                label=label,
                available=code in langs,
                required=True,
                detail=lang_error or (None if code in langs else f"tesseract 缺少 {code} 语言包"),
            )
        )
    caps.append(
        SetupCapability(
            id="ocr_jpn",
            label="OCR 日文（默认不安装）",
            available="jpn" in langs,
            required=False,
            detail=None if "jpn" in langs else "未安装 jpn 语言包；默认不安装",
        )
    )
    return caps


HOTKEY_CONFLICTS_ID = "hotkey_conflicts"
DESKTOP_DAEMON_ID = "desktop_daemon"
# 与 start.sh 的 ACCEPTANCE_CONF 同名（tests/integration 断言两处一致）：验收运行写给各单元的 drop-in 文件名。
ACCEPTANCE_DROPIN = "acceptance.conf"
_RUNNING_STATES = ("active", "activating", "reloading")


def hotkey_conflicts_capability(scan: Callable[[], "ConflictScan"]) -> SetupCapability:
    """快捷键冲突能力项：唯一扫描器 `_linux.conflicts.scan_hotkey_conflicts` 的结果如实呈现。
    unread 非空时不论有无冲突都不可用（不能宣称无冲突）；扫描本身异常 → 不可用并写原因。只读，不改任何键位/授权。"""
    label = "快捷键冲突检查"
    try:
        result = scan()
    except Exception as exc:  # 配置读取/扫描异常都必须可见，不当无冲突
        return SetupCapability(id=HOTKEY_CONFLICTS_ID, label=label, available=False, required=False,
                               detail=f"无法检查快捷键冲突：{type(exc).__name__}: {exc}")
    parts: list[str] = []
    if result.conflicts:
        parts.append("与已登记的快捷键冲突：" + "；".join(result.conflicts))
    if result.unread:
        parts.append("未能读取（不能据此判定无冲突）：" + "；".join(result.unread))
    if parts:
        return SetupCapability(id=HOTKEY_CONFLICTS_ID, label=label, available=False, required=False,
                               detail="。".join(parts))
    return SetupCapability(
        id=HOTKEY_CONFLICTS_ID, label=label, available=True, required=False,
        detail="已读取的 GNOME 键位与其它应用全局快捷键登记中未发现冲突（第三方 evdev 监听与应用内快捷键无法检测）",
    )


def _acceptance_units(states: Sequence["UnitState"]) -> list[str]:
    return [st.unit for st in states if any(Path(p).name == ACCEPTANCE_DROPIN for p in st.drop_in_paths)]


def desktop_daemon_capability(
    *,
    read_state: Callable[[str], "UnitState"],
    daemon_unit: str,
    units: Sequence[str],
    unmet_gates: Callable[[], Sequence[str]],
) -> SetupCapability:
    """桌面守护进程（快捷键/截屏）是否在运行、保存的快捷键如何生效。只读 systemd 单元状态与门闩，不执行任何命令。

    结论只来自实际读到的事实：daemon 运行中 → 保存后 ./restart.sh；未运行且门闩未关 → 完整受控运行
    ./start.sh --acceptance（并说明无参数 ./start.sh/桌面图标会撤销验收运行）；门闩已关 → ./start.sh；
    任一读取失败 → 无法判断。"""
    label = "桌面守护进程（快捷键/截屏）"

    def cap(available: bool, detail: str) -> SetupCapability:
        return SetupCapability(id=DESKTOP_DAEMON_ID, label=label, available=available, required=False, detail=detail)

    try:
        states = [read_state(unit) for unit in units]
        gates = list(unmet_gates())
    except Exception as exc:  # LogicError(读单元失败) 或其它：一律未知
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        return cap(False, f"无法读取守护进程状态：{message}；无法判断快捷键是否生效")
    by_unit = {st.unit: st for st in states}
    daemon = by_unit.get(daemon_unit)
    if daemon is None:
        return cap(False, f"未读取 {daemon_unit} 状态；无法判断快捷键是否生效")
    accepted = _acceptance_units(states)
    if daemon.load_state != "loaded":
        return cap(False, f"守护进程单元未安装（LoadState={daemon.load_state}），快捷键/截屏不会生效")
    if daemon.active_state == "active" and daemon.sub_state == "running":
        return cap(True, "守护进程运行中。保存快捷键后，在项目目录运行 ./restart.sh 重启守护进程才会按新快捷键生效"
                         "（重新向 GNOME 登记，可能弹出确认）；不重启则继续使用启动时的快捷键")
    if daemon.active_state in _RUNNING_STATES:
        return cap(False, f"守护进程正在启动或重启中（{daemon.summary()}），快捷键暂未生效；"
                          f"若持续如此请查看 journalctl --user -u {daemon_unit}")
    failed = daemon.active_state == "failed" or daemon.result not in ("success", "exec-condition")
    state_note = (f"守护进程启动失败（{daemon.summary()}），请查看 journalctl --user -u {daemon_unit}。"
                  if failed else "守护进程未运行，快捷键/截屏不会生效。")
    if not gates:
        return cap(False, state_note + "请在项目目录运行 ./start.sh 启动")
    if accepted and daemon_unit not in accepted:
        scope = f"当前只有 {'、'.join(accepted)} 处于验收运行（部分），设置页可用但守护进程不在其中。"
    elif accepted:
        scope = "守护进程已有验收 drop-in 但未运行。"
    else:
        scope = "当前没有任何单元处于验收运行。"
    return cap(
        False,
        state_note + scope + "生产门闩未关闭，完整受控运行方式：在项目目录执行 ./start.sh --acceptance"
        "（依次启动 Ollama、识别服务〔启动前做显存预检〕、Web 服务与守护进程）。"
        "不要运行无参数 ./start.sh 或桌面图标：门闩未关时它们会撤销验收运行并停止全部服务。",
    )
