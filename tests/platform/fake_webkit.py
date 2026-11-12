"""确定性 GTK/WebKit mock。仅测试用，不是真实 gi / 不是 TLS 握手。"""
from __future__ import annotations


class FakeTlsCertificate:
    def __init__(self, path: str) -> None:
        self.path = path


class FakeGio:
    class TlsCertificate:
        @staticmethod
        def new_from_file(path: str) -> FakeTlsCertificate:
            return FakeTlsCertificate(path)


class FakeWindow:
    def __init__(self, type=None, **_kwargs) -> None:
        self.window_type = type
        self.accept_focus = None
        self.keep_above = None
        self.skip_taskbar = None
        self.skip_pager = None
        self.type_hint = None
        self.decorated = None
        self.child = None
        self.destroyed = False
        self.presented = False
        self.grabbed = False

    def set_accept_focus(self, value) -> None:
        self.accept_focus = value

    def get_accept_focus(self):
        return self.accept_focus

    def set_keep_above(self, value) -> None:
        self.keep_above = value

    def set_skip_taskbar_hint(self, value) -> None:
        self.skip_taskbar = value

    def set_skip_pager_hint(self, value) -> None:
        self.skip_pager = value

    def set_type_hint(self, value) -> None:
        self.type_hint = value

    def set_decorated(self, value) -> None:
        self.decorated = value

    def add(self, child) -> None:
        self.child = child

    def destroy(self) -> None:
        self.destroyed = True

    def present(self) -> None:
        self.presented = True

    def grab_focus(self) -> None:
        self.grabbed = True

    def show_all(self) -> None:
        return None


class FakeGtk:
    class WindowType:
        TOPLEVEL = "TOPLEVEL"

    Window = FakeWindow


class FakeWebContext:
    def __init__(self) -> None:
        self.tls_policy = None
        self.allowed: list[tuple[object, str]] = []

    @classmethod
    def new(cls) -> "FakeWebContext":
        return cls()

    def set_tls_errors_policy(self, policy) -> None:
        self.tls_policy = policy

    def allow_tls_certificate_for_host(self, cert, host: str) -> None:
        self.allowed.append((cert, host))


class FakeWebView:
    def __init__(self, context=None) -> None:
        self.context = context
        self.loaded: list[str] = []

    @classmethod
    def new_with_context(cls, context) -> "FakeWebView":
        return cls(context)

    def load_uri(self, url: str) -> None:
        self.loaded.append(url)


class FakeWebKit2:
    class TLSErrorsPolicy:
        FAIL = "FAIL"
        IGNORE = "IGNORE"

    WebContext = FakeWebContext
    WebView = FakeWebView


class FakeGdk:
    class WindowTypeHint:
        UTILITY = "UTILITY"
        DIALOG = "DIALOG"
        NORMAL = "NORMAL"


def fake_modules():
    """返回注入给 WebKitStatusHost 的 mock 四元组。"""
    return FakeGio, FakeGtk, FakeWebKit2, FakeGdk
