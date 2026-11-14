#!/usr/bin/env python3
"""Tests for the 02AG1A2 release-gate planner (no live writes)."""
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transaction_plan import CANONICAL_ROOT, RULE_TARGET, allowlist
from transaction_plan import plan as _plan

CURRENT_ROOT = None
NOW = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)


def plan(manifest, prestate, backup_root=None, **kwargs):
    """Test shim: inject deterministic clock and the test's physical root."""
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("expected_root", CURRENT_ROOT)
    return _plan(manifest, prestate, backup_root, **kwargs)

SKILL = "~/.gemini/config/skills/pdd-operator/SKILL.md"
OK_SHA = "a" * 64
OLD_SHA = "b" * 64
def full_manifest(*, rule_sha=OK_SHA, skill_sha=OK_SHA, backup=None):
    actions = [{"action": "create", "target": t, "sha256": skill_sha}
               for t in sorted(allowlist()) if t != RULE_TARGET]
    actions.append({"action": "rule_replace", "target": RULE_TARGET, "sha256": rule_sha,
                    "backup": backup or {"path": "backup/AGENTS.md", "sha256": OLD_SHA}})
    return {"target_root": CANONICAL_ROOT, "actions": actions}


def attested(targets, *, root=None, generated_at=None, ok=True, errors=None):
    if root is None:
        root = str(Path(CURRENT_ROOT).resolve()) if CURRENT_ROOT is not None else CANONICAL_ROOT
    return {"ok": ok, "errors": {} if errors is None else errors, "root": root,
            "generated_at": (generated_at or NOW.isoformat()), "targets": targets}


def absent_targets():
    return {t: {"exists": False, "sha256": None} for t in allowlist()}


class PlanTests(unittest.TestCase):
    def setUp(self):
        global CURRENT_ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="02ag1a2-plan-"))
        self.config = self.tmp / "home" / ".gemini" / "config"
        self.config.mkdir(parents=True)
        CURRENT_ROOT = self.config
        self.backups = self.tmp / "backups"
        self.backups.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def make_backup(self, content=b"old-rule", name="AGENTS.md"):
        p = self.backups / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}

    def test_01_full_create_plan_is_deployable(self):
        out = plan(full_manifest(), attested(absent_targets()), now=NOW)
        self.assertTrue(out["ok"], out["errors"])
        self.assertEqual(out["status"], "dry_run_ok")
        self.assertEqual(out["counts"]["create"], 26)

    def test_02_full_skip_plan_is_deployable(self):
        targets = {t: {"exists": True, "sha256": OK_SHA} for t in allowlist()}
        out = plan(full_manifest(), attested(targets), now=NOW)
        self.assertTrue(out["ok"], out["errors"])
        self.assertEqual(out["counts"]["skip"], 26)

    def test_03_full_replace_with_real_backup(self):
        backup = self.make_backup()
        targets = absent_targets()
        targets[RULE_TARGET] = {"exists": True, "sha256": backup["sha256"]}
        out = plan(full_manifest(backup=backup), attested(targets), self.backups, now=NOW)
        self.assertTrue(out["ok"], out["errors"])
        self.assertEqual(out["counts"]["replace"], 1)

    def test_04_replace_without_backup_root_is_pending(self):
        targets = absent_targets()
        targets[RULE_TARGET] = {"exists": True, "sha256": OLD_SHA}
        out = plan(full_manifest(), attested(targets), now=NOW)
        self.assertIn("backup_pending", [e["code"] for e in out["errors"]])
        self.assertEqual(out["status"], "not_deployable")

    def test_05_missing_backup_file_fails(self):
        targets = absent_targets()
        targets[RULE_TARGET] = {"exists": True, "sha256": OLD_SHA}
        out = plan(full_manifest(), attested(targets), self.backups, now=NOW)
        self.assertIn("backup_missing", [e["code"] for e in out["errors"]])

    def test_06_backup_hash_mismatch_fails(self):
        backup = self.make_backup(b"different")
        targets = absent_targets()
        targets[RULE_TARGET] = {"exists": True, "sha256": OLD_SHA}
        out = plan(full_manifest(backup=backup), attested(targets), self.backups, now=NOW)
        self.assertIn("backup_hash_mismatch", [e["code"] for e in out["errors"]])

    def test_07_backup_inside_live_config_is_not_independent(self):
        live = self.tmp / ".gemini" / "config"
        live.mkdir(parents=True)
        (live / "AGENTS.md").write_bytes(b"live")
        targets = absent_targets()
        targets[RULE_TARGET] = {"exists": True, "sha256": OLD_SHA}
        out = plan(full_manifest(), attested(targets), live, now=NOW)
        self.assertIn("backup_not_independent", [e["code"] for e in out["errors"]])

    def test_08_partial_manifest_is_incomplete(self):
        manifest = {"target_root": CANONICAL_ROOT,
                    "actions": [{"action": "create", "target": SKILL, "sha256": OK_SHA}]}
        out = plan(manifest, attested({SKILL: {"exists": False, "sha256": None}}), now=NOW)
        self.assertFalse(out["deployable"])
        self.assertIn("manifest_incomplete", [e["code"] for e in out["errors"]])

    def test_09_incomplete_prestate_is_rejected(self):
        out = plan(full_manifest(), attested({SKILL: {"exists": False, "sha256": None}}), now=NOW)
        self.assertFalse(out["deployable"])
        self.assertIn("prestate_incomplete", [e["code"] for e in out["errors"]])

    def test_10_wrong_manifest_root_is_rejected(self):
        manifest = full_manifest()
        manifest["target_root"] = "~/.codex/config"
        out = plan(manifest, attested(absent_targets()), now=NOW)
        self.assertIn("manifest_root_mismatch", [e["code"] for e in out["errors"]])

    def test_11_root_identity_mismatch_is_rejected(self):
        out = plan(full_manifest(), attested(absent_targets(), root="/elsewhere"), now=NOW)
        self.assertIn("prestate_root_mismatch", [e["code"] for e in out["errors"]])

    def test_12_missing_timestamp_is_rejected(self):
        pre = attested(absent_targets())
        pre.pop("generated_at")
        self.assertIn("prestate_timestamp_missing", [e["code"] for e in plan(full_manifest(), pre, now=NOW)["errors"]])

    def test_13_naive_timestamp_is_rejected(self):
        pre = attested(absent_targets(), generated_at="2026-11-14T00:00:00")
        self.assertIn("prestate_timestamp_missing", [e["code"] for e in plan(full_manifest(), pre, now=NOW)["errors"]])

    def test_14_stale_timestamp_is_rejected(self):
        pre = attested(absent_targets(), generated_at=(NOW - timedelta(hours=1)).isoformat())
        self.assertIn("prestate_stale", [e["code"] for e in plan(full_manifest(), pre, now=NOW)["errors"]])

    def test_15_future_timestamp_is_rejected(self):
        pre = attested(absent_targets(), generated_at=(NOW + timedelta(hours=1)).isoformat())
        self.assertIn("prestate_timestamp_future", [e["code"] for e in plan(full_manifest(), pre, now=NOW)["errors"]])

    def test_16_ok_false_is_rejected(self):
        out = plan(full_manifest(), attested(absent_targets(), ok=False), now=NOW)
        self.assertIn("prestate_not_attested", [e["code"] for e in out["errors"]])

    def test_17_non_empty_errors_is_rejected(self):
        out = plan(full_manifest(), attested(absent_targets(), errors={SKILL: "x"}), now=NOW)
        self.assertIn("prestate_errors_invalid", [e["code"] for e in out["errors"]])

    def test_18_errors_must_be_a_mapping(self):
        out = plan(full_manifest(), attested(absent_targets(), errors=[]), now=NOW)
        self.assertIn("prestate_errors_invalid", [e["code"] for e in out["errors"]])

    def test_19_out_of_scope_target_fails(self):
        target = "~/.ssh/authorized_keys"
        manifest = {"target_root": CANONICAL_ROOT,
                    "actions": [{"action": "rule_replace", "target": target, "sha256": OK_SHA}]}
        out = plan(manifest, attested({target: {"exists": True, "sha256": OLD_SHA}}), now=NOW)
        self.assertIn("target_out_of_scope", [e["code"] for e in out["errors"]])

    def test_20_path_traversal_fails(self):
        target = "~/.gemini/config/skills/../../.ssh/authorized_keys"
        manifest = {"target_root": CANONICAL_ROOT,
                    "actions": [{"action": "create", "target": target, "sha256": OK_SHA}]}
        out = plan(manifest, attested({target: {"exists": False, "sha256": None}}), now=NOW)
        self.assertIn("path_traversal", [e["code"] for e in out["errors"]])

    def test_21_alternate_separator_fails(self):
        target = "~/.gemini/config/skills/pdd-operator\\SKILL.md"
        manifest = {"target_root": CANONICAL_ROOT,
                    "actions": [{"action": "create", "target": target, "sha256": OK_SHA}]}
        out = plan(manifest, attested({target: {"exists": False, "sha256": None}}), now=NOW)
        self.assertIn("alternate_separator", [e["code"] for e in out["errors"]])

    def test_22_duplicate_target_fails(self):
        manifest = full_manifest()
        manifest["actions"].append({"action": "create", "target": SKILL, "sha256": OK_SHA})
        out = plan(manifest, attested(absent_targets()), now=NOW)
        self.assertIn("duplicate_target", [e["code"] for e in out["errors"]])

    def test_23_prestate_error_fails(self):
        targets = absent_targets()
        targets[RULE_TARGET] = {"exists": True, "sha256": None, "error": "reparse_point"}
        out = plan(full_manifest(), attested(targets), now=NOW)
        self.assertIn("prestate_error", [e["code"] for e in out["errors"]])

    def test_24_missing_hash_for_existing_target_fails(self):
        targets = absent_targets()
        targets[SKILL] = {"exists": True, "sha256": None}
        out = plan(full_manifest(), attested(targets), now=NOW)
        self.assertIn("prestate_hash_missing", [e["code"] for e in out["errors"]])

    def test_25_malformed_sha_fails(self):
        manifest = full_manifest()
        manifest["actions"][0]["sha256"] = "xyz"
        out = plan(manifest, attested(absent_targets()), now=NOW)
        self.assertIn("malformed_sha256", [e["code"] for e in out["errors"]])

    def test_26_unknown_action_fails(self):
        manifest = full_manifest()
        manifest["actions"][0]["action"] = "delete"
        out = plan(manifest, attested(absent_targets()), now=NOW)
        self.assertIn("forbidden_action", [e["code"] for e in out["errors"]])

    def test_27_rule_replace_on_skill_target_fails(self):
        manifest = full_manifest()
        for action in manifest["actions"]:
            if action["target"] == SKILL:
                action["action"] = "rule_replace"
        out = plan(manifest, attested(absent_targets()), now=NOW)
        self.assertIn("rule_target_mismatch", [e["code"] for e in out["errors"]])

    def test_28_plan_never_contains_delete(self):
        out = plan(full_manifest(), attested(absent_targets()), now=NOW)
        self.assertTrue(all(o["op"] in {"create", "replace", "skip"} for o in out["ops"]))

    def test_29_missing_prestate_block_fails(self):
        out = plan(full_manifest(), {}, now=NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "not_deployable")

    def test_30_blocked_candidate_hash_is_rejected(self):
        manifest = full_manifest()
        manifest["actions"][0]["sha256"] = "0666ee5ce9227fe3ce2c45877d757df562dcee5b6b329323d7f9d4db99ec2792"
        out = plan(manifest, attested(absent_targets()), now=NOW)
        self.assertIn("blocked_candidate_hash", [e["code"] for e in out["errors"]])


if __name__ == "__main__":
    unittest.main()
