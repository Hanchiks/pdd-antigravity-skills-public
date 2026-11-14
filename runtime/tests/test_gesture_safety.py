import types
import unittest
from unittest.mock import patch

import pdd


SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}


class GestureSafetyTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("pace", None), ("out", None),
                            ("_close_live_player_after_failed_dump", False)):
            item = patch.object(pdd, name, return_value=value)
            item.start()
            self.addCleanup(item.stop)

    def test_swipe_blocks_short_slow_and_out_of_bounds(self):
        for coords, ms in (((300, 300, 301, 301), 400), ((300, 300, 400, 300), 2000),
                           ((300, 87, 300, 500), 400), ((-1, 300, 400, 300), 400),
                           ((300, 300, 720, 300), 400), ((300, 300, 500, 300), 0),
                           ((float('nan'), 300, 500, 300), 400)):
            with self.subTest(coords=coords, ms=ms), patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_swipe(types.SimpleNamespace(x1=coords[0], y1=coords[1], x2=coords[2], y2=coords[3], ms=ms))
                adb.assert_not_called()

    def test_swipe_and_scroll_require_focus_and_send_normal_gesture(self):
        with patch.object(pdd, "run_adb") as adb:
            pdd.cmd_swipe(types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=400, ms=400))
            adb.assert_called_once_with(["shell", "input", "swipe", 540, 1800, 540, 600, 400])
        for cmd, arg in ((pdd.cmd_swipe, types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=400, ms=400)),
                         (pdd.cmd_scroll, types.SimpleNamespace(direction="down", amount=0.6, ms=450))):
            with self.subTest(cmd=cmd.__name__), patch.object(pdd, "_require_pdd_focus", side_effect=pdd.AdbError("focus")), \
                 patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    cmd(arg)
                adb.assert_not_called()
        with patch.object(pdd, "run_adb") as adb:
            pdd.cmd_scroll(types.SimpleNamespace(direction="down", amount=0.6, ms=450))
            adb.assert_called_once()

    def test_key_rejects_numeric_enter_and_checks_focus(self):
        for key in ("66", "23", "KEYCODE_ENTER", "enter", "KEYCODE_DPAD_CENTER"):
            with self.subTest(key=key), patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_key(types.SimpleNamespace(key=key))
                adb.assert_not_called()
        with patch.object(pdd, "run_adb") as adb:
            pdd.cmd_key(types.SimpleNamespace(key="back"))
            adb.assert_called_once_with(["shell", "input", "keyevent", 4])
        with patch.object(pdd, "_require_pdd_focus", side_effect=pdd.AdbError("focus")), \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_key(types.SimpleNamespace(key="back"))
            adb.assert_not_called()

    def test_scroll_blocks_short_or_slow_gesture(self):
        for direction, amount, ms in (("down", 0.1, 2000), ("left", 0.1, 400)):
            with self.subTest(direction=direction), patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_scroll(types.SimpleNamespace(direction=direction, amount=amount, ms=ms))
                adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
