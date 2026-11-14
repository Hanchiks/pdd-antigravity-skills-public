"""Категория (б): офлайн-заглушки, а не устройство.

ADB/DOM/UI подменены моками; зелёный прогон не подтверждает поведение на
реальном телефоне или в браузере.
"""
import types
import unittest
from unittest.mock import patch

import pdd
from test_ui_safety import SC, element


class SafetyDiagnosticTests(unittest.TestCase):
    def setUp(self):
        for name in ("ensure_device", "_require_pdd_focus", "out"):
            patcher = patch.object(pdd, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("screen_info", SC), ("SERIAL", "test-device")):
            patcher = (patch.object(pdd, name, return_value=value) if name == "screen_info"
                       else patch.object(pdd, name, value))
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self, els):
        return {"ui": els, "ui_signature": pdd.ui_signature(els), "ui_serial": "test-device",
                "ui_captured_at": pdd.time.time(), "ui_quality": "full", "ui_page_anchors": pdd._ui_page_anchors(els)}

    def test_tapel_distinguishes_missing_moved_and_ambiguous_without_leaking_label(self):
        secret = "личные-данные"
        original = [element(secret, 300, 300)]
        cases = [([element("другой", 300, 300)], "отсутствует или изменился"),
                 ([element(secret, 320, 300)], "переместился"),
                 ([element(secret, 300, 300), element(secret, 302, 300)], "несколько подходящих")]
        for current, reason in cases:
            with self.subTest(reason=reason), patch.object(pdd, "load_state", return_value=self.state(original)), \
                 patch.object(pdd, "read_ui_elements", return_value=current), patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError) as err:
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
                self.assertIn(reason, str(err.exception))
                self.assertNotIn(secret, str(err.exception))
                tap.assert_not_called()

    def test_forbidden_diagnostic_does_not_repeat_sensitive_label(self):
        sensitive = "частный-текст"
        e = element(sensitive + "立即支付", 300, 300)
        with self.assertRaises(pdd.AdbError) as err:
            pdd._check_target(300, 300, [e])
        self.assertNotIn(sensitive, str(err.exception))
        with patch.object(pdd, "load_state", return_value=self.state([e])):
            with self.assertRaises(pdd.AdbError) as err:
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            self.assertNotIn(sensitive, str(err.exception))

    def test_dump_is_passive_and_points_to_explicit_close_command(self):
        # Старый тест ожидал автозакрытие плеера из dump_ui_xml. Это было
        # неверное ожидание: чтение не должно нажимать на экран. Теперь ошибка
        # честно сообщает об отсутствии действий и ссылается на close-player.
        output = "ERROR: could not get idle state. частные-данные"
        with patch.object(pdd, "_dump_ui_attempts", return_value=(None, output, True)), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError) as err:
                pdd.dump_ui_xml()
            tap.assert_not_called()
            adb.assert_not_called()
        text = str(err.exception)
        self.assertIn("автоматических действий", text)
        self.assertIn("close-player", text)
        self.assertNotIn("Выполнена попытка", text)
        self.assertIn("could not get idle state", text)
        self.assertNotIn("частные-данные", text)


if __name__ == "__main__":
    unittest.main()
