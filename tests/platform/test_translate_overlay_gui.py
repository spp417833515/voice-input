"""真实 WebKit 译文浮层（Xvfb 隔离 GUI，opt-in：OVI_GUI_PROBE=1）。

真实 static/v3 页面 + 真实 TranslateWindow 私有桥 + 真实 GLib 10s。证明：
- API 2xx 已返回且 platform idle 响应被挂起时，DOM 已呈现结果、宿主才收到 presented，10s 从此刻起算；
- 配置初始化失败：页内错误 + presented(error)，不等 30s。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).resolve().parent / "gui_translate_overlay_probe.py"
PY = ROOT / ".venv" / "bin" / "python3"

pytestmark = pytest.mark.skipif(
    os.environ.get("OVI_GUI_PROBE") != "1" or shutil.which("xvfb-run") is None or not PY.exists(),
    reason="opt-in：OVI_GUI_PROBE=1 且有 xvfb-run 与生产 .venv",
)


def _run(scenario: str, tmp_path: Path) -> dict:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "NO_AT_BRIDGE": "1",
        "OVI_XVFB": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    for d in ("home", "data", "cache", "config", "run"):
        (tmp_path / d).mkdir(exist_ok=True)
    (tmp_path / "run").chmod(0o700)
    out = subprocess.run(
        ["xvfb-run", "-a", "-s", "-screen 0 1280x800x24", str(PY), str(PROBE), scenario],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(ROOT),
    )
    line = next((l for l in out.stdout.splitlines() if l.startswith("PROBE_JSON ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    data = json.loads(line[len("PROBE_JSON "):])
    (tmp_path / f"{scenario}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(json.dumps(data, ensure_ascii=False))
    return data


def test_gui_result_rendered_before_presented_and_10s_from_there(tmp_path):
    d = _run("idle_pending", tmp_path)
    assert not d.get("hard_timeout")
    assert [p["kind"] for p in d["presented"]] == ["result"]
    t_p = d["presented"][0]["t"]
    dom = d["dom_at_presented"]
    assert dom["phase"] == "result" and dom["text"] == "Hello" and dom["gen"] == "string"
    idle = [r for r in d["requests"] if r.get("event") == "idle"]
    ocr = [r for r in d["requests"] if r["path"] == "/api/v1/ocr-translate"]
    assert len(ocr) == 1 and ocr[0]["t"] <= t_p
    # idle 上报在 presented 时尚未得到响应（挂起到窗口关闭后才放行）
    assert idle and idle[0].get("responded", 1e9) >= d["closed"]
    assert 9.9 <= d["closed"] - t_p <= 10.8, (t_p, d["closed"])


def test_gui_config_failure_presents_error_without_30s(tmp_path):
    d = _run("config_fail", tmp_path)
    assert not d.get("hard_timeout")
    assert [p["kind"] for p in d["presented"]] == ["error"]
    t_p = d["presented"][0]["t"]
    assert d["dom_at_presented"]["phase"] == "error" and d["dom_at_presented"]["alert"] is True
    assert not any(r["path"] == "/api/v1/ocr-translate" for r in d["requests"])
    assert t_p - d["opened"] < 5.0
    assert 9.9 <= d["closed"] - t_p <= 10.8
    assert d["closed"] - d["opened"] < 16.0
    assert not any(s.get("error") == "timeout" for s in d["host_status"])
