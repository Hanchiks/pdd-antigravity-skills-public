"""02D1F offline identity tests: serial binding and input focus.

Synthetic only: no ADB, no device, no network. Each test states the required
behaviour; on the 02D1R baseline the two identity gaps are expected to fail.
"""
import time
import types
import unittest
from unittest.mock import patch

import pdd

SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}


def element(label, vx, vy, vw=60, vh=30, **kw):
    return {
        "label": label, "desc": "", "rx": round(vx * 1.5), "ry": round(vy * 1.5),
        "vx": vx, "vy": vy, "vw": vw, "vh": vh, "click": kw.get("click", False),
        "rid": kw.get("rid", ""), "class": "android.widget.TextView",
        "placeholder": kw.get("placeholder", False), "editable": False,
        "tree_path": kw.get("tree_path", "0/0"), "package": pdd.PH["package"],
        "forbidden": False, "group_join_forbidden": False,
    }


def tree(*, rid="search_input", bounds="[90,150][900,240]", extra=""):
    return pdd.UiDump(
        f'<hierarchy><node package="{pdd.PH["package"]}" class="android.widget.EditText" '
        f'resource-id="{rid}" focused="true" editable="true" enabled="true" '
        f'bounds="{bounds}" text="" />{extra}</hierarchy>')


class SerialIdentityTests(unittest.TestCase):
    def setUp(self):
        screen = patch.object(pdd, "screen_info", return_value=SC)
        screen.start()
        self.addCleanup(screen.stop)

    def test_multiple_devices_without_pinned_serial_is_rejected(self):
        with patch.object(pdd, "PH", {**pdd.PH, "serial": ""}), \
             patch.object(pdd, "list_devices", return_value=[("FAKE-A", "device"), ("FAKE-B", "device")]):
            with self.assertRaises(pdd.AdbError):
                pdd.ensure_device()

    def test_single_device_is_bound_when_serial_not_pinned(self):
        with patch.object(pdd, "PH", {**pdd.PH, "serial": ""}), \
             patch.object(pdd, "list_devices", return_value=[("FAKE-A", "device")]):
            self.assertEqual(pdd.ensure_device(), "FAKE-A")

    def test_pinned_serial_missing_is_rejected(self):
        with patch.object(pdd, "PH", {**pdd.PH, "serial": "FAKE-A"}), \
             patch.object(pdd, "list_devices", return_value=[("FAKE-B", "device")]), \
             patch.object(pdd, "run_adb", return_value=""), patch.object(pdd.time, "sleep"):
            with self.assertRaises(pdd.AdbError):
                pdd.ensure_device()

    def _tapel_state(self, serial):
        els = [element("товар", 200, 800)]
        return {"ui": els, "ui_signature": pdd.ui_signature(els), "ui_serial": serial,
                "ui_captured_at": time.time(), "ui_quality": "full", "ui_page_anchors": []}

    def test_changed_serial_blocks_stale_tree_tap(self):
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "SERIAL", "FAKE-B"), \
             patch.object(pdd, "load_state", return_value=self._tapel_state("FAKE-A")):
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))

    def test_missing_tree_serial_blocks_tap(self):
        state = self._tapel_state("FAKE-A")
        state.pop("ui_serial")
        with patch.object(pdd, "ensure_device"), patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "load_state", return_value=state):
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))

    def test_direct_ui_helper_without_bound_serial_is_rejected(self):
        with patch.object(pdd, "SERIAL", None), \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "dump_ui_xml", return_value=tree()):
            with self.assertRaises(pdd.AdbError):
                pdd.read_ui_elements(include_all=True)


class FocusIdentityTests(unittest.TestCase):
    def setUp(self):
        screen = patch.object(pdd, "screen_info", return_value=SC)
        screen.start()
        self.addCleanup(screen.stop)

    def test_focus_identity_includes_vertical_bounds(self):
        first = tree(bounds="[90,150][900,240]")
        moved = tree(bounds="[90,1500][900,1590]")
        with patch.object(pdd, "_require_pdd_focus"), \
             patch.object(pdd, "screen_info", return_value=SC), \
             patch.object(pdd, "dump_ui_xml", side_effect=[first, moved]):
            identity = pdd._require_focused_editable(require_search=True)
            with self.assertRaises(pdd.AdbError):
                pdd._require_focused_editable(expected=identity, require_search=True)

    def test_force_cannot_override_focus_identity(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "SERIAL", "FAKE-A"), \
             patch.object(pdd, "_require_pdd_focus", side_effect=pdd.AdbError("не Pinduoduo")), \
             patch.object(pdd, "read_ui_elements", return_value=[]), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tap(types.SimpleNamespace(x=200, y=800, long=0, force=True))
            tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
