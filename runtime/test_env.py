"""Подготовка изолированной среды для офлайн-тестов.

Модуль импортируется только тестовыми пакетами. Он создаёт временный
синтетический каталог внутри репозитория и выставляет переменные
``PDD_CONFIG_PATH``, ``PDD_STATE_PATH`` и ``PDD_LOGS_DIR`` так, чтобы
``common.py`` не читал пользовательский ``runtime/config.yaml`` и не писал
``state.json``/lock-файлы в рабочий каталог. Если переменные не выставлены,
производственное поведение не меняется: отсутствие ``config.yaml`` остаётся
явной ошибкой.

Шаблон конфигурации берётся из отслеживаемого ``runtime/config.example.yaml``
(это не пользовательская конфигурация и не содержит секретов); серийный номер
принудительно очищается.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import yaml

RUNTIME_ROOT = Path(__file__).resolve().parent
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_WORK_ROOT = RUNTIME_ROOT / ".test-runtime"
READY_ENV = "PDD_TEST_ENV_READY"
WORK_ROOT_ENV = "PDD_TEST_WORK_ROOT"


def synthetic_config() -> dict:
    """Синтетическая конфигурация на основе отслеживаемого примера."""
    with (RUNTIME_ROOT / "config.example.yaml").open(encoding="utf-8") as stream:
        cfg = yaml.safe_load(stream)
    if not isinstance(cfg, dict):
        raise RuntimeError("config.example.yaml должен содержать объект")
    phone = cfg.setdefault("phone", {})
    if not isinstance(phone, dict):
        raise RuntimeError("phone в config.example.yaml должен быть объектом")
    phone["serial"] = ""
    cfg.pop("profile_path", None)
    return cfg


def prepare() -> Path:
    """Создать временную среду один раз на процесс и вернуть её каталог."""
    if os.environ.get(READY_ENV) == "1":
        return Path(os.environ["PDD_CONFIG_PATH"]).parent
    base = Path(os.environ.get(WORK_ROOT_ENV) or DEFAULT_WORK_ROOT)
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"run-{os.getpid()}-", dir=base))
    config = work / "config.yaml"
    config.write_text(
        yaml.safe_dump(synthetic_config(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    state = work / "state.json"
    logs = work / "logs"
    tmp = work / "tmp"
    tmp.mkdir()
    os.environ["PDD_CONFIG_PATH"] = str(config)
    os.environ["PDD_STATE_PATH"] = str(state)
    os.environ["PDD_LOGS_DIR"] = str(logs)
    os.environ["TMPDIR"] = str(tmp)
    os.environ[READY_ENV] = "1"
    tempfile.tempdir = str(tmp)
    _owner_pid = os.getpid()

    def _cleanup(pid=_owner_pid, path=work):
        # Forked children inherit atexit handlers; only the owner may remove
        # the work directory, otherwise a child exit could delete it mid-run.
        if os.getpid() == pid:
            shutil.rmtree(path, ignore_errors=True)

    atexit.register(_cleanup)
    return work