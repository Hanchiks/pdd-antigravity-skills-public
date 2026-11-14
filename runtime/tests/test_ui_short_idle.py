import types
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import pdd
import ui_short_idle as short


XML = ('<hierarchy><node package="com.xunmeng.pinduoduo" text="收藏" '
       'bounds="[0,150][200,250]" /></hierarchy>')
FOCUS = ("abc", "0", "com.xunmeng.pinduoduo/.Main")


class ShortIdleTests(unittest.TestCase):
    def test_readonly_ui_invalidates_old_indices_and_does_not_tap(self):
        state = {"ui": ["old"], "ui_signature": "old", "ui_quality": "full"}
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "_diagnostic_focus", return_value=FOCUS), \
             patch.object(short, "read_short_idle", return_value=ET.fromstring(XML)), \
             patch.object(pdd, "update_state", side_effect=lambda f: f(state)), \
             patch.object(pdd, "tap_real") as tap, patch.object(pdd, "out") as output:
            pdd.cmd_ui_short_idle(types.SimpleNamespace(find=None))
        self.assertEqual(state, {})
        tap.assert_not_called()
        self.assertIn("Нажатия по нему запрещены", output.call_args.args[0])
        self.assertIn("системные и другие окна не показаны", output.call_args.args[0])

    def test_changed_focus_rejects_snapshot(self):
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "update_state"), \
             patch.object(pdd, "_diagnostic_focus", side_effect=[FOCUS, ("def", "0", FOCUS[2])]), \
             patch.object(short, "read_short_idle", return_value=ET.fromstring(XML)), \
             patch.object(pdd, "out") as output:
            with self.assertRaisesRegex(pdd.AdbError, "Окно изменилось"):
                pdd.cmd_ui_short_idle(types.SimpleNamespace(find=None))
            output.assert_not_called()

    def test_helper_integrity_and_cleanup(self):
        calls = []
        def adb(args, **kwargs):
            calls.append(args)
            if args[:2] == ["shell", "sha256sum"]:
                return short.HELPER_SHA256 + "  " + args[-1]
            if args[0] == "exec-out":
                return XML
            return ""
        helper = pdd.ROOT / "tools" / "short-idle-dump.jar"
        self.assertEqual(short.read_short_idle(adb, helper, pdd.AdbError).tag, "hierarchy")
        self.assertEqual(calls[-1][:3], ["shell", "rm", "-f"])
        self.assertEqual(calls[-1][-1], calls[0][-1])

    def test_bad_checksums_fail_before_execution(self):
        helper = pdd.ROOT / "tools" / "short-idle-dump.jar"
        with patch.object(short, "HELPER_SHA256", "0" * 64):
            with self.assertRaises(pdd.AdbError):
                short.read_short_idle(lambda *a, **kw: self.fail("device accessed"), helper, pdd.AdbError)
        calls = []
        def adb(args, **kwargs):
            calls.append(args)
            return "0" * 64 if args[:2] == ["shell", "sha256sum"] else ""
        with self.assertRaises(pdd.AdbError):
            short.read_short_idle(adb, helper, pdd.AdbError)
        self.assertFalse(any(c[0] == "exec-out" for c in calls))
        self.assertEqual(calls[-1][:3], ["shell", "rm", "-f"])

    def test_invalid_xml_still_removes_helper(self):
        calls = []
        def adb(args, **kwargs):
            calls.append(args)
            if args[:2] == ["shell", "sha256sum"]:
                return short.HELPER_SHA256
            if args[0] == "exec-out":
                return "<hierarchy>"
            return ""
        with self.assertRaises(pdd.AdbError):
            short.read_short_idle(adb, pdd.ROOT / "tools" / "short-idle-dump.jar", pdd.AdbError)
        self.assertEqual(calls[-1][:3], ["shell", "rm", "-f"])

    def test_helper_errors_do_not_expose_screen_data(self):
        secret = "PRIVATE_PARTIAL_SCREEN"
        calls = []
        def adb(args, **kwargs):
            calls.append(args)
            if args[:2] == ["shell", "sha256sum"]:
                return short.HELPER_SHA256
            if args[0] == "exec-out":
                raise pdd.AdbError(secret)
            return ""
        with self.assertRaises(pdd.AdbError) as caught:
            short.read_short_idle(adb, pdd.ROOT / "tools" / "short-idle-dump.jar", pdd.AdbError)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(calls[-1][:3], ["shell", "rm", "-f"])
