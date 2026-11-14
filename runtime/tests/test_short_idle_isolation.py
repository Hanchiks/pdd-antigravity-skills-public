"""Cross-boundary tests for the read-only short-idle experiment."""
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pdd
import ui_short_idle


PACKAGE = "com.xunmeng.pinduoduo"
FOCUS = ("abc", "0", PACKAGE + "/.Main")


class ShortIdleIsolationTests(unittest.TestCase):
    def test_view_clears_stale_indices_and_warns_about_other_windows(self):
        # A diagnostic root alone cannot prove that a system overlay is absent.
        tree = ET.fromstring('<hierarchy><node package="com.xunmeng.pinduoduo" '
                             'text="商品" bounds="[0,100][400,200]" />'
                             '<node package="android" text="OVERLAY" bounds="[0,100][400,200]" />'
                             '</hierarchy>')
        state = {"ui": [{"label": "收藏"}], "ui_signature": "old", "ui_quality": "full", "other": 7}
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "_diagnostic_focus", return_value=FOCUS), \
             patch.object(pdd, "update_state", side_effect=lambda cb: cb(state)), \
             patch.object(ui_short_idle, "read_short_idle", return_value=tree), \
             patch.object(pdd, "tap_real") as tap, patch.object(pdd, "out") as output:
            pdd.cmd_ui_short_idle(types.SimpleNamespace(find=None))
            self.assertNotIn("ui", state)
            self.assertNotIn("ui_quality", state)
            self.assertEqual(state["other"], 7)
            self.assertIn("системные и другие окна не показаны", output.call_args.args[0])
            self.assertNotIn("OVERLAY", output.call_args.args[0])
            tap.assert_not_called()

    def test_missing_app_and_changed_window_fail_without_output(self):
        no_app = ET.fromstring('<hierarchy><node package="android" text="Пример" /></hierarchy>')
        valid = ET.fromstring('<hierarchy><node package="com.xunmeng.pinduoduo" text="商品" /></hierarchy>')
        cases = ((no_app, [FOCUS, FOCUS]), (valid, [FOCUS, ("def", "0", PACKAGE + "/.Main")]))
        for tree, focuses in cases:
            state = {"ui": [{"label": "старый"}], "ui_quality": "full"}
            with self.subTest(tree=tree), patch.object(pdd, "ensure_device"), \
                 patch.object(pdd, "update_state", side_effect=lambda cb: cb(state)), \
                 patch.object(pdd, "_diagnostic_focus", side_effect=focuses), \
                 patch.object(ui_short_idle, "read_short_idle", return_value=tree), \
                 patch.object(pdd, "out") as output, patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_ui_short_idle(types.SimpleNamespace(find=None))
                self.assertNotIn("ui", state)
                output.assert_not_called()
                tap.assert_not_called()

    def test_helper_failure_clears_indices_and_never_taps(self):
        state = {"ui": [{"label": "старый"}], "ui_quality": "full"}
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "update_state", side_effect=lambda cb: cb(state)), \
             patch.object(pdd, "_diagnostic_focus", return_value=FOCUS), \
             patch.object(ui_short_idle, "read_short_idle", side_effect=pdd.AdbError("таймаут")), \
             patch.object(pdd, "out") as output, patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_ui_short_idle(types.SimpleNamespace(find=None))
            self.assertNotIn("ui", state)
            output.assert_not_called()
            tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
