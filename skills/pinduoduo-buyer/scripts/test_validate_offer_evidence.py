"""Synthetic tests: the audit never establishes authenticity of evidence."""
import copy
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import validate_offer_evidence
from validate_offer_evidence import audit_candidate, audit_session, main


def candidate():
    return {
        'id': 'A', 'title_zh': '样品', 'store_name': '示例店', 'sku_zh': '蓝色 XL',
        'product_id': '123', 'available': True, 'quantity': 1, 'currency': 'CNY',
        'observed_at': '2026-11-12T12:00:00+08:00', 'gate': 'eligible',
        'costs': {
            'goods': {'amount': 30, 'certainty': 'observed', 'source': 'sku-screen-1'},
            'delivery_cn': {'amount': 0, 'certainty': 'observed', 'source': 'delivery-screen-1'},
            'payment': {'amount': 0, 'certainty': 'not_applicable', 'source': 'no payment fee'},
        },
        'offer_evidence': {
            'listing_ref': '123', 'sku_zh': '蓝色 XL', 'store_name': '示例店', 'quantity': 1,
            'currency': 'CNY', 'price_cny': 30, 'observed_at': '2026-11-12T12:03:00+08:00',
            'source': 'sku-screen-1', 'delivery_destination': 'saved_cn_warehouse',
            'discount': {'kind': 'conditional', 'terms': '首件限1件', 'applies': True,
                         'ordinary_price_cny': 40, 'source': 'sku-screen-1'},
        },
    }


def codes(value):
    return {issue['code'] for issue in audit_candidate(value)['issues']}


class OfferEvidenceTests(unittest.TestCase):
    def test_consistent_record_is_ready_but_not_verified(self):
        data = {'candidates': [candidate()]}
        original = copy.deepcopy(data)
        out = audit_session(data)
        self.assertTrue(out['candidates'][0]['ready_for_unqualified_price_claim'])
        self.assertIn('не подтверждает', out['scope'])
        self.assertEqual(data, original)

    def test_missing_legacy_record_is_reported_not_crashed(self):
        item = candidate(); del item['offer_evidence']
        self.assertIn('missing_offer_evidence', codes(item))

    def test_price_and_source_must_match_selected_offer(self):
        item = candidate(); item['costs']['goods']['amount'] = 25
        item['costs']['goods']['source'] = 'other-sku-screen'
        self.assertTrue({'mismatch_price', 'mismatch_price_source'} <= codes(item))

    def test_mismatched_sku_shop_listing_and_quantity_are_reported(self):
        item = candidate(); offer = item['offer_evidence']
        offer.update(sku_zh='蓝色 M', store_name='另一家店', listing_ref='456', quantity=2)
        self.assertTrue({'mismatch_sku_zh', 'mismatch_store_name', 'mismatch_listing', 'mismatch_quantity'} <= codes(item))

    def test_discount_requires_terms_eligibility_and_ordinary_price(self):
        item = candidate(); item['offer_evidence']['discount'].update(terms='', applies=None, ordinary_price_cny=None)
        self.assertTrue({'discount_terms_missing', 'discount_not_confirmed', 'ordinary_price_unknown'} <= codes(item))

    def test_discount_price_conflict(self):
        item = candidate(); item['offer_evidence']['discount']['ordinary_price_cny'] = 20
        self.assertIn('discount_price_conflict', codes(item))

    def test_unknown_shipping_is_not_free_and_estimate_is_marked(self):
        item = candidate(); item['costs']['delivery_cn'] = {'amount': None, 'certainty': 'unknown'}
        self.assertIn('delivery_unknown', codes(item))
        item['costs']['delivery_cn'] = {'amount': 0, 'certainty': 'unknown'}
        self.assertIn('delivery_conflict', codes(item))
        item['costs']['delivery_cn'] = {'amount': 5, 'certainty': 'estimated', 'source': 'estimate'}
        self.assertIn('delivery_estimated', codes(item))

    def test_shipping_must_refer_to_saved_warehouse_without_address(self):
        item = candidate(); item['offer_evidence']['delivery_destination'] = None
        self.assertIn('delivery_destination_unconfirmed', codes(item))

    def test_unknown_payment_is_not_zero(self):
        item = candidate(); item['costs']['payment'] = {'amount': None, 'certainty': 'unknown'}
        self.assertIn('payment_unknown', codes(item))

    def test_no_discount_cannot_coexist_with_recorded_discount_conditions(self):
        item = candidate(); item['offer_evidence']['discount'] = {'kind': 'none', 'source': 'sku-screen-1'}
        item['discount_conditions'] = ['首件']
        self.assertIn('discount_conditions_conflict', codes(item))

    def test_unavailable_and_unknown_discount_never_ready(self):
        item = candidate(); item['available'] = False; item['offer_evidence']['discount']['kind'] = 'unknown'
        self.assertTrue({'availability_unconfirmed', 'discount_unknown'} <= codes(item))
        self.assertFalse(audit_candidate(item)['ready_for_unqualified_price_claim'])

    def test_bad_data_rejected(self):
        with self.assertRaises(ValueError): audit_session({'candidates': [None]})


@contextlib.contextmanager
def session_file(raw):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'session.json'
        path.write_text(raw, encoding='utf-8')
        yield str(path)


def run_cli(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def variant(cid, gate=None, broken=False, legacy=False):
    """A candidate copy: renamed, optionally with a price mismatch or no evidence."""
    item = candidate()
    item['id'] = cid
    if gate is not None:
        item['gate'] = gate
    if legacy:
        del item['offer_evidence']
    if broken:
        item['offer_evidence']['price_cny'] = 25  # differs from costs.goods 30
    return item


class StrictModeTests(unittest.TestCase):
    def test_named_finalist_passes_and_legacy_records_do_not_block(self):
        session = {'candidates': [variant('A'), variant('B', 'conditional', legacy=True),
                                  variant('C', 'rejected', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', 'A'])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(report['strict_passed'])
        self.assertEqual(report['strict_selected_ids'], ['A'])
        self.assertEqual({row['id']: row['strict_selected'] for row in report['candidates']},
                         {'A': True, 'B': False, 'C': False})
        self.assertEqual(report['strict_problems'], [])
        self.assertEqual(set(report['strict_unselected_with_issues']), {'B', 'C'})

    def test_strict_without_ids_is_invalid_input(self):
        # No automatic eligible selection: bare --strict is invalid input (exit 2, no JSON).
        sessions = ({'candidates': [variant('A', 'eligible'), variant('B', 'conditional', legacy=True)]},
                    {'candidates': [variant('B', 'conditional', legacy=True)]})
        for session in sessions:
            eligible_present = any(c.get('gate') == 'eligible' for c in session['candidates'])
            with self.subTest(eligible_present=eligible_present):
                with session_file(json.dumps(session, ensure_ascii=False)) as path:
                    code, out = run_cli([path, '--strict'])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')

    def test_selected_problem_gives_exit_one(self):
        session = {'candidates': [variant('A', broken=True), variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', 'A'])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(report['strict_passed'])
        self.assertEqual([row['id'] for row in report['strict_problems']], ['A'])
        self.assertIn('mismatch_price', [i['code'] for i in report['strict_problems'][0]['issues']])
        self.assertNotIn('B', report['strict_selected_ids'])

    def test_named_conditional_is_checked_strictly(self):
        session = {'candidates': [variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, _ = run_cli([path, '--strict', '--candidate-id', 'B'])
        self.assertEqual(code, 1)

    def test_duplicate_requested_id_is_invalid_input(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', 'A', '--candidate-id', 'A'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')

    def test_ambiguous_duplicate_record_id_is_invalid_input(self):
        session = {'candidates': [variant('A'), variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', 'A'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')

    def test_empty_requested_id_is_invalid_input(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', '   '])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')

    def test_multiple_selected_one_broken_exit_one(self):
        session = {'candidates': [variant('A'), variant('B', broken=True),
                                  variant('C', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', 'A', '--candidate-id', 'B'])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(report['strict_selected_ids'], ['A', 'B'])
        self.assertEqual([row['id'] for row in report['strict_problems']], ['B'])
        self.assertEqual(report['strict_unselected_with_issues'], ['C'])

    def test_unknown_candidate_id_is_invalid_input(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, _ = run_cli([path, '--strict', '--candidate-id', 'NOPE'])
        self.assertEqual(code, 2)

    def test_candidate_id_without_strict_is_invalid_input(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            with self.assertRaises(SystemExit) as ctx:
                run_cli([path, '--candidate-id', 'A'])
        self.assertEqual(ctx.exception.code, 2)

    def test_strict_invalid_inputs_exit_two(self):
        # File-level invalid input must still exit 2 inside a valid strict selection.
        with session_file('{') as path:
            with self.subTest('broken json'):
                self.assertEqual(run_cli([path, '--strict', '--candidate-id', 'A'])[0], 2)
        with session_file('{"candidates": 5}') as path:
            with self.subTest('candidates not a list'):
                self.assertEqual(run_cli([path, '--strict', '--candidate-id', 'A'])[0], 2)
        with session_file('{"candidates": [5]}') as path:
            with self.subTest('candidate not an object'):
                self.assertEqual(run_cli([path, '--strict', '--candidate-id', 'A'])[0], 2)

    def test_missing_file_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / 'no-such-session.json')
            code, _ = run_cli([missing, '--strict'])
        self.assertEqual(code, 2)

    def test_normal_mode_exit_zero_with_full_report(self):
        session = {'candidates': [variant('A', broken=True), variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(set(report), {'candidates', 'scope'})
        self.assertNotIn('strict_selected', report['candidates'][0])
        self.assertFalse(report['candidates'][0]['ready_for_unqualified_price_claim'])
        self.assertTrue(report['candidates'][0]['issues'])

    def test_strict_scope_still_disclaims_verification(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out = run_cli([path, '--strict', '--candidate-id', 'A'])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn('не подтверждает', report['scope'])
        self.assertIn('Строгий режим', report['scope'])


SCRIPT_PATH = Path(validate_offer_evidence.__file__)


def run_cli_subprocess(argv):
    """Run the real CLI as a subprocess with deterministic UTF-8 streams."""
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    proc = subprocess.run([sys.executable, str(SCRIPT_PATH), *argv],
                          capture_output=True, text=True, encoding='utf-8', env=env)
    return proc.returncode, proc.stdout, proc.stderr


class StrictCliTests(unittest.TestCase):
    """Real CLI subprocess runs: shell exit codes, stdout JSON, stderr diagnostics."""

    def test_clean_selected_finalist_exits_zero(self):
        session = {'candidates': [variant('A'), variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A'])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertTrue(report['strict_passed'])
        self.assertEqual(report['strict_selected_ids'], ['A'])

    def test_broken_selected_finalist_exits_one(self):
        session = {'candidates': [variant('A', broken=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A'])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(err, '')
        self.assertFalse(report['strict_passed'])
        self.assertEqual([row['id'] for row in report['strict_problems']], ['A'])

    def test_unselected_broken_record_does_not_block_exit_zero(self):
        session = {'candidates': [variant('A'), variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A'])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertTrue(report['strict_passed'])
        self.assertEqual(report['strict_unselected_with_issues'], ['B'])
        self.assertTrue(report['candidates'][1]['issues'])

    def test_unknown_id_exits_two(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'NOPE'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('неизвестный --candidate-id', err)

    def test_strict_without_id_with_eligible_exits_two(self):
        session = {'candidates': [variant('A', 'eligible')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('--candidate-id', err)
        self.assertNotIn('"strict_passed": true', out)

    def test_strict_without_id_without_eligible_exits_two(self):
        session = {'candidates': [variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('--candidate-id', err)

    def test_candidate_id_without_strict_exits_two(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--candidate-id', 'A'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('--candidate-id требует --strict', err)

    def test_duplicate_id_args_exits_two(self):
        session = {'candidates': [variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A', '--candidate-id', 'A'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('дубликат --candidate-id', err)

    def test_ambiguous_duplicate_record_id_exits_two(self):
        session = {'candidates': [variant('A'), variant('A')]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('неоднозначный --candidate-id', err)

    def test_multiple_selected_one_broken_exits_one(self):
        session = {'candidates': [variant('A'), variant('B', broken=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A', '--candidate-id', 'B'])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(err, '')
        self.assertEqual(report['strict_selected_ids'], ['A', 'B'])
        self.assertEqual([row['id'] for row in report['strict_problems']], ['B'])

    def test_broken_json_exits_two(self):
        with session_file('{') as path:
            code, out, err = run_cli_subprocess([path, '--strict', '--candidate-id', 'A'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertTrue(err)

    def test_normal_mode_with_issues_exits_zero(self):
        session = {'candidates': [variant('A', broken=True), variant('B', 'conditional', legacy=True)]}
        with session_file(json.dumps(session, ensure_ascii=False)) as path:
            code, out, err = run_cli_subprocess([path])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertEqual(set(report), {'candidates', 'scope'})
        self.assertFalse(report['candidates'][0]['ready_for_unqualified_price_claim'])


if __name__ == '__main__':
    unittest.main()
