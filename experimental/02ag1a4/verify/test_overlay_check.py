#!/usr/bin/env python3
"""Negative/positive tests for the 02AG1P1 verifier (offline copies only)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overlay_check import check

ROOT = Path(__file__).resolve().parents[1]
OP = "ANTIGRAVITY-OVERLAY/skills/pdd-operator/SKILL.md"
BUYER = "ANTIGRAVITY-OVERLAY/skills/pinduoduo-buyer/SKILL.md"
RULE = "antigravity/rules/PDD-SECTION.md"
MERGED = "rule-migration/merged-candidate.md"
MANIFEST = "dry-run/DEPLOY-MANIFEST.json"


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="02ag1p1-"))
        self.root = self.tmp / "pkg"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def codes(self):
        return {e["code"] for e in check(self.root)["errors"]}

    def edit(self, rel, old, new):
        p = self.root / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new), encoding="utf-8")

    def test_00_clean_package_passes(self):
        result = check(self.root)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["counts"]["files"], 25)

    def test_01_false_scratch_runtime_claim(self):
        self.edit(OP, "Второй копии рантайма (`…\\antigravity\\scratch\\pdd-operator`) не существует",
                  "Также доступна копия в `%USERPROFILE%\\.gemini\\antigravity\\scratch\\pdd-operator`")
        self.assertIn("false_scratch_runtime_path", self.codes())

    def test_02_wrong_buyer_skill_path(self):
        self.edit(OP, "~/.gemini/config/skills/pinduoduo-buyer/SKILL.md",
                  "%USERPROFILE%\\.codex\\skills\\pinduoduo-buyer\\SKILL.md")
        self.assertIn("wrong_buyer_skill_path", self.codes())

    def test_03_missing_gesture_prohibition_in_operator(self):
        self.edit(OP, "запрещено выполнять на устройстве `pdd.py scroll`",
                  "можно выполнять на устройстве `pdd.py scroll`")
        self.assertIn("missing_gesture_prohibition", self.codes())

    def test_04_missing_gesture_section_in_rule(self):
        p = self.root / RULE
        text = p.read_text(encoding="utf-8")
        start = text.index("## Жесты: запрет до приёмки")
        end = text.index("## Достоверность отчёта")
        p.write_text(text[:start] + text[end:], encoding="utf-8")
        self.assertIn("missing_gesture_prohibition", self.codes())

    def test_05_explicit_allowance_to_press_for_price(self):
        # Reproduces the audit finding: token presence alone must not pass.
        p = self.root / RULE
        p.write_text(p.read_text(encoding="utf-8") + "\nМожно нажать 直接拼成, чтобы узнать цену.\n",
                     encoding="utf-8")
        self.assertIn("allows_purchase_button_for_price", self.codes())

    def test_06_merged_candidate_conflict(self):
        p = self.root / MERGED
        p.write_text(p.read_text(encoding="utf-8") + "\nлишняя строка\n", encoding="utf-8")
        self.assertIn("merged_candidate_conflict", self.codes())

    def test_07_manifest_targets_original_hash(self):
        import hashlib
        man = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        original = (self.root / "ORIGINAL-02C2/skills/pdd-operator/SKILL.md").read_bytes()
        for action in man["actions"]:
            if action["target"].endswith("skills/pdd-operator/SKILL.md"):
                action["sha256"] = hashlib.sha256(original).hexdigest()
        (self.root / MANIFEST).write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        codes = self.codes()
        self.assertTrue({"manifest_target_mismatch", "manifest_targets_original"} & codes, codes)

    def test_08_rule_replace_without_backup_evidence(self):
        man = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        for action in man["actions"]:
            if action["action"] == "rule_replace":
                action["backup"] = {}
        (self.root / MANIFEST).write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertIn("backup_not_required", self.codes())

    def test_09_stale_token_in_merged_candidate(self):
        p = self.root / MERGED
        p.write_text(p.read_text(encoding="utf-8").replace(
            "# PDD rules — portable overlay", "# PDD rules — portable overlay (Gemini 3.8 Flash)"), encoding="utf-8")
        self.assertIn("stale_token", self.codes())

    def test_10_unexpected_adaptation(self):
        p = self.root / "ANTIGRAVITY-OVERLAY/skills/pinduoduo-buyer/references/decision-policy.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertIn("unexpected_adaptation", self.codes())

    def test_11_private_profile_unconditional(self):
        self.edit(BUYER, "если пользователь разрешил и файл существует, ", "")
        self.assertIn("private_profile_unconditional", self.codes())

    def test_12_missing_prestate(self):
        (self.root / "dry-run/PRESTATE.json").unlink()
        self.assertIn("missing_prestate", self.codes())



    def test_13_semantic_guard_rejects_negated_prohibition(self):
        p = self.root / RULE
        p.write_text(p.read_text(encoding="utf-8") + "\nНе запрещено нажать 直接拼成 ради проверки цены.\n",
                     encoding="utf-8")
        self.assertIn("allows_purchase_button_for_price", self.codes())

    def test_14_semantic_guard_rejects_imperative(self):
        p = self.root / RULE
        p.write_text(p.read_text(encoding="utf-8") + "\nНажимайте 直接拼成, чтобы проверить цену.\n",
                     encoding="utf-8")
        self.assertIn("allows_purchase_button_for_price", self.codes())

    def test_15_frontmatter_bom_is_rejected(self):
        p = self.root / OP
        p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
        codes = self.codes()
        self.assertTrue({"frontmatter_invalid", "manifest_target_mismatch"} & codes, codes)

    def test_16_frontmatter_missing_description(self):
        p = self.root / OP
        text = p.read_text(encoding="utf-8")
        p.write_text("\n".join(l for l in text.splitlines() if not l.startswith("description:")),
                     encoding="utf-8")
        self.assertIn("frontmatter_invalid", self.codes())

    def test_17_semantic_guard_applies_to_skill_text(self):
        p = self.root / OP
        p.write_text(p.read_text(encoding="utf-8") + "\nМожно нажать 直接拼成 ради цены.\n", encoding="utf-8")
        self.assertIn("allows_purchase_button_for_price", self.codes())

    def test_18_line_wrapped_permission_is_detected(self):
        p = self.root / RULE
        p.write_text(p.read_text(encoding="utf-8") + "\nцена:\nнажмите 直接拼成\n", encoding="utf-8")
        self.assertIn("allows_purchase_button_for_price", self.codes())

    def test_19_line_wrapped_permission_in_skill_is_detected(self):
        p = self.root / OP
        p.write_text(p.read_text(encoding="utf-8") + "\nцена:\nнажмите 直接拼成\n", encoding="utf-8")
        self.assertIn("allows_purchase_button_for_price", self.codes())
if __name__ == "__main__":
    unittest.main()


