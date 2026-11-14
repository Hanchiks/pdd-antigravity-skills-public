"""Independent, synthetic-only gates for 02AG1A3.

No phone, ADB, browser, live config, or private rule bytes are accessed.
Set PDD_STAGING_ROOT to an extracted 02AG1A2 staging root.
"""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(os.environ['PDD_STAGING_ROOT']).resolve()
sys.path.insert(0, str(ROOT / 'ops'))
from inventory import inventory
from transaction_plan import ALLOWED_TARGETS, RULE_TARGET, CANONICAL_ROOT, plan, verify_backup

KNOWN_LIVE_RULE_SHA256 = 'a8258a36ef3253f247f951789f86d1321aa4305c9ea3dde68561a4b7ecf0a4f9'
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def original_from_unified_diff(patch: Path, rel: str) -> bytes | None:
    """Reconstruct original lines from a diff hunk; never emit their contents."""
    if not patch.is_file():
        return None
    active = False
    hunk = False
    recovered = []
    for line in patch.read_text(encoding='utf-8').splitlines():
        if line.startswith('diff --git '):
            active = line.startswith('diff --git a/' + rel + ' ')
            hunk = False
            continue
        if not active:
            continue
        if line.startswith('@@ '):
            hunk = True
            continue
        if hunk and line[:1] in (' ', '-') and not line.startswith('--- '):
            recovered.append(line[1:])
    return ('\n'.join(recovered) + '\n').encode('utf-8') if recovered else None


class ReleaseSafetyTests(unittest.TestCase):
    def test_01_public_patches_do_not_reconstruct_private_rule(self):
        for patch in sorted(ROOT.rglob('*.patch')):
            with self.subTest(patch=patch.relative_to(ROOT).as_posix()):
                recovered = original_from_unified_diff(patch, 'rule-migration/old-rule.redacted.md')
                self.assertFalse(recovered and hashlib.sha256(recovered).hexdigest() == KNOWN_LIVE_RULE_SHA256,
                                 'PRIVATE_RULE_RECOVERABLE_FROM_DIFF: do not distribute this patch')

    def test_02_arbitrary_gemini_config_suffix_cannot_attest_live_root(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / 'unrelated-test-root' / '.gemini' / 'config'
            fake.mkdir(parents=True)
            targets = sorted(ALLOWED_TARGETS)
            snapshot = inventory(fake, targets, now=NOW)
            manifest = {
                'target_root': CANONICAL_ROOT,
                'actions': [
                    {'action': 'rule_replace' if target == RULE_TARGET else 'create',
                     'target': target, 'sha256': 'a' * 64}
                    for target in targets
                ],
            }
            outcome = plan(manifest, snapshot, now=NOW)
            self.assertFalse(outcome['deployable'],
                             'ARBITRARY_ROOT_ATTESTED: a fake config directory must not authorize deployment')

    def test_03_hardlink_is_not_independent_backup(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            live = base / 'synthetic-live' / '.gemini' / 'config' / 'rules' / 'AGENTS.md'
            live.parent.mkdir(parents=True)
            live.write_bytes(b'synthetic bytes, no user data')
            backup_root = base / 'backup-separate-path'
            backup_root.mkdir()
            backup = backup_root / 'AGENTS.md'
            try:
                os.link(live, backup)
            except OSError:
                self.skipTest('hard links not supported on this test volume')
            self.assertTrue(os.path.samefile(live, backup))
            outcome = verify_backup({'path': 'AGENTS.md'}, hashlib.sha256(live.read_bytes()).hexdigest(), backup_root)
            self.assertEqual(outcome, 'backup_not_independent',
                             'HARDLINK_ACCEPTED_AS_BACKUP: equal hashes do not prove independent storage')


if __name__ == '__main__':
    unittest.main()
