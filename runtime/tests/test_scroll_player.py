import types
import unittest
from unittest.mock import patch

import pdd

SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}


class GesturePlayerTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("pace", None)):
            patcher = patch.object(pdd, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_scroll_and_swipe_check_player_after_gesture_once(self):
        actions = ((pdd.cmd_scroll, types.SimpleNamespace(direction="down", amount=0.6, ms=450)),
                   (pdd.cmd_swipe, types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=400, ms=400)))
        for fn, args in actions:
            with self.subTest(action=fn.__name__), \
                 patch.object(pdd, "run_adb") as adb, \
                 patch.object(pdd, "_close_live_player_after_failed_dump", return_value=True) as close, \
                 patch.object(pdd, "out") as out:
                fn(args)
                adb.assert_called_once()
                close.assert_called_once()
                self.assertTrue(out.call_args.args[0]["player_close_attempted"])

    def test_unverified_player_does_not_cancel_gesture_or_tap(self):
        args = types.SimpleNamespace(direction="down", amount=0.6, ms=450)
        for failure in (pdd.AdbError("capture failed"), OSError("capture failed")):
            with self.subTest(failure=failure), patch.object(pdd, "run_adb") as adb, \
                 patch.object(pdd, "capture", side_effect=failure), \
                 patch.object(pdd, "tap_real") as tap, patch.object(pdd, "out") as out:
                pdd.cmd_scroll(args)
                adb.assert_called_once()
                tap.assert_not_called()
                self.assertFalse(out.call_args.args[0]["player_close_attempted"])

    def test_invalid_gesture_never_checks_or_closes_player(self):
        args = types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=1199, ms=400)
        with patch.object(pdd, "_close_live_player_after_failed_dump") as close, \
             patch.object(pdd, "run_adb") as adb, self.assertRaises(pdd.AdbError):
            pdd.cmd_swipe(args)
        adb.assert_not_called()
        close.assert_not_called()
