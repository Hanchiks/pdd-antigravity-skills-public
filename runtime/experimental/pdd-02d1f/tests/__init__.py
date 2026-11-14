"""Тестовый пакет экспериментального кандидата.

Добавляет каталог ``runtime`` в конец ``sys.path``, чтобы был доступен общий
``common.py`` и тестовая подготовка, но не подменял ``pdd`` кандидата рабочей
версией: каталог кандидата остаётся первым в пути импорта.
"""
import sys
from pathlib import Path

_RUNTIME = Path(__file__).resolve().parents[3]
if str(_RUNTIME) not in sys.path:
    sys.path.append(str(_RUNTIME))

from test_env import prepare

prepare()