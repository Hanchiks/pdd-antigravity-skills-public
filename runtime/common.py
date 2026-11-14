"""Общие помощники: конфиг, состояние, логи, UTF-8 вывод.

Порядок межпроцессных блокировок: сначала блокировка устройства
(``device_lock``) или черновика чата (``draft_lock``), затем блокировка
состояния (``_state_lock``). Обратный порядок запрещён: короткие транзакции
состояния не должны ждать устройство или черновик. Блокировка конфигурации
(``_config_lock``) независима и никогда не берётся под блокировкой состояния;
если когда-либо понадобятся обе, первой берётся конфигурация.
"""
import errno
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def _path_from_env(name: str, default: Path) -> Path:
    """Тестовые/автоматизационные переопределения путей.

    Переменные ``PDD_CONFIG_PATH``, ``PDD_STATE_PATH`` и ``PDD_LOGS_DIR``
    нужны офлайн-тестам и CI, чтобы не читать пользовательский
    ``runtime/config.yaml`` и не писать lock/state в рабочий каталог. Если
    переменная не задана, используется прежнее производственное значение.
    Отсутствие конфигурации по-прежнему является явной ошибкой, а не
    поводом подставить пример.
    """
    value = os.environ.get(name)
    return Path(value) if value else default


CONFIG_PATH = _path_from_env("PDD_CONFIG_PATH", ROOT / "config.yaml")
STATE_PATH = _path_from_env("PDD_STATE_PATH", ROOT / "state.json")
LOGS = _path_from_env("PDD_LOGS_DIR", ROOT / "logs")

STATE_LOCK_TIMEOUT_SECONDS = 30.0
DEVICE_LOCK_TIMEOUT_SECONDS = 30.0
DRAFT_LOCK_TIMEOUT_SECONDS = 30.0
CONFIG_LOCK_TIMEOUT_SECONDS = 30.0

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


def load_config() -> dict:
    with _config_lock():
        return _read_config_unlocked()


def save_config(cfg: dict) -> None:
    """Совместимость: атомарная запись всего объекта под блокировкой конфигурации.

    Для чтения-изменения-записи использовать ``update_config``, иначе
    параллельные процессы могут потерять чужие обновления.
    """
    with _config_lock():
        _write_config_unlocked(cfg)


def update_config(callback):
    """Прочитать, изменить и атомарно записать конфигурацию одной транзакцией."""
    with _config_lock():
        cfg = _read_config_unlocked()
        result = callback(cfg)
        _write_config_unlocked(cfg)
        return result


class StateError(RuntimeError):
    """Операция остановлена: состояние недоступно или повреждено."""


class DeviceLockError(StateError):
    """Не удалось получить межпроцессную блокировку конкретного устройства."""


class DraftLockError(StateError):
    """Не удалось получить межпроцессную блокировку черновика/чата."""


def _ensure_lock_byte(lock):
    lock.seek(0, os.SEEK_END)
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)


def _acquire_os_lock(lock, timeout, error_factory):
    """Неблокирующая попытка с дедлайном: без бесконечного ожидания."""
    deadline = time.monotonic() + max(0.0, timeout)
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise error_factory() from exc
                time.sleep(0.05)
    import fcntl
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                raise error_factory() from exc
            time.sleep(0.05)


def _release_os_lock(lock):
    if os.name == "nt":
        import msvcrt
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def _state_lock(timeout=None):
    """Общий для pdd.py и chat.py межпроцессный lock коротких транзакций.

    На POSIX используется flock с дедлайном, как и msvcrt-ветка на Windows:
    бесконечное ожидание недопустимо. Блокировка снимается ОС при завершении
    процесса.
    """
    if timeout is None:
        timeout = STATE_LOCK_TIMEOUT_SECONDS
    lock_path = STATE_PATH.with_name(STATE_PATH.name + ".lock")
    with open(lock_path, "a+b") as lock:
        _ensure_lock_byte(lock)
        _acquire_os_lock(
            lock, timeout,
            lambda: StateError("Не удалось заблокировать состояние; операция отменена (истекло ожидание)"))
        try:
            yield
        finally:
            _release_os_lock(lock)


def _resource_lock_path(prefix, key, error_cls, what, fallback, hint=""):
    """Безопасное имя lock-файла из внешнего ключа.

    Разделители путей и прочие внешние символы заменяются; хеш сохраняет
    различие между ключами, которые после санитизации совпали. Пустой ключ
    запрещён: общего lock «на всё» нет.
    """
    if not isinstance(key, str) or not key.strip():
        message = f"Неизвестен {what}; блокировка невозможна."
        if hint:
            message += " " + hint
        raise error_cls(message)
    key = key.strip()
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._-")[:64] or fallback
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return STATE_PATH.with_name(f"{prefix}-{token}-{digest}.lock")


def _device_lock_path(serial):
    return _resource_lock_path(
        "device", serial, DeviceLockError, "серийный номер устройства", "device",
        "Сначала выполните python pdd.py status или connect.")


def _draft_lock_path(key):
    return _resource_lock_path(
        "draft", key, DraftLockError, "черновик/чат", "draft",
        "Укажите конкретный черновик или чат.")


_RESOURCE_LOCKS = {}
_RESOURCE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _named_lock(prefix, key, timeout, error_cls, what, fallback, busy_message):
    """Межпроцессная блокировка именованного ресурса с дедлайном.

    Повторный вход из того же потока в том же процессе разрешён; другой поток
    или процесс ждёт до дедлайна. После аварийного завершения владельца ОС
    снимает блокировку.
    """
    path = _resource_lock_path(prefix, key, error_cls, what, fallback)
    registry_key = str(path)
    with _RESOURCE_LOCKS_GUARD:
        entry = _RESOURCE_LOCKS.get(registry_key)
        if entry is None:
            entry = {"rlock": threading.RLock(), "depth": 0, "file": None}
            _RESOURCE_LOCKS[registry_key] = entry
    rlock = entry["rlock"]
    if not rlock.acquire(timeout=max(0.0, timeout)):
        raise error_cls(busy_message(timeout))
    try:
        if entry["depth"] == 0:
            lock_file = open(path, "a+b")
            try:
                _ensure_lock_byte(lock_file)
                _acquire_os_lock(lock_file, timeout, lambda: error_cls(busy_message(timeout)))
            except BaseException:
                lock_file.close()
                raise
            entry["file"] = lock_file
        entry["depth"] += 1
        try:
            yield
        finally:
            entry["depth"] -= 1
            if entry["depth"] == 0:
                lock_file = entry.pop("file", None)
                if lock_file is not None:
                    try:
                        _release_os_lock(lock_file)
                    finally:
                        lock_file.close()
    finally:
        rlock.release()


@contextmanager
def device_lock(serial, timeout=None):
    """Межпроцессная блокировка одного устройства (по серийному номеру).

    Порядок: эта блокировка берётся до ``_state_lock``. Повторный вход из того
    же потока в том же процессе разрешён (реентерабельно) и не блокируется;
    другой поток того же процесса ждёт как отдельный владелец. После
    аварийного завершения процесса ОС снимает блокировку. Блокировка не
    управляет внешней программой, которая её игнорирует (прямой adb, другой
    инструмент).
    """
    if timeout is None:
        timeout = DEVICE_LOCK_TIMEOUT_SECONDS

    def busy(seconds):
        return DeviceLockError(
            f"Устройство занято другим процессом; не удалось получить блокировку за {seconds:g} с. "
            "Операция отменена, на устройство ничего не отправлено.")

    with _named_lock("device", serial, timeout, DeviceLockError,
                     "серийный номер устройства", "device", busy):
        yield


@contextmanager
def draft_lock(key, timeout=None):
    """Межпроцессная блокировка конкретного черновика/чата.

    Порядок: эта блокировка берётся до ``_state_lock`` (как и ``device_lock``),
    охватывая весь цикл работы с черновиком: чтение контекста, проверки,
    добавление вложений и текста, отправку и фиксацию состояния. По истечении
    ожидания — явная ошибка, а не молчаливое продолжение. После аварийного
    завершения процесса ОС снимает блокировку.
    """
    if timeout is None:
        timeout = DRAFT_LOCK_TIMEOUT_SECONDS

    def busy(seconds):
        return DraftLockError(
            f"Черновик занят другим процессом; не удалось получить блокировку за {seconds:g} с. "
            "Операция отменена, отправка не выполнялась.")

    with _named_lock("draft", key, timeout, DraftLockError,
                     "черновик/чат", "draft", busy):
        yield


@contextmanager
def _config_lock(timeout=None):
    """Межпроцессная блокировка конфигурации для атомарных транзакций.

    Независима от ``_state_lock``; порядок при возможном совмещении:
    конфигурация, затем состояние. Содержимое конфигурации не попадает в
    сообщения об ошибках.
    """
    if timeout is None:
        timeout = CONFIG_LOCK_TIMEOUT_SECONDS
    lock_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".lock")
    with open(lock_path, "a+b") as lock:
        _ensure_lock_byte(lock)
        _acquire_os_lock(
            lock, timeout,
            lambda: StateError("Не удалось заблокировать конфигурацию; операция отменена (истекло ожидание)"))
        try:
            yield
        finally:
            _release_os_lock(lock)


def _read_config_unlocked() -> dict:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise StateError("Файл конфигурации недоступен или повреждён; исходный файл не изменён") from exc
    if not isinstance(cfg, dict):
        raise StateError("Неожиданная структура конфигурации; исходный файл не изменён")
    return cfg


def _write_config_unlocked(cfg: dict) -> None:
    if not isinstance(cfg, dict):
        raise StateError("Конфигурация должна быть объектом; запись отменена")
    try:
        text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    except yaml.YAMLError as exc:
        raise StateError("Не удалось сериализовать конфигурацию; запись отменена") from exc
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_PATH.parent,
                                         prefix=".pdd-config-", suffix=".tmp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, CONFIG_PATH)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


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


def create_unique_file(directory, stem: str, suffix: str, text: str) -> Path:
    """Атомарно создать новый файл с читаемым уникальным именем.

    Имя: ``<stem>_<UTC timestamp>_<random>.suffix``. Файл создаётся через
    ``O_CREAT|O_EXCL``: параллельные процессы не перезаписывают друг друга,
    а удаление прежних файлов не приводит к переиспользованию имени. На
    POSIX новый файл получает режим 0600; на Windows действует ACL каталога.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        name = f"{stem}_{stamp}_{uuid.uuid4().hex[:8]}{suffix}"
        path = directory / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return path
    raise StateError("Не удалось атомарно создать уникальный файл журнала; запись отменена")


def out(obj) -> None:
    """Печать результата: dict -> JSON, остальное как есть.

    Функция ничего не редактирует. Вызывающий код обязан не передавать сюда
    содержимое задач, ответов, секреты и токены без явного opt-in.
    """
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    else:
        print(obj)
    sys.stdout.flush()
