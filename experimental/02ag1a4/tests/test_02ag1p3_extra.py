"""Independent offline test-only checks. Run with PDD_STAGING_ROOT and PDD_PREVIOUS_STAGING."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ['PDD_STAGING_ROOT']).resolve()
PREV = Path(os.environ['PDD_PREVIOUS_STAGING']).resolve()
for sub in ('verify', 'ops', 'rule-migration'):
    sys.path.insert(0, str(ROOT / sub))
from inventory import inventory, main as inventory_main
from migrate_rule import main as migrate_main
from overlay_check import press_allowances

SKILL_TARGET = '~/.gemini/config/skills/pdd-operator/SKILL.md'

class DeploymentExtraTests(unittest.TestCase):
    def test_01_parent_reparse_inside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'config'
            actual = root / 'skills' / 'actual'
            actual.mkdir(parents=True)
            (actual / 'SKILL.md').write_text('synthetic', encoding='utf-8')
            link = root / 'skills' / 'pdd-operator'
            if sys.platform == 'win32':
                p = subprocess.run(['cmd','/c','mklink','/J',str(link),str(actual)], capture_output=True, text=True)
                if p.returncode:
                    self.skipTest('Windows mklink /J unavailable')
            else:
                link.symlink_to(actual, target_is_directory=True)
            entry = inventory(root, [SKILL_TARGET])['targets'][SKILL_TARGET]
            self.assertEqual(entry.get('error'), 'reparse_point', entry)

    def test_02_inventory_cli_reports_errors_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            manifest=root/'manifest.json'
            manifest.write_text(json.dumps({'actions':[{'target':'~/.ssh/authorized_keys'}]}), encoding='utf-8')
            output=root/'prestate.json'
            with contextlib.redirect_stdout(io.StringIO()):
                rc=inventory_main(['--root',str(root/'config'),'--manifest',str(manifest),'--output',str(output)])
            self.assertNotEqual(rc,0, 'inventory errors must not have success exit code')
            self.assertFalse(json.loads(output.read_text())['ok'], 'inventory must report ok=false')

    def test_03_migration_cannot_write_to_active_config_path(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            current=root/'staging'/'old-rule.md'
            current.parent.mkdir(parents=True)
            current.write_bytes((ROOT/'rule-migration'/'old-rule.redacted.md').read_bytes())
            live=root/'.gemini'/'config'/'rules'/'AGENTS.md'
            live.parent.mkdir(parents=True)
            live.write_text('DO NOT OVERWRITE', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                rc=migrate_main(['--current',str(current),
                                 '--target',str(ROOT/'antigravity'/'rules'/'PDD-SECTION.md'),
                                 '--output',str(live)])
            self.assertNotEqual(rc,0, 'a generator must refuse a live config output path')
            self.assertEqual(live.read_text(encoding='utf-8'), 'DO NOT OVERWRITE')

    def test_04_line_wrapped_permission_for_purchase_button_is_rejected(self):
        policy=json.loads((ROOT/'verify'/'policy.json').read_text(encoding='utf-8-sig'))
        text='\u0446\u0435\u043d\u0430:\n\u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u76f4\u63a5\u62fc\u6210'
        self.assertTrue(press_allowances(text,policy), 'cross-line permission cannot evade detection')

    def test_05_rollback_inventory_example_is_executable(self):
        doc=(ROOT/'ROLLBACK.md').read_text(encoding='utf-8-sig')
        commands=[line.strip() for line in doc.splitlines() if line.strip().startswith('python ops\\inventory.py')]
        self.assertTrue(commands, 'no inventory CLI example')
        self.assertTrue(all('--manifest' in line for line in commands), 'required --manifest missing')

    def test_06_patch_applies_to_original_with_standard_git_apply(self):
        if not shutil.which('git'):
            self.skipTest('git is unavailable')
        patch=ROOT/'diff'/'02ag1p1-to-02ag1p2.patch'
        p=subprocess.run(['git','-c','core.autocrlf=false','apply','--check','--whitespace=nowarn',str(patch)],
                         cwd=PREV,capture_output=True,text=True)
        self.assertEqual(p.returncode,0, p.stderr[:1400])

if __name__ == '__main__': unittest.main()
