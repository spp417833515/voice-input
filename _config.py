"""配置存取的唯一目标实现。迁移时保留未知/旧字段；不在 live 配置上做试验。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from _contracts import (
    PRODUCT_OLLAMA_MODEL,
    AppConfig,
    AppConfigUpdate,
    AppConfigView,
    ErrorCode,
    LogicError,
)

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).absolute())
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def default_config_path() -> Path:
    return Path(__file__).resolve().parent / ".config.json"


class ConfigStore:
    """对指定路径做读写。测试必须传入临时路径，禁止用默认 live 路径做试验。

    load / save / update 均走同一路径锁。update 的读改写在锁内完成，并发 update 不得互相覆盖字段。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = _lock_for(self.path)

    def _load_unlocked(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LogicError(ErrorCode.config_io, f"配置文件不是合法 JSON: {exc}") from exc
        except OSError as exc:
            raise LogicError(ErrorCode.config_io, f"配置文件无法读取: {exc}") from exc
        if not isinstance(data, dict):
            raise LogicError(ErrorCode.config_io, "配置文件根节点必须是对象")
        try:
            return AppConfig.model_validate(data)
        except Exception as exc:
            raise LogicError(ErrorCode.config_io, f"配置字段无法解析: {exc}") from exc

    def _save_unlocked(self, config: AppConfig) -> None:
        payload = config.model_dump(mode="json", exclude_none=False)
        # 保留模型声明之外、但当前文件里已有的键（双重保险：extra="allow" 已在 dump 中）。
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(self.path.parent))
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
            raise LogicError(ErrorCode.config_io, f"配置文件无法写入: {exc}") from exc

    def load(self) -> AppConfig:
        with self._lock:
            return self._load_unlocked()

    def save(self, config: AppConfig) -> None:
        with self._lock:
            self._save_unlocked(config)

    def update(self, **fields: object) -> AppConfig:
        """合并已知字段后写回，未知已有字段随 AppConfig extra 保留。读改写在路径锁内完成。"""
        with self._lock:
            current = self._load_unlocked()
            data = current.model_dump()
            data.update(fields)
            updated = AppConfig.model_validate(data)
            self._save_unlocked(updated)
            return updated


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        path = default_config_path()
    return ConfigStore(path).load()


def save_config(config: AppConfig, path: Path | None = None) -> None:
    if path is None:
        path = default_config_path()
    ConfigStore(path).save(config)


LEGACY_OLLAMA_MODEL_KEY = "legacy_ollama_model"
_HOTKEY_FIELDS = ("hotkey_voice", "hotkey_screenshot", "hotkey_repeat")


def config_view(config: AppConfig) -> AppConfigView:
    """配置对外视图：只含产品字段，不回传百度密钥、Whisper 等旧键。"""
    return AppConfigView(
        polish_enabled=config.polish_enabled is True,
        custom_keywords=list(config.custom_keywords),
        translate_target_lang=config.translate_target_lang,
        hotkey_voice=config.hotkey_voice,
        hotkey_screenshot=config.hotkey_screenshot,
        hotkey_repeat=config.hotkey_repeat,
        ollama_model=config.ollama_model,
    )


def _normalized_update(update: AppConfigUpdate) -> dict[str, object]:
    from _hotkey import validate_hotkey

    fields = update.model_dump(exclude_none=True)
    for name in _HOTKEY_FIELDS:
        if name in fields:
            try:
                fields[name] = validate_hotkey(str(fields[name]))
            except LogicError as exc:
                raise LogicError(
                    ErrorCode.invalid_request, f"{name}: {exc.message}", {**(exc.details or {}), "field": name}
                ) from exc
    if "custom_keywords" in fields:
        fields["custom_keywords"] = [str(item).strip() for item in fields["custom_keywords"] if str(item).strip()]
    return fields


def apply_config_update(store: ConfigStore, update: AppConfigUpdate) -> AppConfigView:
    """唯一配置写路径（设置页与 API 共用）。未知字段已由 AppConfigUpdate 拒绝。"""
    from _hotkey import ensure_distinct_hotkeys

    fields = _normalized_update(update)
    if not fields:
        return config_view(store.load())
    with store._lock:  # 与 update 同一把可重入锁：查重与写入之间不被并发写插入
        if any(name in fields for name in _HOTKEY_FIELDS):
            current = store.load()
            ensure_distinct_hotkeys({name: str(fields.get(name, getattr(current, name))) for name in _HOTKEY_FIELDS})
        return config_view(store.update(**fields))


def migrate_to_product_config(store: ConfigStore) -> tuple[AppConfig, bool]:
    """生产启动时的一次性迁移：保留已有快捷键/关键词/语言与所有旧键；

    只把纠错/翻译模型固定为产品模型，旧值另存为 legacy_ollama_model，不删除任何字段。
    返回 (迁移后配置, 是否写盘)。写失败抛 config_io，不吞错。
    """
    current = store.load()
    if current.ollama_model == PRODUCT_OLLAMA_MODEL:
        return current, False
    return store.update(
        ollama_model=PRODUCT_OLLAMA_MODEL,
        **{LEGACY_OLLAMA_MODEL_KEY: current.ollama_model},
    ), True
