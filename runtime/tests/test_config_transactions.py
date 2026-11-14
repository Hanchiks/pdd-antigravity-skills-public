"""Атомарные транзакции конфигурации: категория (а) для Linux, офлайн.

Содержимое тестовой конфигурации синтетическое. Реальный runtime/config.yaml
не читается и не изменяется.
"""
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import common


class ConfigTransactionTests(unittest.TestCase):
    def setUp(self):
        test_root = (Path(__file__).resolve().parent / "state-test-work").resolve()
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="config-", dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.config_path = self.work / "config.yaml"
        self.config_path.write_text(
            "counter_a: 0\ncounter_b: 0\nnested:\n  keep: synthetic-value\n", encoding="utf-8")
        patcher = patch.object(common, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_update_config_preserves_unrelated_fields(self):
        common.update_config(lambda cfg: cfg.__setitem__("counter_a", cfg["counter_a"] + 1))
        cfg = common.load_config()
        self.assertEqual(cfg["counter_a"], 1)
        self.assertEqual(cfg["counter_b"], 0)
        self.assertEqual(cfg["nested"]["keep"], "synthetic-value")

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        before = self.config_path.read_bytes()
        with patch.object(common.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                common.update_config(lambda cfg: cfg.__setitem__("counter_a", 9))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(list(self.work.glob(".pdd-config-*.tmp")), [])
        self.assertEqual(common.load_config()["counter_a"], 0)

    def test_failure_does_not_leak_config_contents_in_error(self):
        secret = "SYNTHETIC-SECRET-DO-NOT-LOG"
        self.config_path.write_text(f"private_token: {secret}\ncounter_a: 0\n", encoding="utf-8")
        before = self.config_path.read_bytes()
        with patch.object(common.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError) as err:
                common.update_config(lambda cfg: cfg.__setitem__("counter_a", 1))
        self.assertNotIn(secret, str(err.exception))
        self.assertNotIn(secret, repr(err.exception))
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_serialization_failure_does_not_leak_config_contents(self):
        secret = "SYNTHETIC-SECRET-IN-CONFIG"
        self.config_path.write_text(f"private_token: {secret}\n", encoding="utf-8")
        before = self.config_path.read_bytes()

        def broken_dump(*args, **kwargs):
            raise yaml.YAMLError(f"cannot dump {secret}")

        with patch.object(common.yaml, "safe_dump", side_effect=broken_dump):
            with self.assertRaises(common.StateError) as err:
                common.update_config(lambda cfg: cfg.__setitem__("counter_a", 1))
        self.assertNotIn(secret, str(err.exception))
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_concurrent_updates_do_not_lose_values(self):
        code = r'''
import sys
from pathlib import Path
import common
common.CONFIG_PATH = Path(sys.argv[1])
key = "counter_a" if sys.argv[2] == "a" else "counter_b"
def change(cfg):
    cfg[key] = int(cfg.get(key, 0)) + 1
for _ in range(25):
    common.update_config(change)
'''
        processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", code,
                                       str(self.config_path), which],
                                      cwd=str(common.ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for which in ("a", "a", "b", "b")]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (stdout + stderr).decode("utf-8", errors="replace"))
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["counter_a"], 50)
        self.assertEqual(cfg["counter_b"], 50)
        self.assertEqual(cfg["nested"]["keep"], "synthetic-value")

    def test_corrupt_config_is_not_replaced_with_defaults(self):
        original = b"counter_a: [broken\n"
        self.config_path.write_bytes(original)
        with self.assertRaises(common.StateError):
            common.load_config()
        with self.assertRaises(common.StateError):
            common.update_config(lambda cfg: cfg.__setitem__("counter_a", 1))
        self.assertEqual(self.config_path.read_bytes(), original)


def _hold_config(ready, release):
    import common
    with common._config_lock():
        ready.set()
        release.wait(10)


class ConfigLockTimeoutTests(unittest.TestCase):
    def setUp(self):
        test_root = (Path(__file__).resolve().parent / "state-test-work").resolve()
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="config-lock-", dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name).resolve() / "config.yaml"
        self.config_path.write_text("counter_a: 0\n", encoding="utf-8")
        patcher = patch.object(common, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_config_lock_times_out_on_posix(self):
        if not hasattr(os, "fork"):
            self.skipTest("требуется POSIX fork")
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_config, args=(ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        with self.assertRaises(common.StateError):
            with common._config_lock(timeout=0.3):
                self.fail("блокировка конфигурации не должна ждать бесконечно")
        release.set()
        holder.join(5)


if __name__ == "__main__":
    unittest.main()