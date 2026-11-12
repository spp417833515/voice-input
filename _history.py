"""转写历史（处理记录，不是投递记录）。兼容旧 history.jsonl；错误可见。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from _contracts import ErrorCode, HistoryListResponse, HistoryRecord, LogicError


def default_history_path() -> Path:
    return Path(__file__).resolve().parent / ".cache" / "history.jsonl"


class HistoryStore:
    """读写指定 jsonl。测试必须使用 fixture 路径，不得打开私人历史文件。

    本层不承诺并发多写者的原子追加或读改写；下一层若并行调用 append/list，必须自行串行化，
    不得假定本 API 已加锁。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: HistoryRecord) -> None:
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise LogicError(ErrorCode.history_io, f"历史记录无法写入: {exc}") from exc

    def list_records(self, limit: int = 50, offset: int = 0) -> HistoryListResponse:
        if limit < 0 or offset < 0:
            raise LogicError(ErrorCode.invalid_request, "limit 与 offset 不能为负")
        if not self.path.exists():
            return HistoryListResponse(records=[], total=0, skipped_malformed=0)
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LogicError(ErrorCode.history_io, f"历史记录无法读取: {exc}") from exc
        records: list[HistoryRecord] = []
        skipped = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(HistoryRecord.model_validate(data))
            except Exception:
                skipped += 1
        # 与旧接口一致：最新在前
        records.reverse()
        total = len(records)
        return HistoryListResponse(
            records=records[offset : offset + limit],
            total=total,
            skipped_malformed=skipped,
        )
