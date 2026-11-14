#!/usr/bin/env python3
"""Check recorded offer evidence for internal consistency; never verifies a listing.

This is a pre-recommendation check, independent of compare_prices.py. Older
sessions remain readable, but lack of evidence is reported as incomplete.

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


def audit_candidate(candidate):
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
    return {'id': candidate.get('id'), 'gate': candidate.get('gate'),
            'ready_for_unqualified_price_claim': not issues, 'issues': issues}


def audit_session(data):
    if not isinstance(data, dict) or not isinstance(data.get('candidates'), list):
        raise ValueError('input must contain a candidates array')
    return {'candidates': [audit_candidate(c) for c in data['candidates']],
            'scope': ('Проверка только внутренней согласованности введённых данных; '
                      'она не подтверждает подлинность источника, качество товара или право на скидку.')}


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
