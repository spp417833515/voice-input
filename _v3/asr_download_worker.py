"""在隔离 ASR 环境中运行的下载进程（只依赖标准库与 huggingface_hub）。

用法: python asr_download_worker.py <repo_id> <local_dir>
stdout 输出 JSON 行：{"total_bytes": N}，结束 {"done": true}。失败非零退出，原因写 stderr。
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: asr_download_worker.py <repo_id> <local_dir>", file=sys.stderr)
        return 2
    repo_id, local_dir = argv[1], argv[2]
    from huggingface_hub import HfApi, snapshot_download

    info = HfApi().model_info(repo_id, files_metadata=True)
    total = sum(int(s.size or 0) for s in info.siblings or [])
    print(json.dumps({"total_bytes": total}), flush=True)
    snapshot_download(repo_id=repo_id, local_dir=local_dir)
    print(json.dumps({"done": True}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
