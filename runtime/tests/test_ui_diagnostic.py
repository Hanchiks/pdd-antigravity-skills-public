import types
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pdd
import ui_diagnostic as diag


HELPER = Path(pdd.ROOT) / "tools" / "no-idle-dump.jar"
XML = ('<hierarchy><node package="com.xunmeng.pinduoduo" text="收藏" '
       'resource-id="com.xunmeng.pinduoduo:id/item" class="Button" '
       'bounds="[0,150][200,250]" /></hierarchy>')


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        # Никогда не изменяем пользовательский state.json из тестов.
        self.state = {"ui": [{"label": "старый"}], "ui_signature": "old", "ui_serial": "device",
                      "ui_captured_at": 1, "ui_page_anchors": [], "ui_quality": "full", "other": 7}
        patcher = patch.object(pdd, "update_state", side_effect=lambda cb: cb(self.state))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_experimental_view_does_not_save_tap_state(self):
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "_diagnostic_focus", return_value=("abc", "0", "com.xunmeng.pinduoduo/.Main")), \
             patch.object(diag, "read_no_idle", return_value=ET.fromstring(XML)), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as output:
            pdd.cmd_ui_diagnostic(types.SimpleNamespace(find=None, all_packages=False))
            self.assertEqual(self.state, {"other": 7})
            tap.assert_not_called()
            self.assertIn("Нажатия по нему запрещены", output.call_args.args[0])
            self.assertIn("попытка 1 из 3", output.call_args.args[0])
            self.assertIn("id=com.xunmeng.pinduoduo:id/item", output.call_args.args[0])

    def test_missing_pdd_is_retried_but_only_as_read_only_diagnostic(self):
        other = ET.fromstring('<hierarchy><node package="android" /></hierarchy>')
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_diagnostic_focus", return_value=("abc", "0", "com.xunmeng.pinduoduo/.Main")), \
             patch.object(diag, "read_no_idle", side_effect=[other, other, ET.fromstring(XML)]) as read, \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as output:
            pdd.cmd_ui_diagnostic(types.SimpleNamespace(find=None, all_packages=False))
            self.assertEqual(read.call_count, 3)
            self.assertIn("попытка 3 из 3", output.call_args.args[0])
            self.assertEqual(self.state, {"other": 7})
            tap.assert_not_called()

    def test_changed_focus_or_exhausted_attempts_fail_closed(self):
        first = ("abc", "0", "com.xunmeng.pinduoduo/.Main")
        other_focus = ("def", "0", "com.xunmeng.pinduoduo/.Main")
        other = ET.fromstring('<hierarchy><node package="android" /></hierarchy>')
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_diagnostic_focus", side_effect=[first, first, other_focus]), \
             patch.object(diag, "read_no_idle", return_value=ET.fromstring(XML)), \
             patch.object(pdd, "out") as output:
            with self.assertRaisesRegex(pdd.AdbError, "Окно изменилось"):
                pdd.cmd_ui_diagnostic(types.SimpleNamespace(find=None, all_packages=False))
            output.assert_not_called()
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "_diagnostic_focus", return_value=first), \
             patch.object(diag, "read_no_idle", return_value=other) as read, patch.object(pdd, "out") as output:
            with self.assertRaisesRegex(pdd.AdbError, "после 3 попыток"):
                pdd.cmd_ui_diagnostic(types.SimpleNamespace(find=None, all_packages=False))
            self.assertEqual(read.call_count, 3)
            output.assert_not_called()

    def test_invalid_snapshot_does_not_retry(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_diagnostic_focus", return_value=("abc", "0", "com.xunmeng.pinduoduo/.Main")), \
             patch.object(diag, "read_no_idle", side_effect=pdd.AdbError("bad checksum")) as read:
            with self.assertRaisesRegex(pdd.AdbError, "bad checksum"):
                pdd.cmd_ui_diagnostic(types.SimpleNamespace(find=None, all_packages=False))
            read.assert_called_once()
            self.assertEqual(self.state, {"other": 7})

    def test_diagnostic_focus_must_be_one_concrete_pdd_window(self):
        good = "mCurrentFocus=Window{abc u0 com.xunmeng.pinduoduo/.MainActivity}\n"
        with patch.object(pdd, "run_adb", return_value=good):
            self.assertEqual(pdd._diagnostic_focus(), ("abc", "0", "com.xunmeng.pinduoduo/.MainActivity"))
        for bad in ("", good + "mCurrentFocus=null\n", "mCurrentFocus=Window{abc u0 android/.Main}\n"):
            with self.subTest(focus=bad), patch.object(pdd, "run_adb", return_value=bad), self.assertRaises(pdd.AdbError):
                pdd._diagnostic_focus()

    def test_helper_integrity_and_cleanup_even_when_tree_invalid(self):
        calls = []
        def adb(args, **kwargs):
            calls.append(args)
            if args[:2] == ["shell", "sha256sum"]:
                return diag.HELPER_SHA256 + "  " + args[-1]
            if args[0] == "exec-out":
                return "invalid XML"
            return ""
        with self.assertRaises(pdd.AdbError):
            diag.read_no_idle(adb, HELPER, pdd.AdbError)
        push, cleanup = calls[0], calls[-1]
        self.assertEqual(push[0], "push")
        self.assertEqual(cleanup[:3], ["shell", "rm", "-f"])
        self.assertEqual(cleanup[-1], push[-1])

    def test_bad_remote_checksum_never_executes_helper(self):
        calls = []
        def adb(args, **kwargs):
            calls.append(args)
            return "0" * 64 if args[:2] == ["shell", "sha256sum"] else ""
        with self.assertRaises(pdd.AdbError):
            diag.read_no_idle(adb, HELPER, pdd.AdbError)
        self.assertFalse(any(c[0] == "exec-out" for c in calls))
        self.assertEqual(calls[-1][:3], ["shell", "rm", "-f"])

    def test_single_leaf_is_not_mistaken_for_empty_tree(self):
        def adb(args, **kwargs):
            if args[:2] == ["shell", "sha256sum"]:
                return diag.HELPER_SHA256
            return XML if args[0] == "exec-out" else ""
        result = diag.read_no_idle(adb, HELPER, pdd.AdbError)
        self.assertIsNotNone(result.find("node"))

    def test_locally_modified_helper_is_rejected_without_device_access(self):
        with patch.object(diag, "HELPER_SHA256", "0" * 64):
            with self.assertRaises(pdd.AdbError):
                diag.read_no_idle(lambda *args, **kw: self.fail("device accessed"), HELPER, pdd.AdbError)


if __name__ == "__main__":
    unittest.main()
