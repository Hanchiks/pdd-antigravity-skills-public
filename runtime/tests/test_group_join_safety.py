import types
import unittest
from unittest.mock import patch

import pdd
from test_ui_safety import SC, element


class GroupJoinSafetyTests(unittest.TestCase):
    def setUp(self):
        for name, kwargs in (("ensure_device", {}), ("_require_pdd_focus", {}),
                             ("screen_info", {"return_value": SC}), ("out", {})):
            p = patch.object(pdd, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(pdd, "SERIAL", "test-device")
        p.start()
        self.addCleanup(p.stop)

    def test_join_button_blocked_for_tap_force_and_tapel(self):
        button = element("直接拼成", 610, 1200, 120, 70)
        st = {"ui": [button], "ui_signature": pdd.ui_signature([button]), "ui_serial": "test-device",
              "ui_captured_at": pdd.time.time(), "ui_quality": "full", "ui_page_anchors": []}
        with patch.object(pdd, "load_state", return_value=st), \
             patch.object(pdd, "read_ui_elements", return_value=[button]), \
             patch.object(pdd, "tap_real") as tap:
            for force in (False, True):
                with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                    pdd.cmd_tap(types.SimpleNamespace(x=610, y=1200, long=0, force=force))
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_not_called()

    def test_unrelated_sku_button_remains_allowed(self):
        button = element("直接拼成", 610, 1200, 120, 70, tree_path="0/0")
        sku = element("发起拼单", 610, 1470, 170, 80, tree_path="0/1")
        with patch.object(pdd, "read_ui_elements", return_value=[button, sku]), \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tap(types.SimpleNamespace(x=610, y=1470, long=0))
            tap.assert_called_once_with(915, 2205, 0)

    def test_group_aggregate_blocks_full_uncertain_row(self):
        parent = element("恩赐囤货2次还差1人拼成直接拼成", 360, 1250, 650, 100,
                         tree_path="0/0", click=False)
        text = element("恩赐", 150, 1250, 60, 40, tree_path="0/0/0")
        join = element("直接拼成", 620, 1250, 100, 60, tree_path="0/0/1")
        for force in (False, True):
            for x in (150, 620):
                with self.subTest(force=force, x=x), self.assertRaises(pdd.AdbError):
                    pdd._check_target(x, 1250, [parent, text, join], force=force)

    def test_multiple_group_actions_one_child_does_not_authorize_other_area(self):
        parent = element("直接拼成 | 直接拼成", 360, 1250, 650, 100,
                         tree_path="0/0", click=False)
        join = element("直接拼成", 620, 1250, 100, 60, tree_path="0/0/1")
        # A sibling label could be an incompletely exposed second button.
        other = element("优惠", 150, 1250, 60, 40, tree_path="0/0/0")
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                pdd._check_target(150, 1250, [parent, other, join], force=force)

    def test_group_text_beyond_truncated_output_still_blocked(self):
        xml = (f'<hierarchy><node package="{pdd.PH["package"]}" '
               f'text="{"a" * 121}直接拼成" bounds="[750,1700][1000,1850]" /></hierarchy>')
        with patch.object(pdd, "dump_ui_xml", return_value=xml):
            elements = pdd.read_ui_elements(include_all=True)
        self.assertTrue(pdd._is_forbidden(elements[0]))
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(elements[0]["vx"], elements[0]["vy"], elements)


if __name__ == "__main__":
    unittest.main()
