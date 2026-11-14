#!/usr/bin/env python3
"""Tests for fail-closed rule migration (hash-gated, offline temp files)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_rule import KNOWN_SOURCE_SHA256, RuleMergeError, fingerprint, migrate, stale_claim_lines

BASE = Path(__file__).resolve().parent
OLD = (BASE / "old-rule.redacted.md").read_text(encoding="utf-8")
TARGET = (BASE.parent / "antigravity/rules/PDD-SECTION.md").read_text(encoding="utf-8")


class MigrateTests(unittest.TestCase):
    def test_00_fixture_hash_is_pinned(self):
        self.assertIn(fingerprint(OLD), KNOWN_SOURCE_SHA256)

    def test_01_approved_pdd_only_rule_is_replaced_exactly(self):
        self.assertEqual(migrate(OLD, TARGET), TARGET)

    def test_02_unrelated_h2_section_is_rejected(self):
        with self.assertRaises(RuleMergeError):
            migrate(OLD + "\n## Личные заметки\n\nчто-то ещё\n", TARGET)

    def test_03_unrelated_h1_section_is_rejected(self):
        with self.assertRaises(RuleMergeError):
            migrate(OLD + "\n# General system security rules\nKeep unrelated user policies intact.\n", TARGET)

    def test_04_unrelated_plain_paragraph_is_rejected(self):
        with self.assertRaises(RuleMergeError):
            migrate(OLD + "\nUnrelated global rule: do not delete local work projects.\n", TARGET)

    def test_05_unknown_changed_source_is_rejected(self):
        altered = OLD.replace("Pinduoduo", "Pinduoduo ", 1)
        self.assertNotEqual(fingerprint(altered), fingerprint(OLD))
        with self.assertRaises(RuleMergeError):
            migrate(altered, TARGET)

    def test_06_idempotent_only_with_explicit_approval(self):
        with self.assertRaises(RuleMergeError):
            migrate(TARGET, TARGET)
        self.assertEqual(migrate(TARGET, TARGET, {fingerprint(TARGET)}), TARGET)

    def test_07_target_without_required_control_is_rejected(self):
        with self.assertRaises(RuleMergeError):
            migrate(OLD, TARGET.replace("Жесты: запрет до приёмки", "Жесты"))

    def test_08_unnegated_stale_claim_is_rejected(self):
        with self.assertRaises(RuleMergeError):
            migrate(OLD, TARGET + "\nРаботает на Gemini 3.8 Flash.\n")

    def test_09_negated_scratch_warning_is_allowed(self):
        self.assertIn("не существует", TARGET)
        self.assertEqual(stale_claim_lines(TARGET), [])

    def test_10_non_pdd_file_is_rejected(self):
        with self.assertRaises(RuleMergeError):
            migrate("# Общие правила\n\nНичего про маркетплейс.\n", TARGET,
                    {fingerprint("# Общие правила\n\nНичего про маркетплейс.\n")})

    def test_11_cli_refuses_same_input_and_output(self):
        from migrate_rule import main
        self.assertEqual(main(["--current", str(BASE / "old-rule.redacted.md"),
                               "--target", str(BASE.parent / "antigravity/rules/PDD-SECTION.md"),
                               "--output", str(BASE / "old-rule.redacted.md")]), 2)



    def test_12_cli_refuses_live_config_output(self):
        import tempfile
        from pathlib import Path
        from migrate_rule import main as migrate_main
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / "staging" / "old.md"
            current.parent.mkdir(parents=True)
            current.write_bytes((BASE / "old-rule.redacted.md").read_bytes())
            live = root / ".gemini" / "config" / "rules" / "AGENTS.md"
            live.parent.mkdir(parents=True)
            live.write_text("DO NOT OVERWRITE", encoding="utf-8")
            rc = migrate_main(["--current", str(current),
                               "--target", str(BASE.parent / "antigravity/rules/PDD-SECTION.md"),
                               "--output", str(live)])
            self.assertNotEqual(rc, 0)
            self.assertEqual(live.read_text(encoding="utf-8"), "DO NOT OVERWRITE")

    def test_13_cli_refuses_output_outside_staging(self):
        import tempfile
        from pathlib import Path
        from migrate_rule import main as migrate_main
        with tempfile.TemporaryDirectory() as td:
            outside = Path(td) / "candidate.md"
            rc = migrate_main(["--current", str(BASE / "old-rule.redacted.md"),
                               "--target", str(BASE.parent / "antigravity/rules/PDD-SECTION.md"),
                               "--output", str(outside)])
            self.assertNotEqual(rc, 0)
            self.assertFalse(outside.exists())
if __name__ == "__main__":
    unittest.main()
