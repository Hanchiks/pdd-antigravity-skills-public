"""Тестовый пакет runtime: изолированная синтетическая среда до импорта pdd."""
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.append(str(_TESTS))

from test_env import prepare

prepare()