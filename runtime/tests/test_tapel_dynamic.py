import types
import unittest
from unittest.mock import patch

import pdd
from test_ui_safety import SC, element


class TapelDynamicTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("ensure_device", None), ("_require_pdd_focus", None),
                            ("screen_info", SC), ("SERIAL", "test-device"), ("out", None)):
            patcher = patch.object(pdd, name, return_value=value) if name not in ("SERIAL",) else patch.object(pdd, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self, elements):
        return {"ui": elements, "ui_signature": pdd.ui_signature(elements), "ui_serial": "test-device",
                "ui_captured_at": pdd.time.time(), "ui_quality": "full", "ui_page_anchors": pdd._ui_page_anchors(elements)}

    def test_changed_ticker_does_not_block_target_and_uses_fresh_position(self):
        old = [element("发起拼单", 620, 1460), element("热*** 刚刚回购", 300, 500)]
        current = [element("发起拼单", 625, 1464), element("热*** 刚刚购买", 300, 500)]
        with patch.object(pdd, "load_state", return_value=self.state(old)), \
             patch.object(pdd, "read_ui_elements", return_value=current), \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_called_once_with(current[0]["rx"], current[0]["ry"], 0)

    def test_missing_moved_or_ambiguous_target_is_rejected(self):
        original = [element("收藏", 300, 300)]
        for current in ([element("收藏", 310, 300)],
                        [element("客服", 300, 300)],
                        [element("收藏", 300, 300), element("收藏", 302, 300)]):
            with self.subTest(current=current), \
                 patch.object(pdd, "load_state", return_value=self.state(original)), \
                 patch.object(pdd, "read_ui_elements", return_value=current), \
                 patch.object(pdd, "tap_real") as tap:
                with self.assertRaises(pdd.AdbError):
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
                tap.assert_not_called()

    def test_new_payment_element_over_target_is_rejected(self):
        original = [element("收藏", 300, 300)]
        current = original + [element("立即支付", 300, 300)]
        with patch.object(pdd, "load_state", return_value=self.state(original)), \
             patch.object(pdd, "read_ui_elements", return_value=current), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_not_called()
