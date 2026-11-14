#!/usr/bin/env python3
"""Focused tests for strict backup-relative-path and independence validation."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transaction_plan import validate_backup_relative_path, verify_backup

PAYLOAD = b"synthetic backup fixture, no user data\n"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
SCOPE = {"backup_out_of_scope", "backup_not_independent"}


class LexicalTests(unittest.TestCase):
    def test_backslash_is_rejected(self):
        self.assertEqual(validate_backup_relative_path("..\\outside.md"), "backup_out_of_scope")

    def test_drive_absolute_is_rejected(self):
        self.assertEqual(validate_backup_relative_path(r"C:\synthetic\copy.md"), "backup_out_of_scope")

    def test_unc_is_rejected(self):
        self.assertEqual(validate_backup_relative_path(r"\\server\share\copy.md"), "backup_out_of_scope")

    def test_posix_absolute_is_rejected(self):
        self.assertEqual(validate_backup_relative_path("/etc/passwd"), "backup_out_of_scope")

    def test_parent_traversal_is_rejected(self):
        self.assertEqual(validate_backup_relative_path("../x"), "backup_out_of_scope")

    def test_dot_and_empty_components_are_rejected(self):
        for value in ("a//b", "./a", "a/.", "", "a/"):
            with self.subTest(value=value):
                self.assertEqual(validate_backup_relative_path(value), "backup_out_of_scope")

    def test_plain_name_is_accepted(self):
        self.assertIsNone(validate_backup_relative_path("copy.md"))

    def test_nested_relative_is_accepted(self):
        self.assertIsNone(validate_backup_relative_path("nested/dir/copy.md"))


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="02ag1a4-backup-"))
        self.root = self.tmp / "backup_root"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_regular_file_is_accepted(self):
        p = self.root / "copy.md"
        p.write_bytes(PAYLOAD)
        self.assertIsNone(verify_backup({"path": "copy.md"}, DIGEST, self.root))

    def test_missing_file_is_reported(self):
        self.assertEqual(verify_backup({"path": "copy.md"}, DIGEST, self.root), "backup_missing")

    def test_hash_mismatch_is_reported(self):
        (self.root / "copy.md").write_bytes(b"other")
        self.assertEqual(verify_backup({"path": "copy.md"}, DIGEST, self.root), "backup_hash_mismatch")

    def test_missing_root_is_reported(self):
        self.assertEqual(verify_backup({"path": "copy.md"}, DIGEST, self.tmp / "nope"), "backup_missing")

    def test_backup_inside_live_config_is_rejected(self):
        live = self.tmp / ".gemini" / "config"
        (live / "rules").mkdir(parents=True)
        (live / "rules" / "AGENTS.md").write_bytes(PAYLOAD)
        self.assertEqual(verify_backup({"path": "rules/AGENTS.md"}, DIGEST, live), "backup_not_independent")

    def test_nested_parent_link_is_rejected(self):
        if os.name != "nt":
            self.skipTest("junction test is Windows-only")
        external = self.tmp / "external"
        external.mkdir()
        (external / "copy.md").write_bytes(PAYLOAD)
        link = self.root / "nested"
        link.mkdir()
        inner = link / "redirect"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(inner), str(external)],
                       check=True, capture_output=True, text=True)
        self.assertIn(verify_backup({"path": "nested/redirect/copy.md"}, DIGEST, self.root), SCOPE)

    def test_root_itself_as_junction_is_rejected(self):
        if os.name != "nt":
            self.skipTest("junction test is Windows-only")
        real = self.tmp / "real_root"
        real.mkdir()
        (real / "copy.md").write_bytes(PAYLOAD)
        link = self.tmp / "linked_root"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                       check=True, capture_output=True, text=True)
        self.assertEqual(verify_backup({"path": "copy.md"}, DIGEST, link), "backup_not_independent")

    def test_hardlink_is_rejected(self):
        live = self.tmp / "live" / "AGENTS.md"
        live.parent.mkdir()
        live.write_bytes(PAYLOAD)
        try:
            os.link(live, self.root / "copy.md")
        except OSError:
            self.skipTest("hard links not supported")
        self.assertEqual(verify_backup({"path": "copy.md"}, DIGEST, self.root), "backup_not_independent")


if __name__ == "__main__":
    unittest.main()