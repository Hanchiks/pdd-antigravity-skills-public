import os
import tempfile
import time
import unittest
from pathlib import Path

import pdd


class CleanTests(unittest.TestCase):
    def test_stale_files_respect_cutoff_and_ignore_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.jpg"
            new = root / "new.jpg"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            old_time = time.time() - 10 * 86400
            os.utime(old, (old_time, old_time))
            stale = pdd._stale_files(root, time.time() - 7 * 86400)
            self.assertEqual(stale, [old])

    def test_missing_directory_is_empty(self):
        self.assertEqual(pdd._stale_files(Path(tempfile.gettempdir()) / "no-such-pdd-dir", time.time()), [])

    def test_clean_rejects_non_positive_days(self):
        args = type("A", (), {"days": 0, "apply": False})()
        with self.assertRaises(pdd.AdbError):
            pdd.cmd_clean(args)


if __name__ == "__main__":
    unittest.main()