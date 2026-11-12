"""jobs.json 存取层：原子写、并发安全。不做业务状态决策或重投。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from _contracts import ErrorCode, JobRecord, JobStoreDocument, LogicError

try:
    import fcntl
except ImportError:  # pragma: no cover — L1 目标为 Linux
    fcntl = None  # type: ignore[assignment]


def default_jobs_path() -> Path:
    return Path(__file__).resolve().parent / ".cache" / "jobs.json"


class JobStore:
    """最小持久化。claim/ack 的业务转移由后续业务层完成，本层只校验 schema 并保存。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._thread_lock = threading.RLock()
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def _empty(self) -> JobStoreDocument:
        return JobStoreDocument()

    def _read_unlocked(self) -> JobStoreDocument:
        if not self.path.exists():
            return self._empty()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LogicError(ErrorCode.jobs_io, f"jobs.json 无法读取: {exc}") from exc
        if not raw.strip():
            return self._empty()
        try:
            data = json.loads(raw)
            return JobStoreDocument.model_validate(data)
        except Exception as exc:
            raise LogicError(ErrorCode.jobs_io, f"jobs.json 损坏或不符合 schema: {exc}") from exc

    def _write_unlocked(self, doc: JobStoreDocument) -> None:
        text = json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".jobs.", suffix=".tmp", dir=str(self.path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise LogicError(ErrorCode.jobs_io, f"jobs.json 无法写入: {exc}") from exc

    def _lock_file(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _with_lock(self, fn):
        with self._thread_lock:
            fh = self._lock_file()
            try:
                return fn()
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                fh.close()

    def load(self) -> JobStoreDocument:
        return self._with_lock(self._read_unlocked)

    def get(self, job_id: str) -> JobRecord | None:
        doc = self.load()
        return doc.jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        doc = self.load()
        return list(doc.jobs.values())

    def put(self, record: JobRecord) -> JobRecord:
        validated = JobRecord.model_validate(record.model_dump())

        def _op() -> JobRecord:
            doc = self._read_unlocked()
            doc.jobs[validated.job_id] = validated
            self._write_unlocked(doc)
            return validated

        return self._with_lock(_op)

    def update(self, job_id: str, **fields: object) -> JobRecord:
        def _op() -> JobRecord:
            doc = self._read_unlocked()
            current = doc.jobs.get(job_id)
            if current is None:
                raise LogicError(ErrorCode.not_found, f"job 不存在: {job_id}")
            data = current.model_dump()
            data.update(fields)
            data["job_id"] = job_id
            try:
                updated = JobRecord.model_validate(data)
            except Exception as exc:
                raise LogicError(ErrorCode.invalid_request, f"job 更新不符合 schema: {exc}") from exc
            doc.jobs[job_id] = updated
            self._write_unlocked(doc)
            return updated

        return self._with_lock(_op)

    def update_if(
        self,
        job_id: str,
        *,
        expected: dict[str, object],
        fields: dict[str, object],
    ) -> JobRecord:
        """锁内按字段等值条件更新。条件不满足则 conflict，找不到则 not_found。

        只比较调用方给出的字段快照，不解释哪些状态转移合法，也不自动 claim/ack。
        """

        def _norm(value: object) -> object:
            return value.value if hasattr(value, "value") else value

        def _op() -> JobRecord:
            doc = self._read_unlocked()
            current = doc.jobs.get(job_id)
            if current is None:
                raise LogicError(ErrorCode.not_found, f"job 不存在: {job_id}")
            data = current.model_dump()
            for key, want in expected.items():
                if _norm(data.get(key)) != _norm(want):
                    raise LogicError(
                        ErrorCode.conflict,
                        f"job 条件更新不匹配: {key}",
                        details={
                            "job_id": job_id,
                            "field": key,
                            "expected": _norm(want),
                            "actual": _norm(data.get(key)),
                        },
                    )
            data.update(fields)
            data["job_id"] = job_id
            try:
                updated = JobRecord.model_validate(data)
            except Exception as exc:
                raise LogicError(ErrorCode.invalid_request, f"job 更新不符合 schema: {exc}") from exc
            doc.jobs[job_id] = updated
            self._write_unlocked(doc)
            return updated

        return self._with_lock(_op)

    def put_if_absent(self, record: JobRecord) -> JobRecord:
        """锁内仅在 job_id 不存在时写入。已存在则 conflict。不做业务状态解释。"""
        validated = JobRecord.model_validate(record.model_dump())

        def _op() -> JobRecord:
            doc = self._read_unlocked()
            if validated.job_id in doc.jobs:
                raise LogicError(
                    ErrorCode.conflict,
                    f"job 已存在: {validated.job_id}",
                    details={"job_id": validated.job_id},
                )
            doc.jobs[validated.job_id] = validated
            self._write_unlocked(doc)
            return validated

        return self._with_lock(_op)
