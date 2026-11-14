"""Player-close safety tests.

The previous version expected ``dump_ui_xml`` to tap the mini-player after an
idle-like dump failure. That expectation was wrong: a read path must be
passive. Automatic closing is now an explicit ``close-player`` command; the
tests below cover both the passive dump and that explicit command.

Category (b): offline stubs, not a device. Green tests do not prove
on-device safety and do not lift the real gesture ban.
"""
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import pdd


SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}


def frame(video_colour=(50, 80, 100), *, cross=True, banner=True):
    img = Image.new("RGB", (720, 1600), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((551, 345, 698, 602), fill=video_colour)
    draw.rectangle((666, 346, 697, 377), fill=(75, 90, 105))
    if cross:
        draw.line((673, 353, 689, 369), fill="white", width=2)
        draw.line((673, 369, 689, 353), fill="white", width=2)
    if banner:
        draw.rectangle((552, 576, 695, 600), fill=(210, 40, 45))
        for x in (585, 591, 597, 616, 623, 630, 637, 644, 651, 665, 672):
            draw.rectangle((x, 581, x + 2, 594), fill="white")
    return img.resize((1080, 2400), Image.Resampling.NEAREST)


class LivePlayerCloseTests(unittest.TestCase):
    def setUp(self):
        serial = patch.object(pdd, "SERIAL", "test-device")
        serial.start()
        self.addCleanup(serial.stop)
        screen = patch.object(pdd, "screen_info", return_value=SC)
        screen.start()
        self.addCleanup(screen.stop)

    def test_player_needs_cross_and_banner(self):
        self.assertTrue(pdd._detect_live_player(frame()))
        self.assertFalse(pdd._detect_live_player(frame(cross=False)))
        self.assertFalse(pdd._detect_live_player(frame(banner=False)))
        self.assertFalse(pdd._detect_live_player(Image.new("RGB", (1080, 2400), "white")))
        self.assertFalse(pdd._detect_live_player(frame().resize((720, 1600))))

    def test_close_requires_two_matching_frames_and_motion(self):
        with patch.object(pdd, "_require_pdd_focus") as focus, \
             patch.object(pdd, "_require_player_touch_window") as window, \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
             patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
            self.assertTrue(pdd._close_live_player_confirmed())
            self.assertEqual(focus.call_count, 3)
            window.assert_called_once_with(1022, 542)
            tap.assert_called_once_with(1022, 542)

        cases = (([frame(), frame()], "не меняется"),
                 ([frame(), frame(cross=False)], "втором"),
                 ([frame(banner=False)], "не найден"))
        for frames, reason in cases:
            with self.subTest(frames=len(frames)), patch.object(pdd, "_require_pdd_focus"), \
                 patch.object(pdd, "screen_info", return_value=SC), \
                 patch.object(pdd, "capture", side_effect=frames), patch.object(pdd.time, "sleep"), \
                 patch.object(pdd, "tap_real") as tap:
                if reason == "не найден":
                    self.assertFalse(pdd._close_live_player_confirmed())
                else:
                    with self.assertRaisesRegex(pdd.AdbError, reason):
                        pdd._close_live_player_confirmed()
                tap.assert_not_called()

    def test_unconfirmed_window_does_not_tap(self):
        with patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "_require_player_touch_window", side_effect=pdd.AdbError("overlay")), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
             patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd._close_live_player_confirmed()
            tap.assert_not_called()

    def test_ambiguous_focus_cannot_confirm_touch_window(self):
        windows = ("mCurrentFocus=Window{abc u0 com.xunmeng.pinduoduo/.Main}\n"
                   "mCurrentFocus=Window{def u0 other.app/.Main}\n")
        with patch.object(pdd, "run_adb", return_value=windows) as adb:
            with self.assertRaises(pdd.AdbError):
                pdd._require_player_touch_window(1022, 542)
            adb.assert_called_once()

    def test_out_of_bounds_close_point_never_taps(self):
        for point in ((-1, 542), (1080, 542), (1022, 87), (1022, 2400)):
            with self.subTest(point=point), \
                 patch.object(pdd, "screen_info", return_value=SC), \
                 patch.object(pdd, "_require_pdd_focus"), \
                 patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
                 patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError):
                    pdd._require_player_tap_bounds(*point)
                tap.assert_not_called()

    def test_focus_change_never_taps(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), \
                 patch.object(pdd, "_require_pdd_focus", side_effect=[None] * (fail_at - 1) + [pdd.AdbError("focus")]), \
                 patch.object(pdd, "screen_info", return_value=SC), \
                 patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
                 patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError):
                    pdd._close_live_player_confirmed()
                tap.assert_not_called()

    def test_failed_dump_is_strictly_passive(self):
        failures = [("ERROR: could not get idle state.", True),
                    ("invalid xml", False)]
        for last, idle in failures:
            with self.subTest(last=last), \
                 patch.object(pdd, "_dump_ui_attempts", return_value=(None, last, idle)) as dump, \
                 patch.object(pdd, "tap_real") as tap, \
                 patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError) as err:
                    pdd.dump_ui_xml()
                self.assertEqual([call.args[0] for call in dump.call_args_list],
                                 [(False,), (True,)])
                tap.assert_not_called()
                adb.assert_not_called()
                text = str(err.exception)
                self.assertIn("автоматических действий", text)
                self.assertIn("close-player", text)
                self.assertNotIn("Выполнена попытка", text)

    def test_compressed_fallback_is_marked_and_passive(self):
        compressed = pdd.UiDump('<?xml version="1.0"?><hierarchy></hierarchy>', compressed=True)
        with patch.object(pdd, "_dump_ui_attempts",
                          side_effect=[(None, "idle", True), (compressed, "", False)]) as dump, \
             patch.object(pdd, "tap_real") as tap:
            self.assertTrue(pdd.dump_ui_xml().compressed)
            self.assertEqual([call.args[0] for call in dump.call_args_list], [(False,), (True,)])
            tap.assert_not_called()

    def test_close_player_command_closes_confirmed_player(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "_require_player_touch_window") as window, \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80)),
                                                       Image.new("RGB", (1080, 2400), "white")]), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as out:
            pdd.cmd_close_player(types.SimpleNamespace())
        window.assert_called_once_with(1022, 542)
        tap.assert_called_once_with(1022, 542)
        self.assertEqual(out.call_args.args[0]["result"], "closed")
        self.assertEqual(out.call_args.args[0]["confirmation"], "state_confirmed")

    def test_close_player_command_reports_not_found_without_tap(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "capture", return_value=frame(banner=False)), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as out:
            pdd.cmd_close_player(types.SimpleNamespace())
        tap.assert_not_called()
        self.assertFalse(out.call_args.args[0]["player_closed"])
        self.assertEqual(out.call_args.args[0]["result"], "not_found")

    def test_close_player_command_refuses_stale_tree_without_tap(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 100.0 + pdd.PLAYER_CLOSE_TREE_MAX_AGE_SECONDS + 1]), \
             patch.object(pdd, "capture") as capture, \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "несвежее"):
                pdd.cmd_close_player(types.SimpleNamespace())
        capture.assert_not_called()
        tap.assert_not_called()

    def test_close_player_command_refuses_ambiguous_overlay_without_tap(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "_require_player_touch_window",
                          side_effect=pdd.AdbError("Нельзя исключить перекрытие")), \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "перекрытие"):
                pdd.cmd_close_player(types.SimpleNamespace())
        tap.assert_not_called()

    def test_close_player_still_visible_reports_unknown(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "_require_player_touch_window"), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80)), frame()]), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as out:
            pdd.cmd_close_player(types.SimpleNamespace())
        tap.assert_called_once()
        payload = out.call_args.args[0]
        self.assertEqual(payload["result"], "unknown")
        self.assertEqual(payload["confirmation"], "unknown")
        self.assertFalse(payload["player_closed"])

    def test_close_player_failed_postcheck_reports_unknown(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "_require_player_touch_window"), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80)),
                                                       pdd.AdbError("screencap timeout")]), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as out:
            pdd.cmd_close_player(types.SimpleNamespace())
        tap.assert_called_once()
        payload = out.call_args.args[0]
        self.assertEqual(payload["result"], "unknown")
        self.assertEqual(payload["confirmation"], "unknown")
        self.assertFalse(payload["player_closed"])


if __name__ == "__main__":
    unittest.main()