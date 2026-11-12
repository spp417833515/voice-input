"""唯一生产界面入口：页面与静态资源只来自 static/v3/。不挂旧 static/，不设 redirect。

页面内资源用相对路径 js/…、css/…，因此资源目录挂在根下 /js、/css。
openapi 快照（static/v3/openapi.json）供局域网手机页读取，地址 /openapi.snapshot.json；
仅本机的 /openapi.json 仍由应用实时派生。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PAGES: dict[str, str] = {
    "/setup": "setup.html",
    "/voice": "voice.html",
    "/overlay-status": "overlay-status.html",
    "/overlay-translate": "overlay-translate.html",
}
ASSET_DIRS = ("js", "css")
OPENAPI_SNAPSHOT_FILE = "openapi.json"
OPENAPI_SNAPSHOT_PATH = "/openapi.snapshot.json"
LAN_PUBLIC_PAGES = frozenset({"/voice"})
_PAGE_HEADERS = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


def install_pages(app: FastAPI, static_v3: Path) -> None:
    root = Path(static_v3).resolve()
    missing = [name for name in (*PAGES.values(), *ASSET_DIRS, OPENAPI_SNAPSHOT_FILE) if not (root / name).exists()]
    if missing:
        raise RuntimeError(f"static/v3 缺少生产界面文件: {missing}")

    def page_route(file: Path):
        async def serve() -> FileResponse:
            return FileResponse(file, media_type="text/html; charset=utf-8", headers=_PAGE_HEADERS)

        return serve

    for url, name in PAGES.items():
        app.add_api_route(url, page_route(root / name), methods=["GET"], include_in_schema=False)

    snapshot = root / OPENAPI_SNAPSHOT_FILE

    async def openapi_snapshot() -> FileResponse:
        return FileResponse(snapshot, media_type="application/json", headers={"Cache-Control": "no-store"})

    app.add_api_route(OPENAPI_SNAPSHOT_PATH, openapi_snapshot, methods=["GET"], include_in_schema=False)
    for name in ASSET_DIRS:
        app.mount(f"/{name}", StaticFiles(directory=str(root / name)), name=f"v3_{name}")


def is_lan_public_path(path: str) -> bool:
    """局域网对端无需令牌即可读取的只读界面资源（手机录音页及其脚本样式与契约快照）。"""
    if path in LAN_PUBLIC_PAGES or path == OPENAPI_SNAPSHOT_PATH:
        return True
    return any(path.startswith(f"/{name}/") for name in ASSET_DIRS)
