"""§8.2-9 生产入口与启动链快照：薄 app.py / daemon.py、systemd 单元渲染、启动脚本。

不调用 systemctl、不启动服务、不碰 live 17945/17946/11434；单元渲染到临时目录。
"""
from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _v3.production import ACCEPTANCE_ENV, CODE_ROOT, build_production_app
from _v3.runtime import UnmetRuntimeGates
from _v3.units import UNIT_NAMES, render_all, write_units

ROOT = CODE_ROOT
LEGACY_MARKERS = ("faster_whisper", "deep_translator", "web_input", "/api/transcribe", "whisper_model", "WhisperModel")


def _rendered(tmp_path: Path, cuda: str = "") -> dict[str, str]:
    return render_all(project_dir=tmp_path / "proj", venv=tmp_path / "proj" / ".venv", cuda_libs=cuda)


def test_units_render_all_four_without_placeholders(tmp_path):
    units = _rendered(tmp_path)
    assert set(units) == set(UNIT_NAMES)
    for name, text in units.items():
        assert "@" not in "".join(l for l in text.splitlines() if not l.startswith("#")), name
        # 不得残留旧 unit 写死的项目路径 / python3.12 NVIDIA 库路径
        assert "/home/spp/桌面/ollama-voice-input/.venv/lib/python3.12" not in text, name
        assert "python3.12/site-packages/nvidia" not in text, name
        assert "qwen2.5:7b" not in text, name
        for line in text.splitlines():
            if line.startswith("ExecStart"):
                assert "sg" not in line.split(), (name, line)


def test_server_unit_https_factory_and_dynamic_cuda(tmp_path):
    server = _rendered(tmp_path, cuda="/x/nvidia/cublas/lib")["ollama-voice-server.service"]
    assert "--factory app:create_production_app" in server
    assert "--ssl-certfile" in server and "--ssl-keyfile" in server and "--port 17945" in server
    assert "Environment=LD_LIBRARY_PATH=/x/nvidia/cublas/lib" in server
    assert ACCEPTANCE_ENV not in server  # 正式单元不带验收开关
    plain = _rendered(tmp_path)["ollama-voice-server.service"]
    assert "LD_LIBRARY_PATH" not in plain


def test_daemon_unit_keeps_notify_watchdog_and_no_sg(tmp_path):
    daemon = _rendered(tmp_path)["ollama-voice-daemon.service"]
    exec_line = next(l for l in daemon.splitlines() if l.startswith("ExecStart="))
    argv = exec_line.split("=", 1)[1].split()
    assert argv == [str(tmp_path / "proj" / ".venv" / "bin" / "python3"), str((tmp_path / "proj").resolve() / "daemon.py")]
    for line in ("Type=notify", "NotifyAccess=main", "WatchdogSec=20", "Environment=GDK_BACKEND=x11"):
        assert line in daemon


def test_asr_and_ollama_units_loopback_and_guarded(tmp_path):
    units = _rendered(tmp_path)
    asr = units["ollama-voice-asr.service"]
    assert "-m runtimes.asr serve --host 127.0.0.1 --port 17946 --load" in asr
    assert "ConditionPathExists=" in asr and "Qwen3-ASR-1.7B/config.json" in asr
    assert "HF_HUB_OFFLINE=1" in asr
    ollama = units["ollama-voice-ollama.service"]
    assert "OLLAMA_HOST=127.0.0.1:11434" in ollama and "ollama-bin-0.40.16/bin/ollama serve" in ollama


def test_write_units_to_temp_dir_only(tmp_path):
    out = tmp_path / "user-units"
    written = write_units(out, _rendered(tmp_path))
    assert sorted(p.name for p in written) == sorted(UNIT_NAMES)
    assert all(p.parent == out for p in written)


@pytest.mark.parametrize("script", ["start.sh", "install.sh", "restart.sh", "uninstall.sh", "gen_cert.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True, timeout=30)


def test_start_script_snapshot():
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "qwen2.5:7b" not in text and "curl -fsSL" not in text and "http://127.0.0.1:17945" not in text
    assert "https://127.0.0.1:17945/setup" in text
    assert "ollama-voice-asr.service" in text and "ollama-voice-ollama.service" in text
    assert "--acceptance" in text and "OVI_ACCEPTANCE_RUN=1" in text
    assert "_activation.py\" check" in text  # 门闩先于副作用（行为见 test_entry_scripts_gate）
    for key in ("Ctrl+Alt+Z", "Ctrl+Alt+X", "Ctrl+Alt+R"):
        assert key in text


def test_install_script_snapshot():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "-m _v3.units" in text
    assert "uvicorn app:app" not in text and "whisper" not in text.lower()
    assert "ollama-voice-input-daemon.desktop" in text  # portal app id 文件保留
    for pkg in ("xclip", "python3-dbus", "xdotool"):
        assert pkg in text
    assert "curl -fsSL https://ollama.com/install.sh" not in text
    assert "https://127.0.0.1:17945/setup" in text


def test_requirements_drop_whisper_keep_av():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "faster-whisper" not in req and "deep-translator" not in req
    assert "av==18.0.0" in req


def test_desktop_file_keywords():
    text = (ROOT / "ollama-voice-input.desktop").read_text(encoding="utf-8")
    assert "whisper" not in text and "./start.sh" in text


def test_old_static_pages_removed_only_v3_served():
    for name in ("setup.html", "voice.html", "history.html", "api.html", "voice_setup.html"):
        assert not (ROOT / "static" / name).exists(), name
    assert (ROOT / "static" / "v3" / "setup.html").is_file()


def _defs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]


def test_app_entry_is_thin_and_side_effect_free():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert _defs(ROOT / "app.py") == ["create_production_app"]
    for marker in LEGACY_MARKERS:
        assert marker not in src
    mod = importlib.import_module("app")
    assert not hasattr(mod, "app"), "导入 app 不得装配 live 应用"


def test_daemon_entry_is_thin():
    path = ROOT / "daemon.py"
    src = path.read_text(encoding="utf-8")
    assert _defs(path) == ["load_bindings", "main"]
    assert len(src.splitlines()) < 220
    for marker in LEGACY_MARKERS + ("/api/web_input", "TranslationOverlay", "StatusBar("):
        assert marker not in src
    for needed in ("_desktop.", "_linux.", "sd_notify(\"READY=1\")", "IdleSafetyWatchdog", "DeliveryLoop", "StatusWindow"):
        assert needed in src


def test_build_production_app_gated_then_acceptance(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    env = {"XDG_SESSION_TYPE": "x11"}
    with pytest.raises(UnmetRuntimeGates):
        build_production_app(data_root=root, environ=env)
    app, paths = build_production_app(data_root=root, environ={**env, ACCEPTANCE_ENV: "1"})
    assert paths.data_root == root.resolve()
    assert paths.token.is_file() and (paths.token.stat().st_mode & 0o777) == 0o600
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945")
    body = client.get("/api/v1/status", headers={"Origin": "https://127.0.0.1:17945"}).json()
    assert body["unmet_gates"], "验收运行仍必须显示未达门闩"
    assert client.get("/setup").status_code == 200
    for old in ("/api/transcribe", "/api/web_input/poll", "/voice_setup", "/api_docs"):
        assert client.get(old).status_code in (403, 404, 405), old
    client.close()
