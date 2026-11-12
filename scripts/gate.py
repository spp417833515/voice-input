#!/usr/bin/env python3
"""分层闸门：logic / business / api / platform / sidecar / integration / ui。
默认 --layer logic，不联网安装，不写入项目 .venv。
--layer all 含 logic,business,api,platform,sidecar,integration 与既有 node tests/ui/run.js。
sidecar 默认只跑 mock；官方短链需 SIDECAR_OFFICIAL=1 且显式启动宿主。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".pp" / "tmp" / "l1" / "venv-test"
REQ = ROOT / "tests" / "logic" / "requirements-l1.txt"
API_REQ = ROOT / "tests" / "api" / "requirements-api.txt"
NY = ZoneInfo("America/New_York")

LAYERS = {
    "logic": {
        "tests": ROOT / "tests" / "logic",
        "gate": ROOT / ".pp" / "gate" / "l1",
        "pytest_tmp": ROOT / ".pp" / "tmp" / "l1" / "pytest",
        "export_openapi": True,
    },
    "business": {
        "tests": ROOT / "tests" / "business",
        "gate": ROOT / ".pp" / "gate" / "business",
        "pytest_tmp": ROOT / ".pp" / "tmp" / "business" / "pytest",
        "export_openapi": False,
    },
    "api": {
        "tests": ROOT / "tests" / "api",
        "gate": ROOT / ".pp" / "gate" / "api",
        "pytest_tmp": ROOT / ".pp" / "tmp" / "api" / "pytest",
        "export_openapi": "app",
    },
    "platform": {
        "tests": ROOT / "tests" / "platform",
        "gate": ROOT / ".pp" / "gate" / "platform",
        "pytest_tmp": ROOT / ".pp" / "tmp" / "platform" / "pytest",
        "export_openapi": False,
    },
    "sidecar": {
        "tests": ROOT / "tests" / "sidecar",
        "gate": ROOT / ".pp" / "gate" / "sidecar",
        "pytest_tmp": ROOT / ".pp" / "tmp" / "sidecar" / "pytest",
        "export_openapi": False,
    },
    "integration": {
        "tests": ROOT / "tests" / "integration",
        "gate": ROOT / ".pp" / "gate" / "integration",
        "pytest_tmp": ROOT / ".pp" / "tmp" / "integration" / "pytest",
        "export_openapi": False,
    },
}

ALL_LAYERS = ("logic", "business", "api", "platform", "sidecar", "integration", "ui")


def now() -> str:
    return datetime.now(tz=NY).isoformat()


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Layered logic/business gate")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Create .pp/tmp/l1/venv-test and install tests/logic/requirements-l1.txt. Default is reuse-only, no install.",
    )
    parser.add_argument(
        "--layer",
        choices=("logic", "business", "api", "platform", "sidecar", "integration", "ui", "all"),
        default="logic",
        help="logic (default) / business / api / platform / sidecar / integration / ui; all includes all of these plus the existing node tests/ui entry. Official GPU sidecar stays opt-in.",
    )
    parser.add_argument(
        "--write-openapi-snapshot",
        action="store_true",
        help="Regenerate the single OpenAPI snapshot static/v3/openapi.json from the real app factory (api layer).",
    )
    return parser.parse_args(argv)


def resolve_python(*, bootstrap: bool) -> Path:
    py = VENV / "bin" / "python"
    pytest_bin = VENV / "bin" / "pytest"
    if bootstrap:
        if not py.exists():
            log(f"[{now()}] creating isolated test venv at {VENV} (not project .venv)")
            subprocess.run(
                ["uv", "venv", str(VENV), "--python", sys.executable],
                check=True,
            )
        log(f"[{now()}] installing isolated test deps from {REQ}")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(py), "-r", str(REQ)],
            check=True,
        )
        if API_REQ.exists():
            log(f"[{now()}] installing isolated API extras from {API_REQ}")
            subprocess.run(
                ["uv", "pip", "install", "--python", str(py), "-r", str(API_REQ)],
                check=True,
            )
        return py
    if not py.exists() or not pytest_bin.exists():
        log(
            f"[{now()}] isolated test env is not ready at {VENV}. "
            "This default run does not install packages and will not use the project .venv."
        )
        raise SystemExit(
            "Isolated test environment is not ready at "
            f"{VENV}. Run: python3 scripts/gate.py --bootstrap"
        )
    log(f"[{now()}] using existing isolated venv {VENV} (no install)")
    return py


def export_openapi(python: Path, gate_dir: Path) -> Path:
    gate_dir.mkdir(parents=True, exist_ok=True)
    dest = gate_dir / "openapi-models.json"
    code = (
        "import json,sys; sys.path.insert(0, %r); from _contracts import build_openapi_skeleton; "
        "print(json.dumps(build_openapi_skeleton(), ensure_ascii=False, indent=2))"
        % str(ROOT)
    )
    out = subprocess.check_output([str(python), "-c", code], text=True)
    dest.write_text(out, encoding="utf-8")
    spec = json.loads(out)
    if spec.get("paths"):
        raise SystemExit("L1 OpenAPI skeleton must not invent paths")
    schemas = spec["components"]["schemas"]
    for required in (
        "SessionPartialEvent",
        "ProductPartialEvent",
        "ProductIdleEvent",
        "JobQueryUnknown",
        "UtteranceResendNeedsConfirmation",
        "ErrorBody",
    ):
        if required not in schemas:
            raise SystemExit(f"missing schema {required}")
    return dest



REQUIRED_API_PATHS = (
    "/api/v1/recognize/sessions",
    "/api/v1/recognize/sessions/{session_id}/chunks",
    "/api/v1/recognize/sessions/{session_id}/events",
    "/api/v1/recognize/sessions/{session_id}/finish",
    "/api/v1/recognize/sessions/{session_id}/cancel",
    "/api/v1/utterances",
    "/api/v1/utterances/from-audio",
    "/api/v1/utterances/resend",
    "/api/v1/jobs/claim",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/ack",
    "/api/v1/stt",
    "/api/v1/polish",
    "/api/v1/translate",
    "/api/v1/ocr-translate",
    "/api/v1/history",
    "/api/v1/events",
    "/api/v1/events/platform",
    "/api/v1/config",
    "/api/v1/status",
    "/api/setup/check",
    "/api/setup/tasks",
    "/api/setup/tasks/{task_id}",
    "/api/v1/lan-access",
)

# 一次切流后必须不存在的旧路径（不保留包装或 redirect）。
FORBIDDEN_API_PATHS = (
    "/api/transcribe",
    "/api/ocr_translate",
    "/api/setup/pull_model",
    "/api/setup/delete_model",
    "/api/setup/install_ollama",
    "/api/setup/start_ollama",
    "/api/setup/preload_whisper",
    "/api/setup/delete_whisper",
    "/api/setup/set_config",
)
FORBIDDEN_API_PREFIXES = ("/api/web_input/",)
OPENAPI_SNAPSHOT = ROOT / "static" / "v3" / "openapi.json"


def export_api_openapi(python: Path, gate_dir: Path, *, write_snapshot: bool = False) -> Path:
    """从真实 FastAPI factory 导出 OpenAPI，并与唯一快照 static/v3/openapi.json 全文比对。

    快照缺失或不一致即失败；只有显式 --write-openapi-snapshot 才由本函数重写快照，禁止手改。
    """
    gate_dir.mkdir(parents=True, exist_ok=True)
    sandbox_a = ROOT / ".pp" / "tmp" / "api" / "openapi-a"
    sandbox_b = ROOT / ".pp" / "tmp" / "api" / "openapi-b"
    dest = gate_dir / "openapi.json"
    paths_txt = gate_dir / "openapi-paths.txt"
    code = (
        "import json,sys; sys.path.insert(0, %r); from pathlib import Path; "
        "from _v3_app import export_openapi_document; "
        "print(json.dumps(export_openapi_document(root=Path(sys.argv[1])), ensure_ascii=False, indent=2))"
        % str(ROOT)
    )

    def _gen(root: Path) -> dict:
        root.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        out = subprocess.check_output(
            [str(python), "-c", code, str(root)],
            text=True,
            cwd=str(ROOT),
            env=env,
        )
        return json.loads(out)

    first = _gen(sandbox_a)
    second = _gen(sandbox_b)
    if set(first.get("paths") or {}) != set(second.get("paths") or {}):
        raise SystemExit("API OpenAPI generation is not consistent between two sandboxes")
    missing = [path for path in REQUIRED_API_PATHS if path not in first.get("paths", {})]
    if missing:
        raise SystemExit("API OpenAPI missing paths: " + ", ".join(missing))
    stale = [
        path
        for path in first.get("paths", {})
        if path in FORBIDDEN_API_PATHS or path.startswith(FORBIDDEN_API_PREFIXES)
    ]
    if stale:
        raise SystemExit("API OpenAPI still exposes removed legacy paths: " + ", ".join(stale))
    schemas = (first.get("components") or {}).get("schemas") or {}
    for required in (
        "SessionSSEEventEnvelope",
        "ProductSSEEventEnvelope",
        "ErrorBody",
        "ApiStatusResponse",
        "SetupCheckResponse",
        "SetupTask",
        "LanAccessResponse",
        "AppConfigView",
    ):
        if required not in schemas:
            raise SystemExit(f"API OpenAPI missing schema {required}")
    rendered = json.dumps(first, ensure_ascii=False, indent=2) + "\n"
    dest.write_text(rendered, encoding="utf-8")
    paths_txt.write_text("\n".join(sorted(first["paths"])) + "\n", encoding="utf-8")
    if write_snapshot:
        OPENAPI_SNAPSHOT.write_text(rendered, encoding="utf-8")
        log(f"[{now()}] wrote OpenAPI snapshot {OPENAPI_SNAPSHOT}")
    if not OPENAPI_SNAPSHOT.exists():
        raise SystemExit(
            f"missing OpenAPI snapshot {OPENAPI_SNAPSHOT}; regenerate with scripts/gate.py --layer api --write-openapi-snapshot"
        )
    if json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8")) != first:
        raise SystemExit(
            "static/v3/openapi.json differs from generated API OpenAPI; regenerate with --write-openapi-snapshot, do not hand-edit"
        )
    return dest




def run_tests(python: Path, *, test_dir: Path, gate_dir: Path, pytest_tmp: Path) -> dict:
    pytest_tmp.mkdir(parents=True, exist_ok=True)
    gate_dir.mkdir(parents=True, exist_ok=True)
    junit = gate_dir / "pytest-junit.xml"
    log_path = gate_dir / "pytest.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["TMPDIR"] = str(pytest_tmp)
    env["PYTEST_ADDOPTS"] = ""
    cmd = [
        str(python),
        "-m",
        "pytest",
        str(test_dir),
        "-q",
        "--tb=short",
        "--junitxml",
        str(junit),
    ]
    log(f"[{now()}] running: {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    counts = _parse_pytest_counts(output, junit)
    counts["exit_code"] = proc.returncode
    counts["log"] = str(log_path)
    return counts


def _parse_pytest_counts(output: str, junit: Path) -> dict:
    passed = failed = skipped = errors = 0
    if junit.exists():
        import xml.etree.ElementTree as ET

        root = ET.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if root.tag == "testsuites" and not suites:
            suites = list(root)
        for suite in suites:
            passed += int(suite.attrib.get("tests", "0"))
            failed += int(suite.attrib.get("failures", "0"))
            errors += int(suite.attrib.get("errors", "0"))
            skipped += int(suite.attrib.get("skipped", "0"))
        passed = passed - failed - errors - skipped
    else:
        import re

        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) skipped", output)
        if m:
            skipped = int(m.group(1))
        m = re.search(r"(\d+) error", output)
        if m:
            errors = int(m.group(1))
    return {"passed": passed, "failed": failed, "skipped": skipped, "errors": errors}


def write_summary(
    counts: dict,
    *,
    gate_dir: Path,
    layer: str,
    bootstrap: bool,
    openapi_path: Path | None,
) -> Path:
    summary = gate_dir / "summary.txt"
    lines = [
        f"{layer} gate (America/New_York) {now()}",
        f"openapi: {openapi_path or '(not exported for this layer)'}",
        f"pytest passed={counts['passed']} failed={counts['failed']} skipped={counts['skipped']} errors={counts['errors']} exit={counts['exit_code']}",
        f"log: {counts['log']}",
        f"bootstrap_install: {bootstrap}",
        "notes:",
        "- Default gate does not install packages; use --bootstrap only to prepare the isolated venv.",
        "- --layer logic|business|api|platform|sidecar|integration|ui|all; all includes python layers plus tests/ui/run.js. Official GPU sidecar stays opt-in skip.",
        "- logic OpenAPI skeleton keeps empty paths; api layer exports real FastAPI paths to .pp/gate/api/openapi.json.",
        "- Live 4b / long-audio ASR must skip or not be marked green. Mocks are not real models.",
        "- Isolated test env: .pp/tmp/l1/venv-test ; project .venv was not modified.",
    ]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def _run_layer(python: Path, layer: str, *, bootstrap: bool, write_snapshot: bool = False) -> dict:
    cfg = LAYERS[layer]
    openapi_path = None
    if cfg["export_openapi"] == "app":
        openapi_path = export_api_openapi(python, cfg["gate"], write_snapshot=write_snapshot)
    elif cfg["export_openapi"]:
        openapi_path = export_openapi(python, cfg["gate"])
    counts = run_tests(
        python,
        test_dir=cfg["tests"],
        gate_dir=cfg["gate"],
        pytest_tmp=cfg["pytest_tmp"],
    )
    write_summary(
        counts,
        gate_dir=cfg["gate"],
        layer=layer,
        bootstrap=bootstrap,
        openapi_path=openapi_path,
    )
    return counts


def _run_ui_layer() -> dict:
    """运行既有 node tests/ui/run.js。不安装依赖，不改 tests/ui。失败不得吞掉。"""
    gate_dir = ROOT / ".pp" / "gate" / "ui"
    log_dir = ROOT / ".pp" / "tmp" / "ui"
    gate_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    script = ROOT / "tests" / "ui" / "run.js"
    log_path = gate_dir / "node.log"
    env = os.environ.copy()
    log(f"[{now()}] running: node {script}")
    proc = subprocess.run(
        ["node", str(script)],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    failed = 0 if proc.returncode == 0 else 1
    counts = {
        "passed": 0 if failed else 1,
        "failed": failed,
        "skipped": 0,
        "errors": 0,
        "exit_code": proc.returncode,
        "log": str(log_path),
    }
    write_summary(
        counts,
        gate_dir=gate_dir,
        layer="ui",
        bootstrap=False,
        openapi_path=None,
    )
    return counts


def _write_aggregate(results: list[tuple[str, dict]], *, exit_code: int) -> Path:
    dest_dir = ROOT / ".pp" / "gate" / "all"
    dest_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"aggregate gate (America/New_York) {now()}",
        f"exit={exit_code}",
        "layers:",
    ]
    for layer, counts in results:
        lines.append(
            f"- {layer}: passed={counts.get('passed')} failed={counts.get('failed')} "
            f"skipped={counts.get('skipped')} errors={counts.get('errors')} exit={counts.get('exit_code')}"
        )
    lines += [
        "notes:",
        "- Default is no-install; project .venv was not modified.",
        "- Official sidecar / long-audio / 4b remain opt-in skips, not green.",
        "- Isolated composition is not a real end-to-end production acceptance.",
        "- UI uses existing node tests/ui/run.js; this gate does not edit tests/ui.",
        "- Failure of any layer makes the aggregate exit non-zero; failures are not swallowed.",
        "- P2 long-audio refresh closed by r9 GPU evidence (user scope 0.5-1s, ~1.5s at word/sentence joins);",
        "  official sidecar opt-in tests still skip here and are not re-run by this gate.",
        "- Untested: official 4b (P3/P4) and coexistence, real TLS WebKit handshake,",
        "  mapped window focus, real mic/clipboard/keyboard, live 17945/17946/11434, production cutover.",
    ]
    path = dest_dir / "summary.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(path.read_text(encoding="utf-8"))
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    python = resolve_python(bootstrap=args.bootstrap)
    layers = ALL_LAYERS if args.layer == "all" else (args.layer,)
    exit_code = 0
    results: list[tuple[str, dict]] = []
    for layer in layers:
        if layer == "ui":
            counts = _run_ui_layer()
        else:
            counts = _run_layer(python, layer, bootstrap=args.bootstrap, write_snapshot=args.write_openapi_snapshot)
        results.append((layer, counts))
        if counts["exit_code"] != 0 or counts["failed"] or counts["errors"]:
            exit_code = counts["exit_code"] or 1
    if args.layer == "all":
        _write_aggregate(results, exit_code=exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
