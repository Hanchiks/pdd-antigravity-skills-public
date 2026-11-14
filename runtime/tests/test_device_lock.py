"""Межпроцессная блокировка устройства: категория (а) для Linux/flock.

Модель: два синтетических процесса, без ADB и без телефона. Освобождение
блокировки ОС после аварийного завершения проверено только на Linux (flock).
Поведение msvcrt на Windows остаётся категорией (в) и здесь не проверяется.
"""
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import common


def _hold(serial, ready, release):
    with common.device_lock(serial, timeout=5):
        ready.set()
        release.wait(10)


def _acquire_and_report(serial, timeout, queue):
    try:
        with common.device_lock(serial, timeout=timeout):
            queue.put("acquired")
    except common.StateError as exc:
        queue.put(("timeout", str(exc)))


def _acquire_and_crash(serial, ready):
    context = common.device_lock(serial, timeout=5)
    context.__enter__()
    ready.set()
    os._exit(7)


class DeviceLockTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path(__file__).resolve().parent / "state-test-work").resolve()
        self.root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="device-lock-", dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name).resolve() / "state.json"
        patcher = patch.object(common, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self):
        if not hasattr(os, "fork"):
            self.skipTest("требуется POSIX fork; Windows-ветка msvcrt не проверяется")
        return multiprocessing.get_context("fork")

    def test_second_process_times_out_while_first_holds(self):
        ctx = self.ctx()
        ready, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_hold, args=("SERIAL-A", ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        second = ctx.Process(target=_acquire_and_report, args=("SERIAL-A", 0.3, queue))
        second.start()
        result = queue.get(timeout=5)
        second.join(5)
        release.set()
        holder.join(5)
        self.assertEqual(result[0], "timeout")
        self.assertIn("занято", result[1])
        self.assertEqual(second.exitcode, 0)
        self.assertEqual(holder.exitcode, 0)

    def test_waiter_acquires_after_owner_releases(self):
        ctx = self.ctx()
        ready, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_hold, args=("SERIAL-A", ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        waiter = ctx.Process(target=_acquire_and_report, args=("SERIAL-A", 5, queue))
        waiter.start()
        release.set()
        self.assertEqual(queue.get(timeout=10), "acquired")
        waiter.join(5)
        holder.join(5)
        self.assertEqual(waiter.exitcode, 0)

    def test_crashed_owner_lock_is_released_by_os(self):
        ctx = self.ctx()
        ready, queue = ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_acquire_and_crash, args=("SERIAL-A", ready))
        holder.start()
        self.assertTrue(ready.wait(5))
        holder.join(5)
        self.assertEqual(holder.exitcode, 7)
        successor = ctx.Process(target=_acquire_and_report, args=("SERIAL-A", 2, queue))
        successor.start()
        self.assertEqual(queue.get(timeout=5), "acquired")
        successor.join(5)
        self.assertEqual(successor.exitcode, 0)

    def test_different_serials_are_independent(self):
        ctx = self.ctx()
        ready, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_hold, args=("SERIAL-A", ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        other = ctx.Process(target=_acquire_and_report, args=("SERIAL-B", 1, queue))
        other.start()
        self.assertEqual(queue.get(timeout=5), "acquired")
        other.join(5)
        release.set()
        holder.join(5)

    def test_reentrant_acquisition_in_same_process_does_not_deadlock(self):
        with common.device_lock("SERIAL-A", timeout=0.5):
            with common.device_lock("SERIAL-A", timeout=0.5):
                pass
            with common.device_lock("SERIAL-A", timeout=0.5):
                pass

    def test_unknown_serial_is_rejected_instead_of_shared_lock(self):
        for serial in (None, "", "   ", 123):
            with self.subTest(serial=serial):
                with self.assertRaises(common.StateError):
                    with common.device_lock(serial, timeout=0.1):
                        self.fail("неизвестный серийный номер не должен давать общий lock")

    def test_lock_filename_is_sanitized_and_distinguishes_serials(self):
        hostile = common._device_lock_path("../../etc/passwd:5555")
        self.assertEqual(hostile.parent, self.state_path.parent)
        self.assertNotIn("/", hostile.name)
        self.assertNotIn("\\", hostile.name)
        self.assertNotIn("..", hostile.name)
        self.assertNotEqual(common._device_lock_path("a/b"), common._device_lock_path("a_b"))
        self.assertEqual(common._device_lock_path("SERIAL-A"), common._device_lock_path("SERIAL-A"))


class StateLockTimeoutTests(unittest.TestCase):
    """POSIX _state_lock ранее ждал flock бесконечно; проверяем дедлайн."""

    def setUp(self):
        self.root = (Path(__file__).resolve().parent / "state-test-work").resolve()
        self.root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="state-lock-", dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name).resolve() / "state.json"
        patcher = patch.object(common, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_state_lock_times_out_on_posix(self):
        if not hasattr(os, "fork"):
            self.skipTest("требуется POSIX fork")
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_state, args=(ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        started = time.monotonic()
        with self.assertRaises(common.StateError):
            with common._state_lock(timeout=0.3):
                self.fail("блокировка состояния не должна ждать бесконечно")
        self.assertLess(time.monotonic() - started, 5)
        release.set()
        holder.join(5)


def _hold_state(ready, release):
    with common._state_lock():
        ready.set()
        release.wait(10)


if __name__ == "__main__":
    unittest.main()