"""WebKit 状态宿主：用标明的 mock gi 验证窗口策略与证书限定加载；不是真实 TLS。"""
from __future__ import annotations

import pytest

from _overlay import STATUS_WINDOW_POLICY, project_cert_path
from _webkit_host import (
    WebKitHostError,
    WebKitStatusHost,
    apply_status_window_policy,
    configure_webkit_context,
    navigate,
)
from tests.platform.fake_webkit import FakeGdk, FakeWebContext, FakeWebView, FakeWindow, fake_modules


def test_create_status_window_applies_policy_and_does_not_steal_focus():
    host = WebKitStatusHost(modules=fake_modules(), cert=project_cert_path(), injected=True)
    window = host.create_status_window()
    assert host.injected is True
    assert window is host.window
    assert window.accept_focus is STATUS_WINDOW_POLICY.accept_focus
    assert window.accept_focus is False
    assert window.keep_above is STATUS_WINDOW_POLICY.keep_above
    assert window.skip_taskbar is STATUS_WINDOW_POLICY.skip_taskbar
    assert window.skip_pager is STATUS_WINDOW_POLICY.skip_pager
    assert window.decorated is STATUS_WINDOW_POLICY.decorated
    assert window.type_hint == FakeGdk.WindowTypeHint.UTILITY
    assert window.presented is False
    assert window.grabbed is False
    assert host.context.tls_policy == fake_modules()[2].TLSErrorsPolicy.FAIL
    assert host.context.tls_policy != fake_modules()[2].TLSErrorsPolicy.IGNORE
    hosts = [item[1] for item in host.context.allowed]
    assert hosts == ["127.0.0.1", "localhost"]
    assert all(getattr(item[0], "path", None) == str(project_cert_path()) for item in host.context.allowed)
    assert host.webview.context is host.context
    host.destroy()
    assert window.destroyed is True


def test_navigate_uses_allowed_url_only():
    host = WebKitStatusHost(modules=fake_modules(), cert=project_cert_path(), injected=True)
    host.create_status_window()
    with pytest.raises(WebKitHostError):
        host.load("file:///tmp/x.html")
    with pytest.raises(WebKitHostError):
        host.load("http://127.0.0.1:17945/")
    with pytest.raises(WebKitHostError):
        host.load("https://example.com/")
    assert host.webview.loaded == []
    host.load("https://127.0.0.1:17945/voice")
    assert host.webview.loaded == ["https://127.0.0.1:17945/voice"]


def test_configure_context_requires_fail_and_project_cert(tmp_path):
    Gio, Gtk, WebKit2, Gdk = fake_modules()
    context = FakeWebContext()
    configure_webkit_context(context, cert=project_cert_path(), modules=(Gio, Gtk, WebKit2, Gdk))
    assert context.tls_policy == WebKit2.TLSErrorsPolicy.FAIL
    missing = tmp_path / "missing.pem"
    context2 = FakeWebContext()
    with pytest.raises(WebKitHostError, match="项目证书不存在"):
        configure_webkit_context(context2, cert=missing, modules=(Gio, Gtk, WebKit2, Gdk))


def test_apply_policy_is_single_source():
    window = FakeWindow()
    apply_status_window_policy(window, policy=STATUS_WINDOW_POLICY, Gdk=FakeGdk)
    assert window.accept_focus is False
    assert window.keep_above is True
    assert window.type_hint == "UTILITY"


def test_navigate_standalone_rejects_before_load():
    view = FakeWebView()
    with pytest.raises(WebKitHostError):
        navigate(view, "file:///")
    assert view.loaded == []
