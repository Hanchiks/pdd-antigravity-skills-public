#!/usr/bin/env python3
"""Offline arithmetic only: compare explicitly supplied Pinduoduo offer costs.

No browsing, device control, quality scoring, account writes, or purchases.
All candidate prices cover the stated quantity, not a single unit.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

COMPONENTS = ('goods', 'delivery_cn', 'inspection', 'delivery_intl',
              'duties', 'payment', 'other')
BASES = {'china': ('goods', 'delivery_cn', 'payment'), 'landed': COMPONENTS}
CERTAINTIES = {'observed', 'provided', 'estimated', 'not_applicable', 'unknown'}
GATES = {'eligible', 'conditional', 'rejected'}


def money(value: Decimal | None) -> float | None:
    """Round display values; internal comparisons retain decimal precision."""
    if value is None:
        return None
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')
    return value.strip()


def read_component(value: Any, label: str) -> tuple[Decimal | None, str]:
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    certainty = value.get('certainty')
    if certainty not in CERTAINTIES:
        raise ValueError(f'{label}: invalid certainty')
    amount = value.get('amount')
    if certainty == 'unknown':
        if amount is not None:
            raise ValueError(f'{label}: unknown must have a null amount')
        return None, certainty
    if isinstance(amount, bool) or not isinstance(amount, (int, float, Decimal)):
        raise ValueError(f'{label}: known amount must be a number, not null/string/bool')
    try:
        number = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError(f'{label}: invalid amount') from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f'{label}: amount must be finite and nonnegative')
    if certainty == 'not_applicable' and number != 0:
        raise ValueError(f'{label}: not_applicable must have a zero amount')
    nonempty(value.get('source'), f'{label}.source')
    return number, certainty


def compare(data: Any, basis: str = 'landed') -> dict[str, Any]:
    """Validate data and return totals; never infer unknown costs as zero."""
    if basis not in BASES:
        raise ValueError('basis must be china or landed')
    if not isinstance(data, dict) or not isinstance(data.get('candidates'), list):
        raise ValueError('input must contain a candidates array')
    seen: set[str] = set()
    internal: list[dict[str, Any]] = []
    for candidate in data['candidates']:
        if not isinstance(candidate, dict):
            raise ValueError('each candidate must be an object')
        cid = nonempty(candidate.get('id'), 'id')
        if cid in seen:
            raise ValueError(f'duplicate candidate id: {cid}')
        seen.add(cid)
        group = nonempty(candidate.get('comparison_group'), f'{cid}.comparison_group')
        qty = candidate.get('quantity')
        if type(qty) is not int or qty <= 0:
            raise ValueError(f'{cid}: quantity must be a positive integer')
        if candidate.get('currency') != 'CNY':
            raise ValueError(f'{cid}: currency must be CNY; convert separately with a sourced rate')
        gate = candidate.get('gate')
        if gate not in GATES:
            raise ValueError(f'{cid}: invalid gate')
        available = candidate.get('available')
        if available is not None and type(available) is not bool:
            raise ValueError(f'{cid}: available must be true, false, or null')
        costs = candidate.get('costs')
        if not isinstance(costs, dict) or set(costs) != set(COMPONENTS):
            raise ValueError(f'{cid}: costs must contain exactly {COMPONENTS}')
        parsed = {key: read_component(costs[key], f'{cid}.{key}') for key in COMPONENTS}
        selected = BASES[basis]
        unknown = [key for key in selected if parsed[key][0] is None]
        subtotal = sum((parsed[key][0] for key in selected if parsed[key][0] is not None), Decimal(0))
        total = None if unknown else subtotal
        estimated = any(parsed[key][1] == 'estimated' for key in selected)
        internal.append({
            'id': cid, 'comparison_group': group, 'quantity': qty,
            'gate': gate, 'available': available, 'total': total,
            'known_subtotal': subtotal, 'unknown_components': unknown,
            'contains_estimate': estimated,
        })
    reference_id = data.get('reference_id')
    reference = None
    if reference_id is not None:
        nonempty(reference_id, 'reference_id')
        reference = next((row for row in internal if row['id'] == reference_id), None)
        if reference is None:
            raise ValueError('reference_id is not a candidate')
        if reference['gate'] != 'eligible' or reference['available'] is not True:
            raise ValueError('reference must be eligible and available')
        if reference['total'] is None:
            raise ValueError('reference total is unknown on the selected cost basis')
    rows = []
    for row in internal:
        result = {key: row[key] for key in ('id', 'comparison_group', 'quantity', 'gate',
                  'available', 'unknown_components', 'contains_estimate')}
        result.update(total_cny=money(row['total']), known_subtotal_cny=money(row['known_subtotal']))
        result['savings_vs_reference_cny'] = None
        result['savings_vs_reference_percent'] = None
        if (reference is not None and row['gate'] == 'eligible' and row['available'] is True
                and row['total'] is not None
                and (row['comparison_group'], row['quantity']) ==
                (reference['comparison_group'], reference['quantity'])):
            savings = reference['total'] - row['total']
            result['savings_vs_reference_cny'] = money(savings)
            if reference['total'] > 0:
                result['savings_vs_reference_percent'] = money(savings / reference['total'] * 100)
        rows.append(result)
    summaries = []
    keys = sorted({(row['comparison_group'], row['quantity']) for row in internal})
    for group, qty in keys:
        cohort = [row for row in internal if (row['comparison_group'], row['quantity']) == (group, qty)]
        computable = [row for row in cohort if row['gate'] == 'eligible'
                      and row['available'] is True and row['total'] is not None]
        least = min((row['total'] for row in computable), default=None)
        summaries.append({
            'comparison_group': group, 'quantity': qty,
            'lowest_among_fully_costed_eligible_ids': [row['id'] for row in computable if row['total'] == least],
            'lowest_total_cny': money(least),
            'any_compared_cost_estimated': any(row['contains_estimate'] for row in computable),
            'eligible_with_unknown_cost_or_availability_ids': [row['id'] for row in cohort
                if row['gate'] == 'eligible' and row['available'] is not False
                and (row['total'] is None or row['available'] is None)],
            'conditional_ids': [row['id'] for row in cohort if row['gate'] == 'conditional'],
        })
    return {'basis': basis, 'currency': 'CNY', 'synthetic': data.get('synthetic') is True,
            'reference_id': reference_id, 'candidates': rows, 'groups': summaries,
            'scope': 'Arithmetic on supplied evidence only. Not product verification or a purchase recommendation.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='UTF-8 JSON with candidates and optional reference_id')
    parser.add_argument('--basis', choices=tuple(BASES), default='landed')
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding='utf-8'), parse_float=Decimal)
        result = compare(data, args.basis)
    except (OSError, ValueError, TypeError, InvalidOperation) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
