import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import common
import pdd


class StateTransactionTests(unittest.TestCase):
    def setUp(self):
        test_root = (Path(__file__).resolve().parent / "state-test-work").resolve()
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="isolated-", dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.assertTrue(self.work.is_relative_to(test_root))
        self.state_path = self.work / "state.json"
        self.state_patch = patch.object(common, "STATE_PATH", self.state_path)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_transaction_returns_result_and_preserves_unrelated_fields(self):
        common.save_state({"shot": 1, "other": {"value": "kept"}})
        def change(state):
            state["shot"] += 1
            return state["shot"]
        self.assertEqual(common.update_state(change), 2)
        state = common.load_state()
        self.assertEqual(state["other"], {"value": "kept"})
        self.assertEqual(state["shot"], 2)

    def test_corrupt_state_is_never_replaced_with_defaults(self):
        original = b'{"shot": 10, broken'
        self.state_path.write_bytes(original)
        with self.assertRaises(common.StateError):
            common.load_state()
        with self.assertRaises(common.StateError):
            common.update_state(lambda state: state.update({"shot": 0}))
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_callback_failure_rolls_back(self):
        common.save_state({"shot": 7})
        before = self.state_path.read_bytes()
        def fail(state):
            state["shot"] = 999
            raise ValueError("stop")
        with self.assertRaises(ValueError):
            common.update_state(fail)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_failed_replace_preserves_original(self):
        common.save_state({"shot": 7})
        before = self.state_path.read_bytes()
        with patch.object(common.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                common.update_state(lambda state: state.update({"shot": 8}))
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(list(self.work.glob(".pdd-state-*.tmp")), [])

    def test_pdd_cli_reports_state_error_without_traceback(self):
        error = common.StateError("Файл состояния повреждён; исходный файл не изменён")
        with patch.object(pdd, "cmd_status", side_effect=error), \
             patch.object(pdd, "out") as output, patch.object(sys, "argv", ["pdd.py", "status"]):
            with self.assertRaises(SystemExit) as stopped:
                pdd.main()
        self.assertEqual(stopped.exception.code, 2)
        output.assert_called_once_with({"ok": False, "error": str(error)})

    def test_concurrent_processes_do_not_lose_unrelated_updates(self):
        common.save_state({"shot": 0, "chats": {"gemini": {"messages": 0}, "chatgpt": {}}})
        code = r'''
import sys
from pathlib import Path
import common
common.STATE_PATH = Path(sys.argv[1])
which = sys.argv[2]
def change(state):
    if which == "shot":
        state["shot"] += 1
    else:
        state["chats"]["gemini"]["messages"] += 1
for _ in range(25):
    common.update_state(change)
'''
        processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(self.state_path), which],
                                      cwd=str(common.ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for which in ("shot", "shot", "chat", "chat")]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (stdout + stderr).decode("utf-8", errors="replace"))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["shot"], 50)
        self.assertEqual(state["chats"]["gemini"]["messages"], 50)


if __name__ == "__main__":
    unittest.main()
