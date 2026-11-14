import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pdd
from test_ui_safety import element


class UiNoLogTests(unittest.TestCase):
    def test_no_log_preserves_old_log_but_still_saves_state(self):
        with tempfile.TemporaryDirectory() as folder:
            latest = Path(folder) / "latest.txt"
            latest.write_text("previous log", encoding="utf-8")
            state = {}
            with patch.object(pdd, "ensure_device"), \
                 patch.object(pdd, "SERIAL", "test-device"), \
                 patch.object(pdd, "read_ui_elements", return_value=pdd.UiElements([element("收藏", 300, 300)], compressed=False)), \
                 patch.object(pdd, "update_state", side_effect=lambda callback: callback(state)), \
                 patch.object(pdd, "log_dir", return_value=Path(folder)) as log_dir, \
                 patch.object(pdd, "out") as output:
                pdd.cmd_ui(types.SimpleNamespace(all=False, find=None, no_log=True))
            self.assertEqual(latest.read_text(encoding="utf-8"), "previous log")
            log_dir.assert_not_called()
            self.assertEqual(state["ui"][0]["label"], "收藏")
            self.assertEqual(state["ui_serial"], "test-device")
            self.assertIn("состояние для tapel сохраняется", output.call_args.args[0])
            self.assertIn("временный дамп создаётся на телефоне", output.call_args.args[0])

    def test_default_ui_still_writes_log(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "read_ui_elements", return_value=[element("收藏", 300, 300)]), \
             patch.object(pdd, "update_state", side_effect=lambda callback: callback({})), \
             patch.object(pdd, "log_dir", return_value=Path(folder)), \
             patch.object(pdd, "out"):
            pdd.cmd_ui(types.SimpleNamespace(all=False, find=None))
            self.assertIn("收藏", (Path(folder) / "latest.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
