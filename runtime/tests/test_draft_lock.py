"""Межпроцессная блокировка черновика/чата: категория (а) для Linux/flock.

Модель: два синтетических процесса, без браузера, CDP и реальных чатов.
Освобождение блокировки ОС после аварийного завершения проверено только на
Linux (flock). Поведение msvcrt на Windows остаётся категорией (в).
"""
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import common


def _hold(key, ready, release):
    with common.draft_lock(key, timeout=5):
        ready.set()
        release.wait(10)


def _acquire_and_report(key, timeout, queue):
    try:
        with common.draft_lock(key, timeout=timeout):
            queue.put("acquired")
    except common.StateError as exc:
        queue.put(("timeout", str(exc)))


def _acquire_and_crash(key, ready):
    context = common.draft_lock(key, timeout=5)
    context.__enter__()
    ready.set()
    os._exit(9)


class DraftLockTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path(__file__).resolve().parent / "state-test-work").resolve()
        self.root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="draft-lock-", dir=self.root)
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
        holder = ctx.Process(target=_hold, args=("chat:gemini", ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        second = ctx.Process(target=_acquire_and_report, args=("chat:gemini", 0.3, queue))
        second.start()
        result = queue.get(timeout=5)
        second.join(5)
        release.set()
        holder.join(5)
        self.assertEqual(result[0], "timeout")
        self.assertIn("занят", result[1])
        self.assertEqual(second.exitcode, 0)
        self.assertEqual(holder.exitcode, 0)

    def test_waiter_acquires_after_owner_releases(self):
        ctx = self.ctx()
        ready, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_hold, args=("chat:gemini", ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        waiter = ctx.Process(target=_acquire_and_report, args=("chat:gemini", 5, queue))
        waiter.start()
        release.set()
        self.assertEqual(queue.get(timeout=10), "acquired")
        waiter.join(5)
        holder.join(5)
        self.assertEqual(waiter.exitcode, 0)

    def test_crashed_owner_lock_is_released_by_os(self):
        ctx = self.ctx()
        ready, queue = ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_acquire_and_crash, args=("chat:gemini", ready))
        holder.start()
        self.assertTrue(ready.wait(5))
        holder.join(5)
        self.assertEqual(holder.exitcode, 9)
        successor = ctx.Process(target=_acquire_and_report, args=("chat:gemini", 2, queue))
        successor.start()
        self.assertEqual(queue.get(timeout=5), "acquired")
        successor.join(5)
        self.assertEqual(successor.exitcode, 0)

    def test_different_drafts_are_independent(self):
        ctx = self.ctx()
        ready, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
        holder = ctx.Process(target=_hold, args=("chat:gemini:conv-a", ready, release))
        holder.start()
        self.assertTrue(ready.wait(5))
        other = ctx.Process(target=_acquire_and_report, args=("chat:gemini:conv-b", 1, queue))
        other.start()
        self.assertEqual(queue.get(timeout=5), "acquired")
        other.join(5)
        release.set()
        holder.join(5)

    def test_failure_inside_cycle_releases_lock(self):
        with self.assertRaises(RuntimeError):
            with common.draft_lock("chat:gemini", timeout=1):
                raise RuntimeError("simulated failure inside draft cycle")
        started = time.monotonic()
        with common.draft_lock("chat:gemini", timeout=1):
            pass
        self.assertLess(time.monotonic() - started, 1)

    def test_reentrant_acquisition_in_same_process_does_not_deadlock(self):
        with common.draft_lock("chat:gemini", timeout=0.5):
            with common.draft_lock("chat:gemini", timeout=0.5):
                pass
            with common.draft_lock("chat:gemini", timeout=0.5):
                pass

    def test_unknown_draft_is_rejected_instead_of_shared_lock(self):
        for key in (None, "", "   ", 123):
            with self.subTest(key=key):
                with self.assertRaises(common.StateError):
                    with common.draft_lock(key, timeout=0.1):
                        self.fail("неизвестный черновик не должен давать общий lock")

    def test_lock_filename_is_sanitized_and_distinguishes_drafts(self):
        hostile = common._draft_lock_path("../../etc/passwd:gemini")
        self.assertEqual(hostile.parent, self.state_path.parent)
        self.assertNotIn("/", hostile.name)
        self.assertNotIn("\\", hostile.name)
        self.assertNotIn("..", hostile.name)
        self.assertNotEqual(common._draft_lock_path("a/b"), common._draft_lock_path("a_b"))
        self.assertEqual(common._draft_lock_path("chat:gemini"), common._draft_lock_path("chat:gemini"))


if __name__ == "__main__":
    unittest.main()