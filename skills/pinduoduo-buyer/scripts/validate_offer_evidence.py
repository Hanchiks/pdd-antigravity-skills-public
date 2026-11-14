#!/usr/bin/env python3
"""Check recorded offer evidence for internal consistency; never verifies a listing.

This is a pre-recommendation check, independent of compare_prices.py. Older
sessions remain readable, but lack of evidence is reported as incomplete.
Provenance is read from the candidate's observed_facts/seller_claims/
externally_verified/inferences/unknowns arrays and from review_sample; a
seller claim never becomes an observation. Optional criteria are evaluated
against requirements_revision, but no status is upgraded automatically.

Modes and exit codes:
  default   full report for every candidate; exit 0 even when issues are
            present (read the JSON), exit 2 only when the file cannot be
            read, parsed or audited.
  --strict  finalists gate for explicitly named records only; there is no
            automatic selection. Requires at least one repeatable
            --candidate-id naming each record that appears in the final
            unconditional price claims. Exit 2 on invalid input: --strict
            without --candidate-id, a repeated --candidate-id, an empty or
            whitespace-only id, an unknown id, or an id that matches more
            than one record in the session (ambiguous selection), or
            --candidate-id used without --strict. For an unambiguous
            explicit selection: exit 0 when every selected record is
            internally consistent, exit 1 when any selected record has
            evidence problems. Records that were not selected never change
            the exit code; their issues stay visible in the report. A
            passing strict check never proves authenticity, quality,
            discount availability, stock, or the right to buy, and it never
            changes a gate status.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROVENANCE_TYPES = ('operator_observed', 'seller_claim', 'user_provided',
                    'externally_verified', 'model_estimate', 'unknown')
FACT_BUCKETS = (
    ('observed_facts', 'operator_observed'),
    ('seller_claims', 'seller_claim'),
    ('externally_verified', 'externally_verified'),
    ('inferences', 'model_estimate'),
    ('unknowns', 'unknown'),
)
CRITERION_STATES = ('confirmed', 'violates_must_have', 'unknown_critical',
                    'unknown_minor', 'not_applicable', 'needs_recheck')


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def audit_fact_sources(candidate):
    """Audit provenance buckets without ever promoting a seller claim.

    The five candidate arrays are the single machine-readable provenance
    schema. A seller-claim bucket can only report ``seller_claim`` even if the
    record itself says "confirmed"; mixed sources stay explicitly mixed.
    Plain-string legacy entries are accepted and inherit their bucket source.
    """
    evidence_issues = []
    facts = []
    seen_by_id = {}

    def problem(code, detail):
        evidence_issues.append({'code': code, 'detail': detail})

    for bucket, default_source in FACT_BUCKETS:
        items = candidate.get(bucket)
        if items is None:
            continue
        if not isinstance(items, list):
            problem('invalid_fact_bucket', f'{bucket} должен быть массивом.')
            continue
        for index, item in enumerate(items):
            text = declared = source = observed_at = asserted = None
            if isinstance(item, str):
                text, effective = _text(item), default_source
            elif isinstance(item, dict):
                text = _text(item.get('text')) or _text(item.get('claim'))
                declared = item.get('source_type')
                source = _text(item.get('source'))
                observed_at = _text(item.get('observed_at'))
                asserted = item.get('asserted_status') or item.get('status')
                source_types = item.get('source_types')
                candidates = []
                if declared is not None:
                    candidates.append(declared)
                if isinstance(source_types, list):
                    candidates.extend(source_types)
                invalid = [value for value in candidates if value not in PROVENANCE_TYPES]
                if invalid:
                    problem('invalid_fact_provenance',
                            f'{bucket}[{index}]: неизвестный источник {invalid!r}.')
                distinct = {value for value in candidates if value in PROVENANCE_TYPES}
                if item.get('mixed_sources') is True or len(distinct) > 1:
                    effective = 'mixed_unresolved'
                    problem('mixed_sources',
                            f'{bucket}[{index}]: в одном утверждении смешаны источники {sorted(distinct)}.')
                elif declared in PROVENANCE_TYPES:
                    effective = declared
                else:
                    effective = default_source
                if bucket == 'seller_claims':
                    if item.get('verified') is True or asserted in ('confirmed', 'verified', 'observed'):
                        problem('seller_claim_upgraded',
                                f'{bucket}[{index}]: заявление продавца помечено как подтверждённое.')
                    if effective != 'mixed_unresolved':
                        effective = 'seller_claim'
                elif bucket == 'unknowns' and effective != 'mixed_unresolved':
                    if declared not in (None, 'unknown'):
                        problem('unknown_bucket_promoted',
                                f'{bucket}[{index}]: неизвестное нельзя повысить до {declared!r}.')
                    effective = 'unknown'
                elif bucket == 'inferences' and effective != 'mixed_unresolved':
                    if declared not in (None, 'model_estimate', 'unknown'):
                        problem('inference_promoted',
                                f'{bucket}[{index}]: вывод модели нельзя повысить до {declared!r}.')
                    effective = 'model_estimate'
                if effective in ('operator_observed', 'externally_verified', 'user_provided'):
                    if not source:
                        problem('fact_source_missing',
                                f'{bucket}[{index}]: для источника {effective} не указан источник.')
                if effective == 'operator_observed' and not observed_at:
                    problem('observation_time_missing',
                            f'{bucket}[{index}]: для наблюдения оператора не указано время.')
                if not text:
                    problem('fact_text_missing', f'{bucket}[{index}]: пустое утверждение.')
            else:
                problem('invalid_fact_record', f'{bucket}[{index}]: запись должна быть строкой или объектом.')
                effective = 'unknown'
            record = {'bucket': bucket, 'index': index,
                      'id': _text(item.get('id')) if isinstance(item, dict) else None,
                      'text': text, 'effective_source': effective,
                      'declared_source': declared, 'source': source,
                      'observed_at': observed_at, 'asserted_status': asserted}
            facts.append(record)
            if record['id']:
                seen_by_id.setdefault(record['id'], []).append(record)

    for fact_id, records in seen_by_id.items():
        texts = {record['text'] for record in records if record['text']}
        if len(texts) > 1:
            problem('conflicting_fact_records',
                    f'{fact_id}: один идентификатор описывает разные утверждения.')
        elif len(records) > 1:
            problem('duplicate_fact_records',
                    f'{fact_id}: утверждение записано повторно; записи не сливаются автоматически.')
        buckets = {record['bucket'] for record in records}
        if 'unknowns' in buckets and buckets - {'unknowns'}:
            problem('unknown_claimed_as_known',
                    f'{fact_id}: поле одновременно неизвестно и заявлено известным.')

    return {'facts': facts, 'issues': evidence_issues}


def audit_review_sample(candidate):
    """Reviews are evidence only with a scope, source and observation time."""
    evidence_issues = []
    review = candidate.get('review_sample')
    if not isinstance(review, dict):
        return {'present': False, 'summary': None, 'issues': [{
            'code': 'missing_review_sample',
            'detail': 'Нет записи о просмотренных отзывах и их области.'}]}

    def problem(code, detail):
        evidence_issues.append({'code': code, 'detail': detail})

    count = review.get('count')
    if count is None:
        problem('review_count_unknown', 'Число просмотренных отзывов остаётся неизвестным (явный null).')
    elif type(count) is not int or count < 0:
        problem('review_count_invalid', 'Число отзывов должно быть неотрицательным целым или null.')
    method = _text(review.get('selection_method'))
    if not method:
        problem('review_method_missing', 'Не указано, как отбирались отзывы.')
    sku_filter = review.get('sku_filter_available')
    if sku_filter is not None and type(sku_filter) is not bool:
        problem('review_sku_filter_invalid', 'sku_filter_available должен быть true, false или null.')
    if count and sku_filter is None:
        problem('review_sku_scope_unknown', 'Неизвестно, относились ли отзывы к нужному SKU.')
    if not _text(review.get('source')):
        problem('review_source_missing', 'Не указан источник отзывов.')
    if not _text(review.get('observed_at')):
        problem('review_time_missing', 'Не указано время просмотра отзывов.')
    recurring = review.get('recurring_issues')
    if recurring is not None and not isinstance(recurring, list):
        problem('review_issues_invalid', 'recurring_issues должен быть массивом.')

    return {
        'present': True,
        'summary': {'count': count, 'selection_method': method,
                    'sku_filter_available': sku_filter,
                    'source': _text(review.get('source')),
                    'observed_at': _text(review.get('observed_at'))},
        'issues': evidence_issues,
    }


def audit_criteria(candidate, requirements_revision=None):
    """Derive a gate only from explicit criterion states; never upgrades.

    ``unknown_critical`` is not treated as a violation, and ``not_applicable``
    is not treated as unknown. When the session requirement revision is newer
    than a criterion's evaluation, that criterion becomes ``needs_recheck``;
    the derived gate cannot be ``eligible`` until it is re-evaluated.
    """
    criteria = candidate.get('criteria')
    base_gate = candidate.get('gate')
    if criteria is None:
        return {'present': False, 'derived_gate': base_gate,
                'criterion_states': [], 'recheck_required': [],
                'issues': []}
    issues = []
    if not isinstance(criteria, list):
        return {'present': True, 'derived_gate': 'unknown',
                'criterion_states': [], 'recheck_required': [],
                'issues': [{'code': 'criteria_invalid',
                            'detail': 'criteria должен быть массивом.'}]}

    states = []
    recheck = []
    violations = critical_unknowns = False
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, dict):
            issues.append({'code': 'criterion_invalid',
                           'detail': f'criteria[{index}] должен быть объектом.'})
            critical_unknowns = True
            continue
        cid = _text(criterion.get('id')) or f'#{index}'
        state = criterion.get('state')
        if state not in CRITERION_STATES:
            issues.append({'code': 'criterion_state_invalid',
                           'detail': f'{cid}: состояние {state!r} не распознано.'})
            state = 'unknown_critical'
        revision = criterion.get('evaluated_at_revision')
        if revision is None:
            revision = candidate.get('evaluated_at_revision')
        stale = (state != 'not_applicable' and requirements_revision is not None
                 and (type(revision) is not int or revision < requirements_revision))
        if stale:
            state = 'needs_recheck'
            recheck.append(cid)
        if state == 'violates_must_have':
            violations = True
        elif state in ('unknown_critical', 'needs_recheck'):
            critical_unknowns = True
        states.append({'id': cid, 'state': state,
                       'evaluated_at_revision': revision if type(revision) is int else None})

    if violations:
        derived = 'rejected'
    elif critical_unknowns:
        derived = 'conditional'
    else:
        derived = 'eligible'
    if not criteria:
        derived = base_gate
    return {'present': True, 'derived_gate': derived,
            'criterion_states': states, 'recheck_required': recheck,
            'issues': issues}


def audit_candidate(candidate, requirements_revision=None):
    if not isinstance(candidate, dict):
        raise ValueError('each candidate must be an object')
    issues = []
    def issue(code, message):
        issues.append({'code': code, 'detail': message})

    for key, label in (('title_zh', 'название карточки'), ('store_name', 'магазин'),
                       ('sku_zh', 'выбранный SKU'), ('observed_at', 'время наблюдения')):
        if not _text(candidate.get(key)):
            issue('missing_' + key, f'Не указано: {label}.')
    if candidate.get('available') is not True:
        issue('availability_unconfirmed', 'Наличие выбранного SKU не подтверждено.')
    refs = [_text(candidate.get(key)) for key in ('product_url', 'observed_share_code', 'product_id', 'listing_locator')]
    refs = [value for value in refs if value]
    if not refs:
        issue('listing_unidentified', 'Нет ссылки, идентификатора или способа найти карточку.')
    if candidate.get('currency') != 'CNY':
        issue('currency', 'Валюта записи должна быть CNY.')
    qty = candidate.get('quantity')
    if type(qty) is not int or qty < 1:
        issue('quantity', 'Укажите положительное целое количество.')

    offer = candidate.get('offer_evidence')
    if not isinstance(offer, dict):
        offer = {}
        issue('missing_offer_evidence', 'Нет записи, связывающей цену с конкретным вариантом.')
    for key, source in (('sku_zh', 'sku_zh'), ('store_name', 'store_name')):
        left, right = _text(candidate.get(source)), _text(offer.get(key))
        if not right:
            issue('missing_offer_' + key, f'В записи цены не указан {key}.')
        elif left and left != right:
            issue('mismatch_' + key, f'В записи цены {key} отличается от карточки.')
    if type(offer.get('quantity')) is not int or offer.get('quantity') != qty:
        issue('mismatch_quantity', 'Количество в записи цены отсутствует или отличается.')
    if not _text(offer.get('source')):
        issue('missing_offer_source', 'Не указан источник цены выбранного SKU.')
    listing = _text(offer.get('listing_ref'))
    if not listing:
        issue('missing_offer_listing', 'Цена не привязана к карточке через её идентификатор или способ поиска.')
    elif refs and listing not in refs:
        issue('mismatch_listing', 'Карточка в записи цены отличается от указанной у кандидата.')
    if not _text(offer.get('observed_at')):
        issue('missing_offer_time', 'Не указано время проверки цены.')
    if offer.get('currency') != 'CNY':
        issue('offer_currency', 'В записи цены укажите валюту CNY.')

    costs = candidate.get('costs') if isinstance(candidate.get('costs'), dict) else {}
    goods = costs.get('goods') if isinstance(costs.get('goods'), dict) else {}
    observed = _number(offer.get('price_cny'))
    recorded = _number(goods.get('amount'))
    if observed is None or recorded is None:
        issue('missing_price', 'Цена SKU отсутствует или не является неотрицательным числом.')
    elif observed != recorded:
        issue('mismatch_price', 'Цена в доказательстве отличается от стоимости товара в расчёте.')
    if goods.get('certainty') != 'observed' or not _text(goods.get('source')):
        issue('price_unverified', 'Цена товара не отмечена как наблюдённая с указанным источником.')
    elif _text(offer.get('source')) and goods['source'].strip() != offer['source'].strip():
        issue('mismatch_price_source', 'Источник цены в расчёте отличается от источника записи SKU.')

    discount = offer.get('discount') if isinstance(offer.get('discount'), dict) else {}
    kind = discount.get('kind')
    if kind not in ('none', 'conditional', 'unknown'):
        issue('discount_unknown', 'Не указан тип скидки: none, conditional или unknown.')
    elif kind == 'unknown':
        issue('discount_unknown', 'Условия цены или скидки не установлены.')
    elif kind == 'conditional':
        if not _text(discount.get('terms')) or not _text(discount.get('source')):
            issue('discount_terms_missing', 'Не указаны условия и источник скидки.')
        if discount.get('applies') is not True:
            issue('discount_not_confirmed', 'Применимость скидки не подтверждена.')
        ordinary = _number(discount.get('ordinary_price_cny'))
        if ordinary is None:
            issue('ordinary_price_unknown', 'Обычная цена для условной скидки неизвестна.')
        elif observed is not None and ordinary < observed:
            issue('discount_price_conflict', 'Обычная цена ниже отмеченной цены со скидкой; проверьте данные.')
    elif kind == 'none' and not _text(discount.get('source')):
        issue('discount_source_missing', 'Не указан источник проверки условий цены.')
    if kind == 'none' and candidate.get('discount_conditions'):
        issue('discount_conditions_conflict', 'Указано отсутствие скидки, но в карточке записаны её условия.')

    delivery = costs.get('delivery_cn') if isinstance(costs.get('delivery_cn'), dict) else {}
    dcert = delivery.get('certainty')
    damount = _number(delivery.get('amount'))
    if dcert == 'unknown' and delivery.get('amount') is not None:
        issue('delivery_conflict', 'Неизвестная доставка должна иметь null, а не сумму.')
    elif dcert not in ('observed', 'provided', 'estimated', 'not_applicable') or damount is None:
        issue('delivery_unknown', 'Стоимость доставки до китайского склада не подтверждена.')
    elif dcert == 'not_applicable' and damount != 0:
        issue('delivery_conflict', 'Для неприменимой доставки сумма должна быть 0.')
    elif not _text(delivery.get('source')):
        issue('delivery_source_missing', 'Не указан источник стоимости доставки.')
    if dcert == 'estimated':
        issue('delivery_estimated', 'Доставка до склада указана оценочно.')
    if offer.get('delivery_destination') != 'saved_cn_warehouse':
        issue('delivery_destination_unconfirmed', 'Не подтверждено, что доставка относится к сохранённому китайскому складу.')
    payment = costs.get('payment') if isinstance(costs.get('payment'), dict) else {}
    pc, pa = payment.get('certainty'), _number(payment.get('amount'))
    if pc not in ('observed', 'provided', 'estimated', 'not_applicable') or pa is None:
        issue('payment_unknown', 'Применимая комиссия оплаты неизвестна.')
    elif pc == 'not_applicable' and pa != 0:
        issue('payment_conflict', 'Для неприменимой комиссии сумма должна быть 0.')
    elif not _text(payment.get('source')):
        issue('payment_source_missing', 'Не указан источник комиссии оплаты.')
    if pc == 'estimated':
        issue('payment_estimated', 'Комиссия оплаты указана оценочно.')
    facts = audit_fact_sources(candidate)
    review = audit_review_sample(candidate)
    criteria = audit_criteria(candidate, requirements_revision)
    evidence_issues = list(facts['issues']) + list(review['issues']) + list(criteria['issues'])
    return {'id': candidate.get('id'), 'gate': candidate.get('gate'),
            'ready_for_unqualified_price_claim': not issues, 'issues': issues,
            'fact_sources': facts['facts'],
            'review_sample': review['summary'],
            'evidence_issues': evidence_issues,
            'evidence_complete': not evidence_issues,
            'derived_gate': criteria['derived_gate'],
            'criterion_states': criteria['criterion_states'],
            'recheck_required': criteria['recheck_required']}


def audit_session(data):
    if not isinstance(data, dict) or not isinstance(data.get('candidates'), list):
        raise ValueError('input must contain a candidates array')
    revision = data.get('requirements_revision')
    if revision is not None and type(revision) is not int:
        raise ValueError('requirements_revision must be an integer')
    result = {'candidates': [audit_candidate(c, revision) for c in data['candidates']],
              'scope': ('Проверка только внутренней согласованности введённых данных; '
                        'она не подтверждает подлинность источника, качество товара или право на скидку. '
                        'Источники фактов и статусы критериев выводятся из явных полей; '
                        'заявление продавца не повышается до наблюдения.')}
    if revision is not None:
        result['requirements_revision'] = revision
    return result


def _selected_ids(data, requested_ids):
    """Explicit finalists only: validate and resolve the requested ids exactly.

    Raises ValueError (exit 2) when the selection is missing, duplicated,
    empty, unknown, or ambiguous. No automatic selection is performed.
    """
    if not requested_ids:
        raise ValueError('--strict требует хотя бы один --candidate-id; автоматический выбор финалистов отключён')
    ordered = []
    for cid in requested_ids:
        if not isinstance(cid, str) or not cid.strip():
            raise ValueError(f'некорректный --candidate-id (пустой или нестроковый): {cid!r}')
        if cid in ordered:
            raise ValueError(f'дубликат --candidate-id: {cid}')
        ordered.append(cid)
    counts = {}
    for candidate in data['candidates']:
        cid = candidate.get('id')
        if isinstance(cid, str) and cid in ordered:
            counts[cid] = counts.get(cid, 0) + 1
    unknown = [cid for cid in ordered if cid not in counts]
    if unknown:
        raise ValueError('неизвестный --candidate-id: ' + ', '.join(map(repr, unknown)))
    ambiguous = [cid for cid in ordered if counts[cid] > 1]
    if ambiguous:
        raise ValueError('неоднозначный --candidate-id (несколько записей журнала с этим id): '
                         + ', '.join(map(repr, ambiguous)))
    return ordered


def apply_strict(data, result, requested_ids):
    """Attach the strict-mode summary; only explicitly selected records gate the exit code."""
    selection = _selected_ids(data, requested_ids)
    selected = set(selection)
    problems = []
    unselected_with_issues = []
    for row, candidate in zip(result['candidates'], data['candidates']):
        cid = candidate.get('id')
        chosen = isinstance(cid, str) and cid in selected
        row['strict_selected'] = chosen
        if chosen:
            if row['issues']:
                problems.append({'id': cid, 'issues': row['issues']})
        elif row['issues']:
            unselected_with_issues.append(cid)
    result['strict'] = True
    result['strict_selected_ids'] = selection
    result['strict_problems'] = problems
    result['strict_unselected_with_issues'] = unselected_with_issues
    result['strict_passed'] = not problems
    result['scope'] += (' Строгий режим: код 0 означает внутреннюю согласованность только '
                        'явно выбранных записей и не подтверждает подлинность источника, '
                        'качество товара, реальность скидки, наличие или право на покупку; '
                        'статус кандидата не меняется.')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='UTF-8 JSON search session')
    parser.add_argument('--candidate-id', action='append', default=[], metavar='ID',
                        help='id финалиста для строгой проверки; повторите для нескольких разных ID (только вместе с --strict)')
    parser.add_argument('--strict', action='store_true',
                        help='строгий режим по явно названным финалистам; без --candidate-id это ошибка ввода (код 2)')
    args = parser.parse_args(argv)
    if args.candidate_id and not args.strict:
        parser.error('--candidate-id требует --strict')
    if args.strict and not args.candidate_id:
        print('ERROR: --strict требует хотя бы один --candidate-id; автоматический выбор финалистов отключён',
              file=sys.stderr)
        return 2
    try:
        data = json.loads(args.input.read_text(encoding='utf-8'), parse_float=Decimal)
        result = audit_session(data)
        if args.strict:
            apply_strict(data, result, args.candidate_id)
    except (OSError, ValueError, TypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('strict'):
        return 0 if result['strict_passed'] else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
