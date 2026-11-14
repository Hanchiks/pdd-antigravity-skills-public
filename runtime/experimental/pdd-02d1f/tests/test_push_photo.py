import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import pdd


class PushPhotoPathTests(unittest.TestCase):
    def test_valid_remote_path_is_within_allowed_directory(self):
        path = pdd._photo_remote_path("/sdcard/DCIM/Camera", "pdd_search_ref_1.jpg")
        self.assertEqual(path, "/sdcard/DCIM/Camera/pdd_search_ref_1.jpg")

    def test_paths_with_separators_or_traversal_are_rejected(self):
        names = ["../evil.jpg", "a/b.jpg", "a\\b.jpg", "..", ".", "", "bad name.jpg", "x" * 121]
        for name in names:
            with self.subTest(name=name), self.assertRaises(pdd.AdbError):
                pdd._safe_photo_name(name)
        for directory in ("/sdcard/Download", "/sdcard/DCIM/Camera/../Pictures", ""):
            with self.subTest(directory=directory), self.assertRaises(pdd.AdbError):
                pdd._photo_remote_path(directory, "photo.jpg")


class PushPhotoCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "source.png"
        Image.new("RGB", (12, 8), "white").save(self.src)
        patcher = patch.object(pdd, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(pdd, "ensure_device")
        patcher.start()
        self.addCleanup(patcher.stop)
        serial = patch.object(pdd, "SERIAL", "test-device")
        serial.start()
        self.addCleanup(serial.stop)
        self.pushes = []
        self.existing = set()

    def _adb(self, args, **kwargs):
        if args[:1] == ["shell"] and isinstance(args[1], str) and args[1].startswith("test -e "):
            return "EXISTS" if any(path in args[1] for path in self.existing) else "MISSING"
        if args[:1] == ["push"]:
            self.pushes.append(args[2])
            return ""
        if args[:3] == ["shell", "am", "broadcast"]:
            return ""
        raise AssertionError(f"unexpected adb args: {args}")

    def test_two_calls_use_distinct_unique_names_and_report_only_created(self):
        captured = []
        with patch.object(pdd, "run_adb", side_effect=self._adb), \
             patch.object(pdd, "out", side_effect=captured.append):
            pdd.cmd_push_photo(types.SimpleNamespace(image=str(self.src), crop=None))
            first = list(self.pushes)
            pdd.cmd_push_photo(types.SimpleNamespace(image=str(self.src), crop=None))
            second = list(self.pushes[len(first):])
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(len(set(first + second)), 4)
        for path in first + second:
            self.assertTrue(path.startswith("/sdcard/DCIM/Camera/pdd_search_ref_")
                            or path.startswith("/sdcard/Pictures/pdd_search_ref_"))
        self.assertEqual(captured[0]["remote"], first)
        self.assertEqual(captured[1]["remote"], second)
        self.assertNotEqual(captured[0]["local_jpg"], captured[1]["local_jpg"])

    def test_existing_file_is_never_overwritten(self):
        taken = "/sdcard/DCIM/Camera/pdd_search_ref_taken.jpg"
        self.existing.add(taken)
        local = self.root / "logs" / "local.jpg"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local")
        names = ["pdd_search_ref_taken.jpg", "pdd_search_ref_camera.jpg", "pdd_search_ref_pictures.jpg"]
        captured = []
        with patch.object(pdd, "run_adb", side_effect=self._adb), \
             patch.object(pdd, "_unique_local_photo_path", return_value=local), \
             patch.object(pdd, "_photo_candidate_name", side_effect=names), \
             patch.object(pdd, "out", side_effect=captured.append):
            pdd.cmd_push_photo(types.SimpleNamespace(image=str(self.src), crop=None))
        self.assertNotIn(taken, self.pushes)
        self.assertEqual(self.pushes, [
            "/sdcard/DCIM/Camera/pdd_search_ref_camera.jpg",
            "/sdcard/Pictures/pdd_search_ref_pictures.jpg",
        ])
        self.assertEqual(captured[0]["remote"], self.pushes)

    def test_failure_reports_only_paths_already_created(self):
        local = self.root / "logs" / "local.jpg"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local")
        names = ["pdd_search_ref_camera.jpg", "pdd_search_ref_pictures.jpg"]

        def adb(args, **kwargs):
            if args[:1] == ["push"]:
                if args[2].startswith("/sdcard/Pictures/"):
                    raise pdd.AdbError("push failed")
                self.pushes.append(args[2])
                return ""
            return self._adb(args, **kwargs)

        with patch.object(pdd, "run_adb", side_effect=adb), \
             patch.object(pdd, "_unique_local_photo_path", return_value=local), \
             patch.object(pdd, "_photo_candidate_name", side_effect=names):
            with self.assertRaises(pdd.AdbError) as ctx:
                pdd.cmd_push_photo(types.SimpleNamespace(image=str(self.src), crop=None))
        message = str(ctx.exception)
        self.assertIn("/sdcard/DCIM/Camera/pdd_search_ref_camera.jpg", message)
        self.assertNotIn("remote", message)
        self.assertEqual(self.pushes, ["/sdcard/DCIM/Camera/pdd_search_ref_camera.jpg"])


if __name__ == "__main__":
    unittest.main()