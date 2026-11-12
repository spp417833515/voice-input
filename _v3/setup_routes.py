"""设置页 API：真实状态检查、异步安装/下载/启动作业、手机局域网入口签发。全部仅本机。"""
from __future__ import annotations

from fastapi import APIRouter

from _contracts import (
    ErrorCode,
    LanAccessResponse,
    LogicError,
    SetupCheckResponse,
    SetupTask,
    SetupTaskCreate,
)
from _v3.runtime import Runtime


def build_setup_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["setup"])

    def _setup():
        if runtime.setup is None:
            raise LogicError(ErrorCode.dependency_failed, "当前运行时未注入设置探测与安装动作")
        return runtime.setup

    @router.get("/api/setup/check", response_model=SetupCheckResponse)
    def setup_check() -> SetupCheckResponse:
        return _setup().check()

    @router.post("/api/setup/tasks", response_model=SetupTask, status_code=202)
    def setup_task_start(body: SetupTaskCreate) -> SetupTask:
        return _setup().tasks.start(body.kind)

    @router.get("/api/setup/tasks/{task_id}", response_model=SetupTask)
    def setup_task_get(task_id: str) -> SetupTask:
        return _setup().tasks.get(task_id)

    @router.get("/api/v1/lan-access", response_model=LanAccessResponse)
    def lan_access() -> LanAccessResponse:
        if runtime.lan is None:
            raise LogicError(ErrorCode.dependency_failed, "当前运行时未注入局域网入口签发")
        return runtime.lan.describe()

    return router
