"""Категория (б): офлайн-заглушки, а не устройство.

ADB/DOM/UI подменены моками; зелёный прогон не подтверждает поведение на
реальном телефоне или в браузере.
"""
import types
import unittest
from unittest.mock import patch

import pdd


class UiIdleFallbackTests(unittest.TestCase):
    def setUp(self):
        self.state = {"ui": [{"label": "старый элемент"}], "ui_quality": "full",
                      "ui_signature": "old", "ui_serial": "device", "ui_captured_at": 1,
                      "ui_page_anchors": [], "other": "keep"}
        def update(fn):
            return fn(self.state)
        self.update = patch.object(pdd, "update_state", side_effect=update)
        self.update.start()
        self.addCleanup(self.update.stop)
        for name in ("ensure_device", "out"):
            p = patch.object(pdd, name)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("screen_info", {"real_w": 1080, "real_h": 2400, "view_w": 720,
                                             "view_h": 1600, "scale": 1.5}), ("SERIAL", "test-device")):
            p = patch.object(pdd, name, return_value=value) if name == "screen_info" else patch.object(pdd, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_idle_fallback_invalidates_indices_and_displays_only_diagnostic(self):
        args = types.SimpleNamespace(find="商品", all=False, no_log=True)
        with patch.object(pdd, "read_ui_elements", side_effect=pdd.UiIdleError("idle")), \
             patch.object(pdd, "cmd_ui_diagnostic") as diagnostic, \
             patch.object(pdd, "log_dir") as log:
            pdd.cmd_ui(args)
        self.assertEqual(self.state, {"other": "keep"})
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0].find, "商品")
        self.assertFalse(diagnostic.call_args.args[0].all_packages)
        log.assert_not_called()
        with patch.object(pdd, "load_state", return_value=self.state), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_not_called()

    def test_non_idle_failure_invalidates_without_fallback(self):
        args = types.SimpleNamespace(find=None, all=False, no_log=False)
        with patch.object(pdd, "read_ui_elements", side_effect=pdd.AdbError("bad XML")), \
             patch.object(pdd, "cmd_ui_diagnostic") as diagnostic:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_ui(args)
        self.assertEqual(self.state, {"other": "keep"})
        diagnostic.assert_not_called()

    def test_mixed_failure_is_not_misclassified_as_idle(self):
        for attempts in ([(None, "bad", False), (None, "idle", True)],
                         [(None, "idle", True), (None, "bad", False), (None, "idle", True)]):
            with self.subTest(attempts=attempts), \
                 patch.object(pdd, "_dump_ui_attempts", side_effect=attempts):
                with self.assertRaises(pdd.AdbError) as error:
                    pdd.dump_ui_xml()
                self.assertNotIsInstance(error.exception, pdd.UiIdleError)

    def test_all_idle_failure_has_specific_type(self):
        with patch.object(pdd, "_dump_ui_attempts", return_value=(None, "ERROR: could not get idle state.", True)):
            with self.assertRaises(pdd.UiIdleError):
                pdd.dump_ui_xml()

    def test_file_read_timeout_is_not_mistaken_for_idle(self):
        def adb(args, **kwargs):
            if args[:3] == ["shell", "uiautomator", "dump"]:
                return "UI hierarchy dumped to: /sdcard/example.xml"
            if args[:2] == ["exec-out", "cat"]:
                raise pdd.AdbError("Таймаут команды: cat")
            return ""
        with patch.object(pdd, "run_adb", side_effect=adb):
            xml, _, is_idle = pdd._dump_ui_attempts((False,))
        self.assertIsNone(xml)
        self.assertFalse(is_idle)
