import hashlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pdd


class ApkIntegrityTests(unittest.TestCase):
    def test_cached_apk_matches_pinned_hash(self):
        apk = pdd.ROOT / "tools" / "ADBKeyboard.apk"
        if not apk.exists():
            self.skipTest("ADBKeyboard.apk ещё не скачан")
        self.assertEqual(pdd._sha256_file(apk).lower(), pdd.ADBKEYBOARD_SHA256)

    def test_tampered_apk_is_deleted_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ADBKeyboard.apk"
            path.write_bytes(b"not the expected apk")
            with self.assertRaises(pdd.AdbError):
                pdd.verify_adbkeyboard_apk(path)
            self.assertFalse(path.exists())


class KeyboardAuthenticityTests(unittest.TestCase):
    APK_PATH = "/data/app/~~abc==/com.android.adbkeyboard-1==/base.apk"
    APK_BYTES = b"FAKE-APK-CONTENT"

    def _adb(self, *, installed=True, paths=None, data=None, read_error=False):
        def fake(args, **kwargs):
            if list(args[:5]) == ["shell", "pm", "list", "packages", pdd.ADBKEYBOARD_PACKAGE]:
                return "package:com.android.adbkeyboard\n" if installed else ""
            if list(args[:3]) == ["shell", "pm", "path"]:
                return "".join(f"package:{p}\n" for p in (paths if paths is not None else [self.APK_PATH]))
            if list(args[:3]) == ["exec-out", "cat", self.APK_PATH]:
                if read_error:
                    raise pdd.AdbError("read denied")
                return data if data is not None else self.APK_BYTES
            raise AssertionError(f"unexpected adb args: {args}")
        return fake

    def test_authentic_installed_apk_matches_pinned_hash(self):
        digest = hashlib.sha256(self.APK_BYTES).hexdigest()
        with patch.object(pdd, "ADBKEYBOARD_SHA256", digest), \
             patch.object(pdd, "run_adb", side_effect=self._adb()):
            status = pdd.adbkeyboard_status()
        self.assertEqual(status["installed"], True)
        self.assertEqual(status["authenticity"], "authentic")

    def test_tampered_installed_apk_is_reported(self):
        with patch.object(pdd, "ADBKEYBOARD_SHA256", "0" * 64), \
             patch.object(pdd, "run_adb", side_effect=self._adb()):
            status = pdd.adbkeyboard_status()
        self.assertEqual(status["authenticity"], "tampered")

    def test_unreadable_or_split_apk_is_unknown(self):
        cases = [
            self._adb(paths=[]),
            self._adb(paths=[self.APK_PATH, "/data/app/split/config.apk"]),
            self._adb(read_error=True),
        ]
        for fake in cases:
            with self.subTest(fake=fake), patch.object(pdd, "run_adb", side_effect=fake):
                status = pdd.adbkeyboard_status()
                self.assertEqual(status["authenticity"], "unknown")
                self.assertTrue(status["installed"])

    def test_missing_keyboard_is_not_installed(self):
        with patch.object(pdd, "run_adb", side_effect=self._adb(installed=False)):
            status = pdd.adbkeyboard_status()
        self.assertEqual(status, {"installed": False, "authenticity": "not_installed"})

    def test_install_refuses_tampered_keyboard_without_installing(self):
        with patch.object(pdd, "ensure_device"), \
             patch.object(pdd, "adbkeyboard_status", return_value={
                 "installed": True, "authenticity": "tampered"}), \
             patch.object(pdd, "run_adb") as adb:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_install_adbkeyboard(types.SimpleNamespace())
        adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()