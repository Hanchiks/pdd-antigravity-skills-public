"""Synthetic unit tests; no real products or phone access."""
import copy
import unittest
from compare_prices import COMPONENTS, compare


def candidate(cid='A', goods=60, intl=10):
    costs = {key: {'amount': 0, 'certainty': 'not_applicable', 'source': 'synthetic test'} for key in COMPONENTS}
    costs['goods'] = {'amount': goods, 'certainty': 'observed', 'source': 'synthetic test'}
    costs['delivery_intl'] = {'amount': intl, 'certainty': 'estimated', 'source': 'synthetic test'}
    return {'id': cid, 'comparison_group': 'same-use-one-item', 'quantity': 1,
            'currency': 'CNY', 'gate': 'eligible', 'available': True, 'costs': costs}


class PriceTests(unittest.TestCase):
    def test_known_total(self):
        self.assertEqual(compare({'candidates': [candidate()]})['candidates'][0]['total_cny'], 70)

    def test_china_excludes_intl(self):
        self.assertEqual(compare({'candidates': [candidate()]}, 'china')['candidates'][0]['total_cny'], 60)

    def test_missing_cost_is_not_zero(self):
        c = candidate(); c['costs']['delivery_intl'] = {'amount': None, 'certainty': 'unknown', 'source': ''}
        out = compare({'candidates': [c]})
        self.assertIsNone(out['candidates'][0]['total_cny'])
        self.assertEqual(out['candidates'][0]['known_subtotal_cny'], 60)
        self.assertEqual(out['groups'][0]['eligible_with_unknown_cost_or_availability_ids'], ['A'])
        self.assertEqual(out['groups'][0]['lowest_among_fully_costed_eligible_ids'], [])

    def test_unknown_intl_does_not_block_china(self):
        c = candidate(); c['costs']['delivery_intl'] = {'amount': None, 'certainty': 'unknown'}
        self.assertEqual(compare({'candidates': [c]}, 'china')['candidates'][0]['total_cny'], 60)

    def test_savings(self):
        out = compare({'candidates': [candidate('A', 60, 0), candidate('B', 42, 0)], 'reference_id': 'A'})
        self.assertEqual(out['candidates'][1]['savings_vs_reference_cny'], 18)
        self.assertEqual(out['candidates'][1]['savings_vs_reference_percent'], 30)

    def test_cheaper_item_loses_after_shipping(self):
        out = compare({'candidates': [candidate('A', 60, 10), candidate('B', 42, 50)]})
        self.assertEqual(out['groups'][0]['lowest_among_fully_costed_eligible_ids'], ['A'])

    def test_conditional_cannot_win(self):
        c = candidate('B', 1, 0); c['gate'] = 'conditional'
        out = compare({'candidates': [candidate(), c]})
        self.assertEqual(out['groups'][0]['lowest_among_fully_costed_eligible_ids'], ['A'])

    def test_unavailable_cannot_win(self):
        c = candidate('B', 1, 0); c['available'] = False
        out = compare({'candidates': [candidate(), c]})
        self.assertEqual(out['groups'][0]['lowest_among_fully_costed_eligible_ids'], ['A'])

    def test_unknown_stock_cannot_win(self):
        c = candidate(); c['available'] = None
        self.assertEqual(compare({'candidates': [c]})['groups'][0]['lowest_among_fully_costed_eligible_ids'], [])

    def test_rejected_cannot_win(self):
        c = candidate('B', 0, 0); c['gate'] = 'rejected'
        self.assertEqual(compare({'candidates': [candidate(), c]})['groups'][0]['lowest_among_fully_costed_eligible_ids'], ['A'])

    def test_different_quantity_not_compared(self):
        c = candidate('B', 42, 0); c['quantity'] = 2
        out = compare({'candidates': [candidate(), c], 'reference_id': 'A'})
        self.assertEqual(len(out['groups']), 2)
        self.assertIsNone(out['candidates'][1]['savings_vs_reference_percent'])

    def test_different_group_not_compared(self):
        c = candidate('B', 42, 0); c['comparison_group'] = 'different-use'
        out = compare({'candidates': [candidate(), c], 'reference_id': 'A'})
        self.assertIsNone(out['candidates'][1]['savings_vs_reference_cny'])

    def test_estimated_flag(self):
        self.assertTrue(compare({'candidates': [candidate()]})['candidates'][0]['contains_estimate'])

    def test_zero_reference_no_division(self):
        out = compare({'candidates': [candidate('A', 0, 0)], 'reference_id': 'A'})
        self.assertIsNone(out['candidates'][0]['savings_vs_reference_percent'])

    def test_decimal_addition(self):
        c = candidate('A', 0.1, 0.2)
        self.assertEqual(compare({'candidates': [c]})['candidates'][0]['total_cny'], 0.3)

    def test_ties_preserved(self):
        out = compare({'candidates': [candidate('A'), candidate('B')]})
        self.assertEqual(out['groups'][0]['lowest_among_fully_costed_eligible_ids'], ['A', 'B'])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError): compare({'candidates': [candidate(), candidate()]})

    def test_other_currency_rejected(self):
        c = candidate(); c['currency'] = 'KGS'
        with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_invalid_amounts(self):
        for value in [-1, True, '1', float('inf'), float('nan'), None]:
            with self.subTest(value=value):
                c = candidate(); c['costs']['goods']['amount'] = value
                with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_known_zero_needs_source(self):
        c = candidate(); c['costs']['duties']['source'] = ''
        with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_unknown_cannot_be_zero(self):
        c = candidate(); c['costs']['delivery_intl'] = {'amount': 0, 'certainty': 'unknown'}
        with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_not_applicable_must_be_zero(self):
        c = candidate(); c['costs']['duties']['amount'] = 1
        with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_invalid_quantity(self):
        for qty in [0, -1, True, 1.5]:
            c = candidate(); c['quantity'] = qty
            with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_missing_component_rejected(self):
        c = candidate(); del c['costs']['duties']
        with self.assertRaises(ValueError): compare({'candidates': [c]})

    def test_invalid_reference_rejected(self):
        with self.assertRaises(ValueError): compare({'candidates': [candidate()], 'reference_id': 'MISSING'})

    def test_conditional_reference_rejected(self):
        c = candidate(); c['gate'] = 'conditional'
        with self.assertRaises(ValueError): compare({'candidates': [c], 'reference_id': 'A'})

    def test_unknown_reference_rejected(self):
        c = candidate(); c['costs']['delivery_intl'] = {'amount': None, 'certainty': 'unknown'}
        with self.assertRaises(ValueError): compare({'candidates': [c], 'reference_id': 'A'})

    def test_empty_input_valid(self):
        self.assertEqual(compare({'candidates': []})['groups'], [])

    def test_bad_top_level(self):
        for data in [None, [], {}, {'candidates': None}]:
            with self.assertRaises(ValueError): compare(data)

    def test_input_not_mutated(self):
        data = {'candidates': [candidate()]}; original = copy.deepcopy(data)
        compare(data); self.assertEqual(data, original)

    def test_synthetic_marker(self):
        self.assertTrue(compare({'synthetic': True, 'candidates': [candidate()]})['synthetic'])


if __name__ == '__main__':
    unittest.main()
