import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import pdd


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
    def test_player_needs_cross_and_banner(self):
        self.assertTrue(pdd._detect_live_player(frame()))
        self.assertFalse(pdd._detect_live_player(frame(cross=False)))
        self.assertFalse(pdd._detect_live_player(frame(banner=False)))
        self.assertFalse(pdd._detect_live_player(Image.new("RGB", (1080, 2400), "white")))
        self.assertFalse(pdd._detect_live_player(frame().resize((720, 1600))))

    def test_close_requires_two_matching_frames_and_motion(self):
        with patch.object(pdd, "_require_pdd_focus") as focus, \
             patch.object(pdd, "_require_player_touch_window") as window, \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
             patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
            self.assertTrue(pdd._close_live_player_after_failed_dump())
            self.assertEqual(focus.call_count, 3)
            window.assert_called_once_with(1022, 542)
            tap.assert_called_once_with(1022, 542)

        cases = ([frame(), frame()], [frame(), frame(cross=False)],
                 [frame(banner=False)])
        for frames in cases:
            with self.subTest(frames=len(frames)), patch.object(pdd, "_require_pdd_focus"), \
                 patch.object(pdd, "capture", side_effect=frames), patch.object(pdd.time, "sleep"), \
                 patch.object(pdd, "tap_real") as tap:
                self.assertFalse(pdd._close_live_player_after_failed_dump())
                tap.assert_not_called()

    def test_unconfirmed_window_does_not_tap(self):
        with patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "_require_player_touch_window", side_effect=pdd.AdbError("overlay")), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
             patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
            self.assertFalse(pdd._close_live_player_after_failed_dump())
            tap.assert_not_called()

    def test_ambiguous_focus_cannot_confirm_touch_window(self):
        windows = ("mCurrentFocus=Window{abc u0 com.xunmeng.pinduoduo/.Main}\n"
                   "mCurrentFocus=Window{def u0 other.app/.Main}\n")
        with patch.object(pdd, "run_adb", return_value=windows) as adb:
            with self.assertRaises(pdd.AdbError):
                pdd._require_player_touch_window(1022, 542)
            adb.assert_called_once()

    def test_focus_change_never_taps(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), \
                 patch.object(pdd, "_require_pdd_focus", side_effect=[None] * (fail_at - 1) + [pdd.AdbError("focus")]), \
                 patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
                 patch.object(pdd.time, "sleep"), patch.object(pdd, "tap_real") as tap:
                self.assertFalse(pdd._close_live_player_after_failed_dump())
                tap.assert_not_called()

    def test_dump_retries_once_only_after_idle_like_failures(self):
        xml = pdd.UiDump('<?xml version="1.0"?><hierarchy></hierarchy>')
        with patch.object(pdd, "_dump_ui_attempts", side_effect=[(None, "idle", True), (xml, "", False)]) as dump, \
             patch.object(pdd, "_close_live_player_after_failed_dump", return_value=True) as close:
            self.assertEqual(pdd.dump_ui_xml(), xml)
            self.assertEqual(dump.call_count, 2)
            self.assertEqual([call.args[0] for call in dump.call_args_list], [(False,), (False,)])
            close.assert_called_once()
        for first, closed in (((None, "invalid xml", False), True), ((None, "idle", True), False)):
            with self.subTest(first=first, closed=closed), \
                 patch.object(pdd, "_dump_ui_attempts", return_value=first) as dump, \
                 patch.object(pdd, "_close_live_player_after_failed_dump", return_value=closed):
                with self.assertRaises(pdd.AdbError):
                    pdd.dump_ui_xml()
                self.assertEqual([call.args[0] for call in dump.call_args_list], [(False,), (True,)])
        with patch.object(pdd, "_dump_ui_attempts", side_effect=[(None, "idle", True), (None, "idle", True), (None, "idle", True)]) as dump, \
             patch.object(pdd, "_close_live_player_after_failed_dump", return_value=True) as close:
            with self.assertRaises(pdd.AdbError):
                pdd.dump_ui_xml()
            close.assert_called_once()
            self.assertEqual([call.args[0] for call in dump.call_args_list], [(False,), (False,), (True,)])

    def test_compressed_fallback_after_player_close_is_marked(self):
        compressed = pdd.UiDump('<?xml version="1.0"?><hierarchy></hierarchy>', compressed=True)
        for closed in (True, False):
            attempts = [(None, "idle", True)]
            if closed:
                attempts.append((None, "idle", True))
            attempts.append((compressed, "", False))
            with self.subTest(closed=closed), \
                 patch.object(pdd, "_dump_ui_attempts", side_effect=attempts), \
                 patch.object(pdd, "_close_live_player_after_failed_dump", return_value=closed) as close:
                self.assertTrue(pdd.dump_ui_xml().compressed)
                close.assert_called_once()
