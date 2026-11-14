"""Offline safety tests for the 02D1F candidate — category (b): stubs, not a device.

Synthetic only: no ADB, no device, no network; ADB and UI output are mocked.
The tests import the candidate ``pdd.py`` from this directory (run with
PYTHONPATH=.:../..). Green tests do not lift the real gesture ban and do not
prove on-device safety.
"""
import subprocess
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


def element(label, vx, vy, vw=100, vh=50, **kw):
    return {"label": label, "desc": "", "rx": round(vx * 1.5), "ry": round(vy * 1.5),
            "vx": vx, "vy": vy, "vw": vw, "vh": vh, "click": kw.get("click", True),
            "rid": kw.get("rid", ""), "placeholder": kw.get("placeholder", False),
            "tree_path": kw.get("tree_path", "0/0"), "package": kw.get("package", pdd.PH["package"])}


class GestureBlockTests(unittest.TestCase):
    def assert_blocked(self, fn, arg):
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
        self.assertIn("guard'а жестов", text)
        self.assertNotIn("выполнена", text.lower())

    def test_scroll_and_swipe_are_blocked_before_device(self):
        self.assert_blocked(pdd.cmd_scroll,
                            types.SimpleNamespace(direction="down", amount=0.6, ms=450))
        self.assert_blocked(pdd.cmd_swipe,
                            types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=400, ms=400))

    def test_long_press_is_blocked_before_any_adb(self):
        # `input swipe` с совпадающими точками — это долгое нажатие, то есть
        # жест; оно должно быть перехвачено до run_adb, как scroll/swipe.
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


class PassiveReadTests(unittest.TestCase):
    def test_failed_dump_does_not_tap_or_close(self):
        for last, idle in (("ERROR: could not get idle state.", True), ("invalid xml", False)):
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

    def test_compressed_fallback_is_passive(self):
        compressed = pdd.UiDump('<?xml version="1.0"?><hierarchy></hierarchy>', compressed=True)
        with patch.object(pdd, "_dump_ui_attempts",
                          side_effect=[(None, "idle", True), (compressed, "", False)]), \
             patch.object(pdd, "tap_real") as tap:
            self.assertTrue(pdd.dump_ui_xml().compressed)
            tap.assert_not_called()

    def test_read_ui_elements_failure_path_never_taps(self):
        with patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "dump_ui_xml", side_effect=pdd.UiIdleError("idle")), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.UiIdleError):
                pdd.read_ui_elements(include_all=True)
            tap.assert_not_called()
            adb.assert_not_called()


class ExplicitClosePlayerTests(unittest.TestCase):
    def setUp(self):
        screen = patch.object(pdd, "screen_info", return_value=SC)
        screen.start()
        self.addCleanup(screen.stop)

    def test_close_player_closes_confirmed_player(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "SERIAL", "FAKE-A"), \
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

    def test_close_player_not_found_does_not_tap(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "capture", return_value=frame(banner=False)), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap, \
             patch.object(pdd, "out") as out:
            pdd.cmd_close_player(types.SimpleNamespace())
        tap.assert_not_called()
        self.assertFalse(out.call_args.args[0]["player_closed"])
        self.assertEqual(out.call_args.args[0]["result"], "not_found")

    def test_close_player_refuses_stale_tree_without_tap(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time",
                          side_effect=[100.0, 100.0 + pdd.PLAYER_CLOSE_TREE_MAX_AGE_SECONDS + 1]), \
             patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "capture") as capture, \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "несвежее"):
                pdd.cmd_close_player(types.SimpleNamespace())
        capture.assert_not_called()
        tap.assert_not_called()

    def test_close_player_refuses_ambiguous_overlay_without_tap(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "_require_player_touch_window",
                          side_effect=pdd.AdbError("Нельзя исключить перекрытие")), \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "capture", side_effect=[frame(), frame((140, 110, 80))]), \
             patch.object(pdd.time, "sleep"), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "перекрытие"):
                pdd.cmd_close_player(types.SimpleNamespace())
        tap.assert_not_called()


class Stage1CSafetyTests(unittest.TestCase):
    """Offline checks for dangerous zones, multi-index taps and key actions."""

    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("SERIAL", "FAKE-A"), ("out", None)):
            patcher = (patch.object(pdd, name, return_value=value) if name == "screen_info"
                       else patch.object(pdd, name, value) if name == "SERIAL"
                       else patch.object(pdd, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self, els):
        return {"ui": els, "ui_signature": pdd.ui_signature(els), "ui_serial": "FAKE-A",
                "ui_captured_at": pdd.time.time(), "ui_quality": "full",
                "ui_page_anchors": pdd._ui_page_anchors(els)}

    def test_force_cannot_skip_unlabelled_bottom_zone(self):
        target = element("发起拼单", 630, 1470, 160, 70, tree_path="0/0")
        overlay = element("<overlay>", 630, 1470, 160, 70, tree_path="0/1", placeholder=True)
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                pdd._check_target(630, 1470, [target, overlay], force=force)

    def test_documented_select_all_synonym_is_forbidden(self):
        e = element("Выбрать всё", 50, 1200)
        self.assertTrue(pdd._is_forbidden(e))

    def test_tapel_refuses_multiple_unlabelled_or_select_all_targets(self):
        cases = {
            "placeholder": [element("<CheckBox>", 50, 200, placeholder=True, tree_path="0/0"),
                            element("<CheckBox>", 50, 300, placeholder=True, tree_path="0/1")],
            "select_all": [element("商品甲", 200, 200, tree_path="0/0"),
                           element("商品乙", 200, 300, tree_path="0/1"),
                           element("全选", 50, 1470, tree_path="0/2")],
        }
        for reason, els in cases.items():
            with self.subTest(reason=reason), \
                 patch.object(pdd, "load_state", return_value=self.state(els)), \
                 patch.object(pdd, "read_ui_elements", return_value=els), \
                 patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0, 1], long=0))
                tap.assert_not_called()

    def test_key_del_and_search_require_focused_field(self):
        for key in ("del", "search"):
            with self.subTest(key=key), \
                 patch.object(pdd, "_require_focused_editable",
                              side_effect=pdd.AdbError("поле не подтверждено")) as check, \
                 patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_key(types.SimpleNamespace(key=key))
                check.assert_called_once_with(require_search=(key == "search"))
                adb.assert_not_called()

    def test_key_requires_fresh_full_tree(self):
        for key in ("back", "home", "recent"):
            with self.subTest(key=key), \
                 patch.object(pdd, "read_ui_elements",
                              side_effect=pdd.AdbError("дерево недоступно")) as read, \
                 patch.object(pdd, "run_adb") as adb:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_key(types.SimpleNamespace(key=key))
                read.assert_called_once_with(include_all=True, require_full=True)
                adb.assert_not_called()

    def test_window_change_between_check_and_tap_blocks(self):
        tree = [element("收藏", 300, 300)]
        with patch.object(pdd, "read_ui_elements", return_value=tree), \
             patch.object(pdd, "_require_pdd_focus",
                          side_effect=[None, pdd.AdbError("окно изменилось")]), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tap(types.SimpleNamespace(x=300, y=300, long=0))
            tap.assert_not_called()


class ActionResultTests(unittest.TestCase):
    def test_tap_timeout_reports_unknown_without_resend(self):
        with patch.object(pdd, "run_adb",
                          side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=40)) as adb, \
             patch.object(pdd, "pace") as pace:
            with self.assertRaisesRegex(pdd.AdbError, "unknown") as err:
                pdd.tap_real(450, 450)
            self.assertIn("неизвестно", str(err.exception))
            self.assertIn("Повтор вслепую", str(err.exception))
            self.assertEqual(adb.call_count, 1)
            pace.assert_not_called()

    def test_close_player_still_visible_reports_unknown(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd.time, "time", side_effect=[100.0, 101.0]), \
             patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "_require_player_touch_window"), \
             patch.object(pdd, "screen_info", return_value=SC), \
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
             patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "_require_player_touch_window"), \
             patch.object(pdd, "screen_info", return_value=SC), \
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