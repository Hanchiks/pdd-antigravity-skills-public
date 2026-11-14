"""Категория (б): офлайн-заглушки, а не устройство.

Тесты подменяют ADB/вывод и проверяют только локальные ветки кода. Зелёный
прогон не снимает запрет на реальные жесты и не подтверждает безопасность
на телефоне; это остаётся для категории (в).
"""
import types
import unittest
from unittest.mock import patch

import pdd


SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}


class GestureSafetyTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("pace", None), ("out", None)):
            item = patch.object(pdd, name, return_value=value)
            item.start()
            self.addCleanup(item.stop)
        serial = patch.object(pdd, "SERIAL", "test-device")
        serial.start()
        self.addCleanup(serial.stop)

    def assert_gesture_blocked(self, fn, arg):
        with patch.object(pdd, "ensure_device") as device, \
             patch.object(pdd, "screen_info") as screen, \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError) as err:
                fn(arg)
            device.assert_not_called()
            screen.assert_not_called()
            adb.assert_not_called()
            text = str(err.exception)
            self.assertIn("заблокирован", text)
            self.assertIn("не выполнялась", text)
            self.assertIn("геометрии", text)
            self.assertNotIn("выполнена", text.lower())
        return text

    def test_scroll_is_blocked_before_any_device_access(self):
        for direction in ("down", "up", "left", "right"):
            with self.subTest(direction=direction):
                self.assert_gesture_blocked(
                    pdd.cmd_scroll,
                    types.SimpleNamespace(direction=direction, amount=0.6, ms=450))

    def test_swipe_is_blocked_before_any_device_access(self):
        for coords, ms in (((360, 1200, 360, 400), 400),
                           ((300, 300, 300, 301), 400),
                           ((300, 300, 400, 300), 2000),
                           ((-1, 300, 400, 300), 400),
                           ((float('nan'), 300, 500, 300), 400)):
            with self.subTest(coords=coords, ms=ms):
                self.assert_gesture_blocked(
                    pdd.cmd_swipe,
                    types.SimpleNamespace(x1=coords[0], y1=coords[1],
                                          x2=coords[2], y2=coords[3], ms=ms))

    def test_block_message_explains_reason_and_does_not_claim_execution(self):
        text = self.assert_gesture_blocked(
            pdd.cmd_scroll, types.SimpleNamespace(direction="down", amount=0.6, ms=450))
        self.assertIn("до независимой приёмки", text)
        self.assertIn("guard'а жестов", text)
        self.assertIn("на устройство ничего не отправлено", text)

    def test_no_environment_or_cli_switch_bypasses_the_block(self):
        # Блокировка не читает env/config/CLI-флаги: даже «разрешающие»
        # переменные не должны ничего менять.
        with patch.dict("os.environ", {"PDD_ALLOW_GESTURES": "1", "PDD_SCROLL": "1"}):
            self.assert_gesture_blocked(
                pdd.cmd_swipe, types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=400, ms=400))

    def test_long_press_is_blocked_before_any_adb(self):
        # Долгое нажатие отправлялось через `input swipe`, поэтому это жест:
        # оно обязано перехватываться до run_adb, как scroll/swipe.
        with patch.object(pdd, "run_adb") as adb:
            with self.assertRaisesRegex(pdd.AdbError, "Длительное нажатие"):
                pdd.tap_real(300, 600, 500)
            adb.assert_not_called()

    def test_cmd_tap_long_press_refused_before_device_access(self):
        with patch.object(pdd, "ensure_device") as device, \
             patch.object(pdd, "_require_pdd_focus") as focus, \
             patch.object(pdd, "read_ui_elements") as read, \
             patch.object(pdd, "screen_info") as screen, \
             patch.object(pdd, "to_real") as to_real, \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaisesRegex(pdd.AdbError, "Длительное нажатие"):
                pdd.cmd_tap(types.SimpleNamespace(x=360, y=600, long=500))
            device.assert_not_called()
            focus.assert_not_called()
            read.assert_not_called()
            screen.assert_not_called()
            to_real.assert_not_called()
            tap.assert_not_called()
            adb.assert_not_called()

    def test_cmd_tapel_long_press_refused_before_device_access(self):
        with patch.object(pdd, "ensure_device") as device, \
             patch.object(pdd, "load_state") as load, \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaisesRegex(pdd.AdbError, "Длительное нажатие"):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=500))
            device.assert_not_called()
            load.assert_not_called()
            adb.assert_not_called()

    def test_zero_long_tap_still_sends_single_input_tap(self):
        with patch.object(pdd, "run_adb") as adb, patch.object(pdd, "pace"):
            pdd.tap_real(300, 600, 0)
            adb.assert_called_once_with(["shell", "input", "tap", 300, 600])

    def test_key_del_and_search_require_fresh_focused_field(self):
        for key in ("del", "search"):
            with self.subTest(key=key), \
                 patch.object(pdd, "_require_focused_editable",
                              side_effect=pdd.AdbError("поле не подтверждено")) as check, \
                 patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_key(types.SimpleNamespace(key=key))
                check.assert_called_once_with(require_search=(key == "search"))
                adb.assert_not_called()

    def test_key_refuses_if_focus_changes_before_send(self):
        with patch.object(pdd, "_require_pdd_focus",
                          side_effect=[None, pdd.AdbError("окно изменилось")]), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_key(types.SimpleNamespace(key="back"))
            adb.assert_not_called()

    def test_key_refuses_without_fresh_full_tree(self):
        for key in ("back", "home", "recent"):
            with self.subTest(key=key), \
                 patch.object(pdd, "read_ui_elements",
                              side_effect=pdd.AdbError("дерево недоступно")) as read, \
                 patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_key(types.SimpleNamespace(key=key))
                read.assert_called_once_with(include_all=True, require_full=True)
                adb.assert_not_called()

    def test_key_rejects_numeric_enter_and_checks_focus(self):
        for key in ("66", "23", "KEYCODE_ENTER", "enter", "KEYCODE_DPAD_CENTER"):
            with self.subTest(key=key), patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_key(types.SimpleNamespace(key=key))
                adb.assert_not_called()
        with patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd, "run_adb") as adb:
            pdd.cmd_key(types.SimpleNamespace(key="back"))
            adb.assert_called_once_with(["shell", "input", "keyevent", 4])
        with patch.object(pdd, "_require_pdd_focus", side_effect=pdd.AdbError("focus")), \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_key(types.SimpleNamespace(key="back"))
            adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()