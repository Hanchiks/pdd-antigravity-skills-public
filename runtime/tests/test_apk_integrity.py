import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()