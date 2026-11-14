import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pdd
from test_ui_safety import SC, element


class UiFreshnessTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("out", None)):
            patcher = patch.object(pdd, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        serial = patch.object(pdd, "SERIAL", "device")
        serial.start()
        self.addCleanup(serial.stop)

    def state(self, elements, timestamp=1000):
        return {"ui": elements, "ui_signature": pdd.ui_signature(elements), "ui_serial": "device",
                "ui_captured_at": timestamp, "ui_quality": "full", "ui_page_anchors": pdd._ui_page_anchors(elements)}

    def test_missing_expired_future_or_invalid_time_blocks(self):
        target = [element("收藏", 300, 300)]
        for timestamp in (None, 819, 1001, float("nan"), "1000", True):
            with self.subTest(timestamp=timestamp), patch.object(pdd.time, "time", return_value=1000), \
                 patch.object(pdd, "load_state", return_value=self.state(target, timestamp)), \
                 patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
                tap.assert_not_called()

    def test_expiration_during_dump_blocks(self):
        target = [element("收藏", 300, 300)]
        with patch.object(pdd.time, "time", side_effect=[1179, 1179, 1181]), \
             patch.object(pdd, "load_state", return_value=self.state(target)), \
             patch.object(pdd, "read_ui_elements", return_value=target), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_not_called()

    def test_same_button_on_changed_anchored_page_is_rejected(self):
        target = element("收藏", 300, 300)
        original = [target, element("棉质针织衫", 300, 500, rid="goods_title")]
        for title in ("另一件商品", None):
            current = [target] + ([element(title, 300, 500, rid="goods_title")] if title else [])
            with self.subTest(title=title), patch.object(pdd.time, "time", return_value=1001), \
                 patch.object(pdd, "load_state", return_value=self.state(original)), \
                 patch.object(pdd, "read_ui_elements", return_value=current), \
                 patch.object(pdd, "tap_real") as tap:
                with self.assertRaisesRegex(pdd.AdbError, "признаки страницы"):
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
                tap.assert_not_called()

    def test_observed_tv_title_is_used_as_page_anchor(self):
        first = element("商品一", 300, 500, rid="tv_title", **{"class": "android.view.ViewGroup"})
        other = element("商品二", 300, 500, rid="tv_title", **{"class": "android.view.ViewGroup"})
        self.assertNotEqual(pdd._ui_page_anchors([first]), pdd._ui_page_anchors([other]))
        self.assertEqual(pdd._ui_page_anchors([first]), [("tv_title", "商品一", "")])
        reviews = element("商品评价(38524)", 300, 800, rid="tv_title", **{"class": "android.widget.TextView"})
        self.assertEqual(pdd._ui_page_anchors([reviews]), [])

    def test_ticker_changes_are_ignored_when_anchor_is_same(self):
        target = element("收藏", 300, 300)
        title = element("棉质针织衫", 300, 500, rid="goods_title")
        old = [target, title, element("热*** 刚刚回购", 200, 700)]
        current = [target, title, element("买*** 刚刚购买", 200, 700)]
        with patch.object(pdd.time, "time", return_value=1001), \
             patch.object(pdd, "load_state", return_value=self.state(old)), \
             patch.object(pdd, "read_ui_elements", return_value=current) as read, \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            read.assert_called_once_with(include_all=True, require_full=True)
            tap.assert_called_once()

    def test_ui_saves_full_context_even_when_output_filtered(self):
        state = {}
        elements = pdd.UiElements([element("收藏", 300, 300), element("商品一", 300, 500, rid="goods_title")])
        with tempfile.TemporaryDirectory() as path, \
             patch.object(pdd.time, "time", return_value=1000), \
             patch.object(pdd, "read_ui_elements", return_value=elements), \
             patch.object(pdd, "update_state", side_effect=lambda f: f(state)), \
             patch.object(pdd, "log_dir", return_value=Path(path)):
            pdd.cmd_ui(types.SimpleNamespace(all=False, find="收藏"))
        self.assertEqual(len(state["ui"]), 1)
        self.assertEqual(state["ui_captured_at"], 1000)
        self.assertEqual(state["ui_page_anchors"], [("goods_title", "商品一", "")])
        self.assertEqual(state["ui_quality"], "full")

    def test_tapel_rejects_compressed_or_unknown_original_tree_before_reading(self):
        target = [element("收藏", 300, 300)]
        for quality in ("compressed", "unknown", None):
            state = self.state(target)
            if quality is None:
                del state["ui_quality"]
            else:
                state["ui_quality"] = quality
            with self.subTest(quality=quality), patch.object(pdd, "load_state", return_value=state), \
                 patch.object(pdd, "read_ui_elements", return_value=pdd.UiElements(target)) as read, \
                 patch.object(pdd, "tap_real") as tap:
                with self.assertRaisesRegex(pdd.AdbError, "Исходное дерево"):
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
                read.assert_not_called()
                tap.assert_not_called()

    def test_ui_warns_when_dump_is_compressed(self):
        class CompressedList(list):
            compressed = True

        elements = CompressedList([element("收藏", 300, 300)])
        state = {}
        with tempfile.TemporaryDirectory() as path, \
             patch.object(pdd, "read_ui_elements", return_value=elements), \
             patch.object(pdd, "update_state", side_effect=lambda f: f(state)), \
             patch.object(pdd, "log_dir", return_value=Path(path)), \
             patch.object(pdd, "out") as output:
            pdd.cmd_ui(types.SimpleNamespace(all=False, find=None))
        self.assertIn("Сжатый дамп", output.call_args.args[0])
        self.assertEqual(state["ui_quality"], "compressed")


if __name__ == "__main__":
    unittest.main()
