"""生产 Web 入口（薄）：uvicorn --factory app:create_production_app。

唯一装配在 _v3.production.build_production_app：v1 API + static/v3 界面 + 统一鉴权。
导入本模块没有副作用；只有 uvicorn 调用工厂时才绑定 live 数据与令牌。
门闩未关且非验收运行时以 _activation.EXIT_GATED(78) 退出（单元 RestartPreventExitStatus=78，不崩溃循环）。
"""
from __future__ import annotations

import os
import sys


def create_production_app():
    from _activation import EXIT_GATED, exit_if_blocked
    from _v3.production import build_production_app
    from _v3.runtime import UnmetRuntimeGates

    exit_if_blocked(os.environ)
    try:
        app, _paths = build_production_app(environ=os.environ)
    except UnmetRuntimeGates as exc:
        print(f"生产门闩未关闭，拒绝启动：{exc}", file=sys.stderr)
        raise SystemExit(EXIT_GATED) from None
    return app
