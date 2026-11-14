"""Общие помощники: конфиг, состояние, логи, UTF-8 вывод."""
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"
LOGS = ROOT / "logs"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


DEFAULT_STATE = {
    "task": "",
    "shot": 0,
    "screen": {},
    "ui": [],
    "chats": {
        "gemini": {"url": None, "manifest_sent": False, "messages": 0},
        "chatgpt": {"url": None, "manifest_sent": False, "messages": 0},
    },
    "manifest_urls": [],
    "notes": [],
}


class StateError(RuntimeError):
    """Операция остановлена: состояние недоступно или повреждено."""


@contextmanager
def _state_lock():
    """Общий для pdd.py и chat.py межпроцессный lock коротких транзакций."""
    lock_path = STATE_PATH.with_name(STATE_PATH.name + ".lock")
    with open(lock_path, "a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise StateError("Не удалось заблокировать состояние; операция отменена") from exc
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_state_unlocked() -> dict:
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("Файл состояния недоступен или повреждён; исходный файл не изменён") from exc
    else:
        st = {}
    if not isinstance(st, dict) or ("chats" in st and not isinstance(st["chats"], dict)):
        raise StateError("Неожиданная структура состояния; исходный файл не изменён")
    for k, v in DEFAULT_STATE.items():
        if k not in st:
            st[k] = json.loads(json.dumps(v))
    for p in ("gemini", "chatgpt"):
        st["chats"].setdefault(p, {"url": None, "manifest_sent": False, "messages": 0})
        if not isinstance(st["chats"][p], dict):
            raise StateError("Неожиданная структура чата; исходный файл не изменён")
    return st


def _write_state_unlocked(st: dict) -> None:
    if not isinstance(st, dict):
        raise StateError("Состояние должно быть объектом; запись отменена")
    text = json.dumps(st, ensure_ascii=False, indent=2)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE_PATH.parent,
                                         prefix=".pdd-state-", suffix=".tmp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, STATE_PATH)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def load_state() -> dict:
    with _state_lock():
        return _read_state_unlocked()


def save_state(st: dict) -> None:
    """Совместимость: атомарная запись всего объекта; для обновлений использовать update_state."""
    with _state_lock():
        _write_state_unlocked(st)


def update_state(callback):
    """Прочитать, изменить и записать состояние одной короткой транзакцией."""
    with _state_lock():
        st = _read_state_unlocked()
        result = callback(st)
        _write_state_unlocked(st)
        return result


def log_dir(name: str) -> Path:
    d = LOGS / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def out(obj) -> None:
    """Печать результата: dict -> JSON, остальное как есть."""
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    else:
        print(obj)
    sys.stdout.flush()
