"""GTK/XWayland WebKit 状态窗宿主。import 本模块不强制加载 gi。

只加载 https://127.0.0.1:17945，只信任项目证书，TLS 失败不得 IGNORE，拒绝 file://。
窗口策略只来自 `_overlay.STATUS_WINDOW_POLICY`，与现用 daemon.StatusBar 同一组 GTK 调用：
set_accept_focus / set_keep_above / skip_taskbar / skip_pager / set_decorated / type_hint。
不调用 present() 或 grab_focus，避免抢焦点。gi 可注入（测试夹具须标明 mock）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from _overlay import (
    ALLOWED_UI_ORIGIN,
    STATUS_WINDOW_POLICY,
    StatusWindowPolicy,
    is_allowed_ui_url,
    project_cert_path,
    tls_verify_disabled_forbidden,
)


class WebKitHostError(RuntimeError):
    pass


def load_webkit_modules():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("WebKit2", "4.1")
    gi.require_version("Gio", "2.0")
    from gi.repository import Gdk, Gio, Gtk, WebKit2

    return Gio, Gtk, WebKit2, Gdk


def _unpack_modules(modules: tuple[Any, ...] | None):
    if modules is None:
        modules = load_webkit_modules()
    if len(modules) == 3:
        gio, gtk, webkit = modules
        return gio, gtk, webkit, None
    if len(modules) != 4:
        raise WebKitHostError("gi 模块必须是 (Gio, Gtk, WebKit2, Gdk)")
    return modules


def apply_status_window_policy(
    window,
    *,
    policy: StatusWindowPolicy = STATUS_WINDOW_POLICY,
    Gdk=None,
):
    """把 STATUS_WINDOW_POLICY 落到真实（或注入的）GTK 窗口上。这是唯一应用点。"""
    window.set_accept_focus(policy.accept_focus)
    window.set_keep_above(policy.keep_above)
    window.set_skip_taskbar_hint(policy.skip_taskbar)
    window.set_skip_pager_hint(policy.skip_pager)
    window.set_decorated(policy.decorated)
    if Gdk is not None:
        hint = getattr(getattr(Gdk, "WindowTypeHint", None), policy.type_hint.upper(), None)
        if hint is None:
            raise WebKitHostError(f"无法应用 type_hint={policy.type_hint!r}")
        window.set_type_hint(hint)
    return window


def configure_webkit_context(
    context,
    *,
    cert: Path | None = None,
    hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
    modules: tuple[Any, ...] | None = None,
):
    Gio, _Gtk, WebKit2, _Gdk = _unpack_modules(modules)
    if not tls_verify_disabled_forbidden():
        raise WebKitHostError("TLS 校验关闭被禁止")
    policy = getattr(WebKit2, "TLSErrorsPolicy", None)
    if (
        policy is None
        or not hasattr(policy, "FAIL")
        or not hasattr(context, "set_tls_errors_policy")
    ):
        raise WebKitHostError("WebKit 无法设置 TLSErrorsPolicy.FAIL")
    context.set_tls_errors_policy(policy.FAIL)
    path = Path(cert) if cert else project_cert_path()
    if not path.is_file():
        raise WebKitHostError(f"项目证书不存在: {path}")
    if not hasattr(Gio, "TlsCertificate") or not hasattr(Gio.TlsCertificate, "new_from_file"):
        raise WebKitHostError("Gio.TlsCertificate.new_from_file 不可用")
    gcert = Gio.TlsCertificate.new_from_file(str(path))
    if not hasattr(context, "allow_tls_certificate_for_host"):
        raise WebKitHostError("WebKit 缺少 allow_tls_certificate_for_host")
    for host in hosts:
        context.allow_tls_certificate_for_host(gcert, host)
    return context


def navigate(webview, url: str, *, origin: str = ALLOWED_UI_ORIGIN) -> str:
    if not is_allowed_ui_url(url, origin=origin):
        raise WebKitHostError(f"拒绝加载非本机 https 或 file://: {url}")
    webview.load_uri(url)
    return url


def js_result_string(js_result) -> str | None:
    """WebKit2 4.1 script-message 的 JavascriptResult → 字符串；非字符串一律 None（页面只发 JSON 字符串）。"""
    try:
        value = js_result.get_js_value() if hasattr(js_result, "get_js_value") else js_result
        if value is not None and value.is_string():
            return value.to_string()
    except Exception:
        return None
    return None


class WebKitStatusHost:
    """创建状态窗、配置证书限定 context、经 navigate 加载 URL。默认不 present。"""

    def __init__(
        self,
        *,
        modules: tuple[Any, ...] | None = None,
        cert: Path | None = None,
        policy: StatusWindowPolicy = STATUS_WINDOW_POLICY,
        context=None,
        injected: bool | None = None,
        origin: str = ALLOWED_UI_ORIGIN,
    ) -> None:
        self.origin = origin
        self.manager = None
        self._message_handler: tuple[str, int] | None = None
        self.modules = modules
        self.cert = Path(cert) if cert else project_cert_path()
        self.policy = policy
        self.context = context
        self.window = None
        self.webview = None
        self.injected = bool(modules is not None) if injected is None else bool(injected)

    def _mods(self):
        unpacked = _unpack_modules(self.modules)
        self.modules = unpacked
        return unpacked

    def create_status_window(
        self,
        url: str | None = None,
        *,
        user_script: str | None = None,
        message_handler: tuple[str, Any] | None = None,
    ):
        """message_handler=(name, callback(raw_str|None))：在加载任何 URL 之前注册到本 WebView 私有的
        UserContentManager，页面经 window.webkit.messageHandlers[name].postMessage(string) 通知。"""
        Gio, Gtk, WebKit2, Gdk = self._mods()
        if self.context is None:
            factory = getattr(WebKit2.WebContext, "new", None)
            self.context = factory() if callable(factory) else WebKit2.WebContext()
        configure_webkit_context(
            self.context,
            cert=self.cert,
            modules=(Gio, Gtk, WebKit2, Gdk),
        )
        window_type = getattr(getattr(Gtk, "WindowType", None), "TOPLEVEL", None)
        if window_type is not None:
            self.window = Gtk.Window(type=window_type)
        else:
            self.window = Gtk.Window()
        apply_status_window_policy(self.window, policy=self.policy, Gdk=Gdk)
        self.webview = self._new_webview(WebKit2, user_script, message_handler)
        if hasattr(self.window, "add"):
            self.window.add(self.webview)
        if url is not None:
            self.load(url)
        return self.window

    def _new_webview(self, WebKit2, user_script: str | None, message_handler=None):
        if user_script is None and message_handler is None:
            if not hasattr(WebKit2.WebView, "new_with_context"):
                raise WebKitHostError(
                    "WebKit 缺少 WebView.new_with_context，拒绝创建未配置 context 的 WebView"
                )
            return WebKit2.WebView.new_with_context(self.context)
        # 需要 document-start 脚本时，WebView 同时绑定已配置证书的 context 与内容管理器。
        manager = WebKit2.UserContentManager()
        if user_script is not None:
            manager.add_script(
                WebKit2.UserScript.new(
                    user_script,
                    WebKit2.UserContentInjectedFrames.TOP_FRAME,
                    WebKit2.UserScriptInjectionTime.START,
                    None,
                    None,
                )
            )
        if message_handler is not None:
            name, callback = message_handler
            if not manager.register_script_message_handler(name):
                raise WebKitHostError(f"无法注册页面通知 {name}")

            def on_message(_manager, js_result) -> None:
                callback(js_result_string(js_result))

            sid = manager.connect(f"script-message-received::{name}", on_message)
            self._message_handler = (name, sid)
        self.manager = manager
        return WebKit2.WebView(web_context=self.context, user_content_manager=manager)

    def unregister_message_handler(self) -> None:
        """窗口关闭时注销：之后页面再 postMessage 也到不了宿主。"""
        handler, self._message_handler = self._message_handler, None
        if handler is None or self.manager is None:
            return
        name, sid = handler
        try:
            self.manager.disconnect(sid)
        finally:
            self.manager.unregister_script_message_handler(name)

    def load(self, url: str) -> str:
        if self.webview is None:
            raise WebKitHostError("尚未创建 WebView")
        return navigate(self.webview, url, origin=self.origin)

    def destroy(self) -> None:
        if self.window is not None and hasattr(self.window, "destroy"):
            self.window.destroy()
        self.window = None
        self.webview = None


__all__ = [
    "ALLOWED_UI_ORIGIN",
    "STATUS_WINDOW_POLICY",
    "WebKitHostError",
    "WebKitStatusHost",
    "apply_status_window_policy",
    "configure_webkit_context",
    "is_allowed_ui_url",
    "js_result_string",
    "load_webkit_modules",
    "navigate",
]
