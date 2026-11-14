#!/usr/bin/env python3
"""Tests for the 02AG1A2 read-only inventory."""
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inventory import inventory, main as inventory_main
from transaction_plan import CANONICAL_ROOT

SKILL = "~/.gemini/config/skills/pdd-operator/SKILL.md"
RULE = "~/.gemini/config/rules/AGENTS.md"
NOW = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="02ag1a2-inv-"))
        self.root = self.tmp / ".gemini" / "config"
        self.root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def write_skill(self, content=b"hello"):
        p = self.root / "skills" / "pdd-operator" / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_absent_target_is_reported_absent(self):
        data = inventory(self.root, [SKILL], now=NOW)
        self.assertTrue(data["ok"])
        self.assertFalse(data["targets"][SKILL]["exists"])
        self.assertEqual(data["root"], str(self.root.resolve()))
        self.assertTrue(data["generated_at"].endswith("+00:00"))

    def test_present_target_has_matching_hash(self):
        self.write_skill(b"hello")
        data = inventory(self.root, [SKILL], now=NOW)
        self.assertEqual(data["targets"][SKILL]["sha256"], hashlib.sha256(b"hello").hexdigest())

    def test_rule_target_supported(self):
        data = inventory(self.root, [RULE], now=NOW)
        self.assertIn(RULE, data["targets"])

    def test_out_of_scope_target_is_error(self):
        data = inventory(self.root, ["~/.ssh/authorized_keys"], now=NOW)
        self.assertEqual(data["targets"]["~/.ssh/authorized_keys"]["error"], "target_out_of_scope")
        self.assertFalse(data["ok"])

    def test_inventory_does_not_create_or_modify(self):
        self.write_skill()
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        inventory(self.root, [SKILL, RULE], now=NOW)
        after = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_missing_root_is_an_error_not_absence(self):
        data = inventory(self.tmp / "does-not-exist", [SKILL], now=NOW)
        self.assertFalse(data["ok"])
        self.assertEqual(data["targets"][SKILL]["error"], "root_unavailable")

    @unittest.skipUnless(sys.platform == "win32", "junction test is Windows-only")
    def test_reparse_at_root_is_error(self):
        real = self.tmp / "real-config"
        real.mkdir()
        link = self.tmp / "linked-config"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                       check=True, capture_output=True, text=True)
        data = inventory(link, [SKILL], now=NOW)
        self.assertFalse(data["ok"])
        self.assertEqual(data["targets"][SKILL]["error"], "reparse_point")

    @unittest.skipUnless(sys.platform == "win32", "junction test is Windows-only")
    def test_reparse_in_ancestor_of_root_is_error(self):
        actual = self.tmp / "actual"
        (actual / "config").mkdir(parents=True)
        alias = self.tmp / "alias"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(actual)],
                       check=True, capture_output=True, text=True)
        data = inventory(alias / "config", [SKILL], now=NOW)
        self.assertFalse(data["ok"])
        self.assertEqual(data["targets"][SKILL]["error"], "reparse_point")

    def test_cli_reports_error_and_nonzero_exit(self):
        manifest = self.tmp / "manifest.json"
        manifest.write_text(json.dumps({"actions": [{"target": "~/.ssh/authorized_keys"}]}),
                            encoding="utf-8")
        output = self.tmp / "prestate.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = inventory_main(["--root", str(self.root), "--manifest", str(manifest),
                                 "--output", str(output), "--now", NOW.isoformat()])
        self.assertNotEqual(rc, 0)
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["root"], str(self.root.resolve()))


if __name__ == "__main__":
    unittest.main()
