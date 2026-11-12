"""systemd 用户单元的唯一渲染器：模板在 systemd/*.service，install.sh 调用本模块写入用户目录。

本模块不调用 systemctl；测试把输出目录指到临时目录做快照比对。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

from _v3.production import CODE_ROOT, ProductionPaths

UNIT_NAMES = (
    "ollama-voice-server.service",
    "ollama-voice-daemon.service",
    "ollama-voice-asr.service",
    "ollama-voice-ollama.service",
)
TEMPLATE_DIR = CODE_ROOT / "systemd"


def unit_values(*, project_dir: Path, venv: Path, cuda_libs: str = "") -> dict[str, str]:
    paths = ProductionPaths.for_root(project_dir, live=True)
    return {
        "@DIR@": str(paths.data_root),
        "@VENV@": str(venv),
        "@CUDA_ENV@": f"Environment=LD_LIBRARY_PATH={cuda_libs}" if cuda_libs else "",
        "@ASR_MODEL_DIR@": str(paths.asr_model_dir),
        "@ASR_PYTHON@": str(paths.asr_python),
        "@HF_HOME@": str(paths.hf_home),
        "@OLLAMA_BIN@": str(paths.ollama_binary),
        "@OLLAMA_MODELS@": str(paths.ollama_models),
    }


def render_unit(template: str, values: Mapping[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace(key, value)
    lines = [line for line in out.splitlines() if not line.startswith("# 模板：")]
    # 空的 @CUDA_ENV@ 只留下空行：去掉以免 unit 中出现无意义空行堆叠
    text = "\n".join(lines).replace("\n\n\n", "\n\n")
    if _placeholders(text):
        raise ValueError(f"模板存在未替换的占位符: {_placeholders(text)}")
    return text.rstrip("\n") + "\n"


def _placeholders(text: str) -> str:
    import re

    return " ".join(sorted(set(re.findall(r"@[A-Z_]+@", text))))


def render_all(*, project_dir: Path, venv: Path, cuda_libs: str = "", template_dir: Path = TEMPLATE_DIR) -> dict[str, str]:
    values = unit_values(project_dir=project_dir, venv=venv, cuda_libs=cuda_libs)
    return {name: render_unit((template_dir / name).read_text(encoding="utf-8"), values) for name in UNIT_NAMES}


def write_units(out_dir: Path, rendered: Mapping[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in rendered.items():
        target = out_dir / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染 systemd 用户单元（不调用 systemctl）")
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--cuda-libs", default="")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    for path in write_units(args.out_dir, render_all(project_dir=args.project_dir, venv=args.venv, cuda_libs=args.cuda_libs)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
