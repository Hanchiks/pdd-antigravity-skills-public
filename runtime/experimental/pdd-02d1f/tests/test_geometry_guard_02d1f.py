"""Геометрия экрана для экспериментального кандидата 02D1F (категория (б)).

ADB/wm size/PNG подменены; телефон не используется. Проверяются те же
инварианты, что и в рабочей версии: поворот, смена разрешения, отсутствие
метрик и несовпадение размера скриншота.
"""
import copy
import io
import unittest
from unittest.mock import patch

from PIL import Image

import pdd


class GeometryGuardTests(unittest.TestCase):
    """Категория (б): wm size и PNG подменены; телефон не используется."""

    def setUp(self):
        self.state = {"screen": {"serial": "test-device", "real_w": 1080, "real_h": 2400,
                                 "view_w": 720, "view_h": 1600, "scale": 1.5}}
        self.wm = "Physical size: 1080x2400\n"
        def load():
            return copy.deepcopy(self.state)
        def update(fn):
            return fn(self.state)
        for name, value in (("load_state", load), ("update_state", update),
                            ("SERIAL", "test-device"), ("out", None)):
            patcher = (patch.object(pdd, name, side_effect=value) if name in ("load_state", "update_state")
                       else patch.object(pdd, name, value))
            patcher.start()
            self.addCleanup(patcher.stop)
        def adb(args, **kwargs):
            if args[:3] == ["shell", "wm", "size"]:
                return self.wm
            self.fail(f"unexpected adb call: {args}")
        self.adb = patch.object(pdd, "run_adb", side_effect=adb)
        self.adb.start()
        self.addCleanup(self.adb.stop)

    def test_same_dimensions_are_accepted_and_orientation_is_recorded(self):
        fresh = pdd._require_fresh_geometry()
        self.assertEqual((fresh["real_w"], fresh["real_h"]), (1080, 2400))
        self.assertEqual(fresh["orientation"], "portrait")
        self.assertIn("captured_at", fresh)

    def test_rotation_invalidates_cache_and_refuses(self):
        self.wm = "Physical size: 2400x1080\n"
        with self.assertRaisesRegex(pdd.AdbError, "Геометрия экрана изменилась"):
            pdd._require_fresh_geometry()
        self.assertNotIn("screen", self.state)

    def test_resolution_change_invalidates_cache_and_refuses(self):
        self.wm = "Override size: 1440x3200\nPhysical size: 1080x2400\n"
        with self.assertRaisesRegex(pdd.AdbError, "Геометрия экрана изменилась"):
            pdd._require_fresh_geometry()
        self.assertNotIn("screen", self.state)

    def test_missing_or_unparseable_metrics_refuse_and_invalidate(self):
        for response in ("", "Physical size: unknown", "garbage"):
            with self.subTest(response=response):
                self.state["screen"] = {"serial": "test-device", "real_w": 1080, "real_h": 2400}
                self.wm = response
                with self.assertRaises(pdd.AdbError):
                    pdd._require_fresh_geometry()
                self.assertNotIn("screen", self.state)

    def test_capture_refuses_screenshot_of_different_size(self):
        def adb(args, **kwargs):
            self.assertEqual(args[:2], ["exec-out", "screencap"])
            return png(720, 1600)
        with patch.object(pdd, "run_adb", side_effect=adb):
            with self.assertRaisesRegex(pdd.AdbError, "Размер скриншота"):
                pdd.capture()
        self.assertNotIn("screen", self.state)

    def test_capture_accepts_matching_screenshot(self):
        def adb(args, **kwargs):
            return png(1080, 2400)
        with patch.object(pdd, "run_adb", side_effect=adb):
            img = pdd.capture()
        self.assertEqual(img.size, (1080, 2400))


def png(width, height):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, format="PNG")
    return stream.getvalue()

if __name__ == "__main__":
    unittest.main()
