"""状态栏 / WebKit 宿主策略。不抢焦点；只加载本机 https；只信任项目证书。

具体 GTK/WebKit 控件在有显示环境时再创建。本模块先把可测试的策略独立出来，
避免在无 DISPLAY 时导入 gi。复制译文不得改写已绑定的注入焦点。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_CERT = PROJECT_ROOT / "certs" / "cert.pem"
ALLOWED_UI_ORIGIN = "https://127.0.0.1:17945"


@dataclass(frozen=True)
class StatusWindowPolicy:
    """与现用 daemon.StatusBar 同一组 GTK 窗口策略，由宿主应用，不另写一份常量。"""

    accept_focus: bool = False
    keep_above: bool = True
    skip_taskbar: bool = True
    skip_pager: bool = True
    decorated: bool = False
    type_hint: str = "utility"


@dataclass
class InjectFocusGuard:
    """截图译文复制与注入焦点隔离。复制只动自己的缓冲，不改 job.focus_id。"""

    bound_focus_id: int | str | None = None
    copied_text: str | None = None

    def bind(self, focus_id: int | str | None) -> None:
        self.bound_focus_id = focus_id

    def copy_translation(self, text: str) -> str:
        self.copied_text = text
        return text

    def inject_focus_id(self) -> int | str | None:
        return self.bound_focus_id


def is_allowed_ui_url(url: str, *, origin: str = ALLOWED_UI_ORIGIN) -> bool:
    parsed = urlparse(url)
    allowed = urlparse(origin)
    if parsed.scheme == "file":
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        return False
    if parsed.port and allowed.port and parsed.port != allowed.port:
        return False
    return True


def tls_verify_disabled_forbidden() -> bool:
    """策略声明：宿主不得设置 verify=False 或 TLS IGNORE。"""
    return True


def project_cert_path() -> Path:
    return PROJECT_CERT


STATUS_WINDOW_POLICY = StatusWindowPolicy()

# 译文浮层：需要接收 Esc 与"复制"点击，故可获得焦点（与旧 daemon.TranslationOverlay 同一组策略）；
# 关闭后焦点回到原窗口。注入只按 job 绑定焦点，不受它影响（见 InjectFocusGuard）。
TRANSLATE_WINDOW_POLICY = StatusWindowPolicy(accept_focus=True)

STATUS_PAGE_PATH = "/overlay-status"
TRANSLATE_PAGE_PATH = "/overlay-translate"

# 译文页“结果已呈现”本地桥：只注册在每个译文窗口自己的 UserContentManager 上（其它页面/窗口拿不到），
# 名字与 static/v3/js/business/translate-controller.js 的 PRESENTED_HANDLER 一致。
TRANSLATE_PRESENTED_HANDLER = "oviTranslatePresented"
_GENERATION_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def translate_generation_script(generation: str) -> str:
    """document-start 脚本：把本窗口一次性 generation 写成页面只读常量（不可改写/删除）。"""
    if not (16 <= len(generation) <= 64) or not set(generation) <= _GENERATION_CHARS:
        raise ValueError("generation 必须是 16–64 位 URL 安全随机串")
    return (
        "Object.defineProperty(window,'__V3_TRANSLATE_GENERATION__',"
        "{value:'" + generation + "',writable:false,configurable:false,enumerable:false});"
    )


def translate_image_script(png: bytes) -> str:
    """页面 document-start 脚本：把截图 PNG 交给浮层页面的 initialImage（`__V3_TRANSLATE_IMAGE__`）。

    只传图片，OCR/翻译请求与结果/错误显示由页面完成，不在宿主重写第二份。
    """
    import base64

    encoded = base64.b64encode(png).decode("ascii")
    return (
        "(function(){var b=atob('" + encoded + "');var u=new Uint8Array(b.length);"
        "for(var i=0;i<b.length;i++){u[i]=b.charCodeAt(i);}"
        "window.__V3_TRANSLATE_IMAGE__=new Blob([u],{type:'image/png'});})();"
    )
