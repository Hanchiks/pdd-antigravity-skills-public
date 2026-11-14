"""Independent 02AG1A1 activation-preflight regressions: no live I/O.

Run with PDD_STAGING_ROOT=/path/to/extracted/staging.
All fixtures and backup proofs are synthetic and confined to TemporaryDirectory.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

STAGING = Path(os.environ["PDD_STAGING_ROOT"]).resolve()
sys.path.insert(0, str(STAGING / "ops"))
from inventory import inventory
from transaction_plan import plan

TARGET = "~/.gemini/config/skills/pdd-operator/SKILL.md"
RULE = "~/.gemini/config/rules/AGENTS.md"


def import_preflight_collector():
    loc = STAGING / "preflight" / "collect.py"
    spec = importlib.util.spec_from_file_location("preflight_collector_for_test", loc)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ActivationPreflightTests(unittest.TestCase):
    def test_01_planner_must_not_deploy_from_unattested_prestate_even_with_backup(self):
        """No explicit ok=True/errors={} must never be interpreted as approved inventory."""
        src_manifest = json.loads((STAGING / "dry-run" / "DEPLOY-MANIFEST.json").read_text(encoding="utf-8"))
        unattested = json.loads((STAGING / "preflight" / "PRESTATE-SUMMARY.json").read_text(encoding="utf-8"))
        self.assertNotIn("ok", unattested)
        self.assertNotIn("errors", unattested)
        with tempfile.TemporaryDirectory() as td:
            fake_backup = Path(td) / "_backup-02ag1p2" / "AGENTS.md"
            fake_backup.parent.mkdir(parents=True)
            fake_backup.write_bytes((STAGING / "rule-migration" / "old-rule.redacted.md").read_bytes())
            outcome = plan(src_manifest, unattested, Path(td))
            self.assertFalse(outcome["deployable"],
                             "a synthetic backup must not turn a nonattested preflight summary into deployment authorization")
            self.assertEqual(outcome["status"], "not_deployable")

    def test_02_missing_config_root_is_inventory_error_not_successful_absence(self):
        with tempfile.TemporaryDirectory() as td:
            nonexistent_root = Path(td) / "config-does-not-exist"
            result = inventory(nonexistent_root, [TARGET])
            self.assertFalse(result.get("ok"),
                             "a missing configuration root must be an explicit inventory failure")
            self.assertIn("error", result["targets"][TARGET],
                          "cannot classify missing-root as a valid absent skill to create")

    def test_03_preflight_collector_must_identify_parent_symlink(self):
        """The reporting collector must not claim reparse=False if a parent redirects the path."""
        module = import_preflight_collector()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "config"
            parent = root / "skills"
            real = parent / "real"
            real.mkdir(parents=True)
            (real / "SKILL.md").write_text("synthetic test only", encoding="utf-8")
            link = parent / "pdd-operator"
            if sys.platform == "win32":
                outcome = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                                         capture_output=True, text=True)
                if outcome.returncode:
                    self.skipTest("Windows junction setup unavailable")
            else:
                try:
                    link.symlink_to(real, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("test environment forbids creating a directory symlink")
            entry = module.path_entry(link / "SKILL.md")
            self.assertTrue(entry.get("reparse") or entry.get("error"),
                            "reparse through a parent must never be reported as a normal file")


if __name__ == "__main__":
    unittest.main()
