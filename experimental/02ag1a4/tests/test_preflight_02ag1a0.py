"""Independent offline RED-before checks for Antigravity PDD deployment preflight.

Only synthetic temporary filesystem objects. No ADB, browser, device, or live config.
Requires PDD_STAGING_ROOT pointing to an extracted 02AG1P3 staging tree.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

root_str = os.environ.get('PDD_STAGING_ROOT')
if not root_str:
    raise RuntimeError('Set PDD_STAGING_ROOT to an extracted staging root')
ROOT = Path(root_str).resolve()
sys.path.insert(0, str(ROOT / 'ops'))
from transaction_plan import plan
from inventory import inventory

TARGET = '~/.gemini/config/skills/pdd-operator/SKILL.md'

class PreflightFailClosedTests(unittest.TestCase):
    def test_refuse_explicit_failed_prestate_even_when_action_target_looks_clean(self):
        manifest = {'actions': [{'action': 'create', 'target': TARGET, 'sha256': 'a'*64}]}
        prestate = {
            'ok': False,
            'errors': {'~/.gemini/config/rules/AGENTS.md': 'synthetic_unreadable'},
            'targets': {TARGET: {'exists': False, 'sha256': None}},
        }
        result = plan(manifest, prestate)
        self.assertFalse(result['deployable'], 'overall inventory failure must never yield deployable=true')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'not_deployable')

    def test_refuse_reparse_at_config_root_not_only_beneath_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / 'real-config'
            (real / 'skills' / 'pdd-operator').mkdir(parents=True)
            (real / 'skills' / 'pdd-operator' / 'SKILL.md').write_text('synthetic', encoding='utf-8')
            link = base / 'config'
            if sys.platform == 'win32':
                r = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)], capture_output=True, text=True)
                if r.returncode != 0:
                    self.skipTest('Windows junction setup unavailable')
            else:
                link.symlink_to(real, target_is_directory=True)
            entry = inventory(link, [TARGET])['targets'][TARGET]
            self.assertEqual(entry.get('error'), 'reparse_point', 'config root itself is a reparse point')

if __name__ == '__main__':
    unittest.main()
