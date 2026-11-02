"""Current operational reports calculated from permitted Sheets rows, without an LLM."""
import logging
import re
from collections import Counter
from datetime import datetime, time
from zoneinfo import ZoneInfo

from access_ui import can_read
from payable_invoices import payable_rows

TABLES = ('Заявки', 'Закупка', 'Доручення', 'Рахунки')
KYIV = ZoneInfo('Europe/Kyiv')
log = logging.getLogger('shuher-lite')


def normalized(value):
    return ' '.join(str(value or '').casefold().replace('проект', 'проєкт').split())




def deadline(value):
    value = str(value or '').strip()
    for fmt in ('%d.%m.%Y %H:%M', '%Y-%m-%d %H:%M', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            parsed = datetime.strptime(value, fmt)
            if '%H' not in fmt:
                parsed = datetime.combine(parsed.date(), time.max)
            return parsed.replace(tzinfo=KYIV)
        except ValueError:
            pass
    return None


def closed(row, table):
    state = normalized(row.get('Статус'))
    if table == 'Закупка':
        state = normalized(row.get('Статус замовлення'))
    return state in ('виконано', 'виконана', 'завершено', 'закрито', 'отримано') or state.startswith('скасован')


def blockers(row):
    values = [row.get(key, '').strip() for key in ('Блокер', 'Блокер / потрібне рішення')]
    values = [v for v in values if normalized(v) not in ('', 'ні', 'немає', 'нема', '-', '—', 'no')]
    if normalized(row.get('Потрібне рішення власника')) in ('так', 'yes'):
        values.append('Потрібне рішення власника')
    return '; '.join(values)


def label(row):
    text = row.get('Найменування') or row.get('Завдання') or row.get('Постачальник') or 'Без назви'
    parts = [row.get('ID', '?'), text]
    parts += [row[k] for k in ('Проєкт', 'Виконавець') if row.get(k)]
    return ' · '.join(' '.join(str(p).split())[:90] for p in parts)


def build_report(store, access, question='', at=None):
    at = at or datetime.now(KYIV)
    tables = [t for t in TABLES if can_read(access.get(t))]
    if not tables:
        return 'Немає права читати робочі реєстри для звіту. Власник може надати доступ.'
    snapshots = {}
    errors = []
    for table in tables:
        try:
            headers, rows = store.read_table(table, None)
            if not headers:
                raise ValueError('Missing table headers')
            snapshots[table] = [dict(zip(headers, [str(v or '').strip() for v in row]))
                                for row in rows if any(str(v or '').strip() for v in row)]
        except Exception:
            log.exception('Cannot read report source %s', table)
            errors.append(table)

    # Select only names present in the readable source rows. Never invent a project.
    question = normalized(question)
    names = {r.get('Проєкт', '') for rows in snapshots.values() for r in rows} - {''}
    selected = [p for p in names if normalized(p) in question]
    explicit = re.search(r'\bпроєкт(?:у|і|ом)?\s+[«"\w]', question)
    if explicit and not selected:
        return ('У прочитаних доступних реєстрах цей проєкт не знайдено. '
                'Перевір назву або /projects. Дані інших проєктів не підставляю.'
                + (' Не вдалося прочитати: ' + ', '.join(errors) if errors else ''))
    if len(selected) > 1:
        return 'Уточни один проєкт для звіту: ' + ', '.join(sorted(selected))
    if selected:
        snapshots = {t: [r for r in rows if r.get('Проєкт') == selected[0]] for t, rows in snapshots.items()}

    scope = 'проєкт «' + selected[0] + '»' if selected else 'усі доступні проєкти'
    lines = ['Стан на ' + at.strftime('%d.%m.%Y %H:%M') + ' (Київ).',
             'Джерело: поточні Google Sheets; ' + scope + '.']
    blocked = []
    overdue = []
    no_deadline = 0
    for table in tables:
        if table in errors:
            lines.append('\n' + table + ': не вдалося прочитати. Дані не підтверджені.')
            continue
        rows = snapshots[table]
        if table == 'Рахунки':
            headers = sorted({k for r in rows for k in r})
            payable = payable_rows(headers, [[r.get(k, '') for k in headers] for r in rows])
            lines.append(f'\nРахунки: {len(rows)}; без підтвердженої повної оплати — {len(payable)}.')
            if payable:
                lines.append('Вкладення рахунків власнику: /invoices.')
            continue
        pending = [r for r in rows if not closed(r, table)]
        field = 'Статус замовлення' if table == 'Закупка' else 'Статус'
        unknown = sum(not r.get(field) for r in pending)
        lines.append(f'\n{table}: {len(rows)}; активні — {len(pending) - unknown}; '
                     f'закриті/скасовані — {len(rows) - len(pending)}; без статусу — {unknown}.')
        statuses = Counter(r.get(field) or 'Не вказано' for r in pending)
        if statuses:
            lines.append('; '.join(f'{s}: {n}' for s, n in sorted(statuses.items())) + '.')
        for row in pending:
            reason = blockers(row)
            if reason:
                blocked.append(label(row) + ': ' + reason[:180])
            due = deadline(next((row[k] for k in ('Дедлайн', 'Термін', 'Потрібно до', 'Очікувана доставка') if row.get(k)), ''))
            if due is None:
                no_deadline += 1
            elif due < at:
                overdue.append(label(row) + ': до ' + due.strftime('%d.%m.%Y'))
        for row in pending[:3]:
            lines.append('• ' + label(row) + ' — ' + (row.get(field) or 'статус не вказано'))
        if len(pending) > 3:
            lines.append(f'Показано 3 із {len(pending)} незакритих. Повний список: /export {table}')
    lines.append(f'\nЗафіксовані блокери — {len(blocked)}; прострочені — {len(overdue)}'
                 + (' (лише прочитані таблиці).' if errors else '.'))
    lines += ['• ' + x for x in blocked[:5]]
    lines += ['• Прострочено: ' + x for x in overdue[:5]]
    if len(blocked) > 5 or len(overdue) > 5:
        lines.append('Показано перші 5 у кожній групі; повні рядки — /export НазваТаблиці.')
    if no_deadline:
        lines.append(f'Строк відсутній або нерозбірливий: {no_deadline}; прострочення цих позицій невідоме.')
    if not blocked:
        lines.append('Це відсутність записаних блокерів, а не підтвердження, що проблем немає.')
    if len(tables) < len(TABLES):
        lines.append('Звіт обмежений правами читання: ' + ', '.join(tables) + '.')
    lines.append('Старі реєстри й переписка до цих підрахунків не входять. Історія: /history запит.')
    return '\n'.join(lines)
