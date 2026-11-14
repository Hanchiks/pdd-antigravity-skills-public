"""Independent 02AG1A2 negative probes. Synthetic temporary paths only.

Set PDD_STAGING_ROOT to an extracted 02AG1A1 staging directory.
No ADB, browser, phone, or active configuration access is performed.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ["PDD_STAGING_ROOT"]).resolve()
sys.path.insert(0, str(ROOT / "ops"))
from transaction_plan import plan
from inventory import main as inventory_main

SKILL = "~/.gemini/config/skills/pdd-operator/SKILL.md"


class ReleaseGateTests(unittest.TestCase):
    def test_01_partial_manifest_cannot_get_deployable(self):
        manifest = {"actions": [{"action": "create", "target": SKILL, "sha256": "a" * 64}]}
        prestate = {"ok": True, "errors": {}, "targets": {SKILL: {"exists": False, "sha256": None}}}
        observed = plan(manifest, prestate)
        self.assertFalse(observed["deployable"], "only 1/26 expected targets must not authorize installation")
        self.assertEqual(observed["status"], "not_deployable")

    def test_02_attestation_without_root_or_timestamp_cannot_deploy(self):
        manifest = json.loads((ROOT / "dry-run" / "DEPLOY-MANIFEST.json").read_text(encoding="utf-8"))
        targets = {a["target"]: {"exists": False, "sha256": None} for a in manifest["actions"]}
        prestate = {"ok": True, "errors": {}, "targets": targets}
        observed = plan(manifest, prestate)
        self.assertFalse(observed["deployable"],
                         "two booleans do not prove root identity or freshness of inventory")
        self.assertEqual(observed["status"], "not_deployable")

    def test_03_inventory_detects_symlink_in_ancestor_of_config_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            actual = base / "actual" / "config"
            actual.mkdir(parents=True)
            alias = base / "redirect"
            if sys.platform == "win32":
                outcome = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(base / "actual")],
                                         capture_output=True, text=True)
                if outcome.returncode:
                    self.skipTest("Windows junction creation unavailable")
            else:
                try:
                    alias.symlink_to(base / "actual", target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("directory symlinks unsupported in this environment")
            root = alias / "config"
            manifest_file = base / "synthetic-manifest.json"
            manifest_file.write_text(json.dumps({"actions": [{"target": SKILL}]}), encoding="utf-8")
            outfile = base / "synthetic-output.json"
            with contextlib.redirect_stdout(io.StringIO()):
                status = inventory_main(["--root", str(root), "--manifest", str(manifest_file),
                                         "--output", str(outfile)])
            data = json.loads(outfile.read_text(encoding="utf-8"))
            self.assertNotEqual(status, 0, "ancestor reparse should block inventory")
            self.assertFalse(data["ok"])
            self.assertEqual(data["targets"][SKILL]["error"], "reparse_point")


if __name__ == "__main__":
    unittest.main()
