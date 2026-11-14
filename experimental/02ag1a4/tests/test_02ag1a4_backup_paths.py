"""Independent synthetic-only regression tests for 02AG1A4.

Run with PDD_STAGING_ROOT=<extracted 02AG1A3 staging directory>.
No ADB, browser, mobile device, live rule, live config, or user data is touched.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ["PDD_STAGING_ROOT"]).resolve()
sys.path.insert(0, str(ROOT / "ops"))
from transaction_plan import verify_backup  # noqa: E402

PAYLOAD = b"synthetic backup-scope fixture -- no user content\n"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
SCOPE_ERRORS = {"backup_out_of_scope", "backup_not_independent"}


class BackupScopeTests(unittest.TestCase):
    def test_01_symlink_or_junction_in_backup_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backup_root = base / "designated_backup_root"
            backup_root.mkdir()
            external = base / "external_storage"
            external.mkdir()
            (external / "AGENTS.md").write_bytes(PAYLOAD)
            link = backup_root / "redirect"
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(external)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    self.skipTest("Windows junction creation unavailable")
            else:
                try:
                    link.symlink_to(external, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("symlink creation unavailable")
            observed = verify_backup({"path": "redirect/AGENTS.md"}, DIGEST, backup_root)
            self.assertIn(observed, SCOPE_ERRORS,
                          "Backup must not resolve through parent link outside --backup-root")

    def test_02_windows_backslash_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backup_root = base / "designated_backup_root"
            backup_root.mkdir()
            (base / "outside.md").write_bytes(PAYLOAD)
            if os.name != "nt":
                # On POSIX backslash is a literal filename; verify that the
                # guard rejects unsafe Windows syntax before file access.
                (backup_root / "..\\outside.md").write_bytes(PAYLOAD)
            observed = verify_backup({"path": "..\\outside.md"}, DIGEST, backup_root)
            self.assertIn(observed, SCOPE_ERRORS,
                          "Windows backslash traversal must not escape backup root")

    def test_03_windows_drive_absolute_backup_path_is_rejected_as_scope(self):
        with tempfile.TemporaryDirectory() as td:
            backup_root = Path(td) / "designated_backup_root"
            backup_root.mkdir()
            observed = verify_backup({"path": r"C:\synthetic\copy.md"}, DIGEST, backup_root)
            self.assertIn(observed, SCOPE_ERRORS,
                          "Absolute Windows paths must fail scope validation before file access")

    def test_04_normal_independent_backup_remains_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            backup_root = Path(td) / "designated_backup_root"
            backup_root.mkdir()
            (backup_root / "copy.md").write_bytes(PAYLOAD)
            self.assertIsNone(verify_backup({"path": "copy.md"}, DIGEST, backup_root))


if __name__ == "__main__":
    unittest.main()
