"""Owner-directed Master edits, atomic with their durable receipt in Журнал.

The model selects records and fields. This module only validates and applies
edits to records read during the current turn; it does not classify messages.
"""
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from store import quoted

TABLES = {
    'Задачі': {'Статус', 'Owner', 'Дедлайн', 'Контрольна дата', 'Чекаємо від',
               'Наступний крок', 'Нотатки', 'Пріоритет'},
    'Проєкти': {'Статус', 'Owner', 'Дедлайн', 'Контрольна дата', 'Поточний стан',
                'Наступна дія', 'Блокер / ризик', 'Пріоритет'},
    'Регулярні': {'Статус', 'Коли / тригер', 'Owner', 'Що потрібно від Павла',
                  'Що робить ChatGPT', 'Останнє виконання', 'Наступний контроль', 'Нотатки'},
}
CLOSED = {'Готово', 'Скасовано'}
JOURNAL_HEADERS = ['Дата/час', 'Сутність', 'Подія / апдейт', 'Джерело', 'Нотатки']


def row_dict(headers, row):
    return {h: str(row[i]) if i < len(row) and row[i] is not None else ''
            for i, h in enumerate(headers)}


def cell_write(sheet_id, row, col, value):
    return {'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': row,
                                     'columnIndex': col},
                            'rows': [{'values': [{'userEnteredValue': {'stringValue': value}}]}],
                            'fields': 'userEnteredValue'}}


def apply_updates(store, updates, reason, source, snapshots):
    """Called inside the existing single Sheets worker; never sends messages."""
    targets = [(u['table'], u['id']) for u in updates]
    if not updates or len(targets) != len(set(targets)):
        raise ValueError('Потрібні різні конкретні записи Master.')
    if any(t not in TABLES for t, _ in targets):
        raise ValueError('Цю таблицю Master змінювати не можна.')
    normalized = []
    for update in updates:
        changes = {c['field']: c['value'] for c in update['changes']}
        if not changes or len(changes) != len(update['changes']):
            raise ValueError('Поля зміни мають бути непорожні й без повторів.')
        if not set(changes) <= TABLES[update['table']]:
            raise ValueError('Зміна цього поля Master недоступна.')
        normalized.append({'table': update['table'], 'id': update['id'], 'changes': changes})
    canonical = json.dumps(sorted(normalized, key=lambda u: (u['table'], u['id'])),
                           sort_keys=True, ensure_ascii=False)
    key = 'master:' + hashlib.sha256((source + ':' + canonical).encode()).hexdigest()
    store._invalidate()
    journal = store.data('Журнал', None)
    if not journal or journal[0][:5] != JOURNAL_HEADERS:
        raise ValueError('Невідома структура журналу Master.')
    for row in journal[1:]:
        if len(row) >= 5 and row[3] == source:
            try:
                receipt = json.loads(row[4])
            except (ValueError, TypeError):
                continue
            if isinstance(receipt, dict) and receipt.get('key') == key:
                return verify(store, receipt['updates'], key, duplicate=True)

    prepared = []
    ranges = []
    for update in normalized:
        table, record_id = update['table'], update['id']
        rows = store.data(table, None)
        headers = rows[0]
        matches = [(n, row_dict(headers, r)) for n, r in enumerate(rows[1:], 1)
                   if r and r[0] == record_id]
        if len(matches) != 1:
            raise ValueError('Запис Master не знайдений однозначно: ' + record_id)
        n, current = matches[0]
        if snapshots.get((table, record_id)) != current:
            raise ValueError('Спершу перечитай цей запис Master: дані не прочитані або вже змінилися.')
        changes = update['changes'].copy()
        if changes.get('Статус') in CLOSED:
            for field in ('Дедлайн', 'Контрольна дата', 'Чекаємо від', 'Блокер / ризик',
                          'Наступний контроль', 'Що потрібно від Павла'):
                if field in headers:
                    changes[field] = ''
            for field in ('Наступний крок', 'Наступна дія', 'Що робить ChatGPT'):
                if field in headers:
                    changes[field] = 'Закрито. Не нагадувати.'
        if 'Останній апдейт' in headers:
            changes['Останній апдейт'] = datetime.now(ZoneInfo('Europe/Kyiv')).date().isoformat()
        if any(f not in headers for f in changes):
            raise ValueError('Поле відсутнє у поточній структурі Master.')
        prepared.append({**update, 'changes': changes, 'row': n, 'headers': headers})
        ranges.append(f'{quoted(table)}!A{n+1}:{chr(64+len(headers))}{n+1}')

    grid = store.client.get(spreadsheetId=store.id, ranges=ranges,
        fields='sheets(properties(sheetId,title),data(startRow,rowData(values(userEnteredValue,formattedValue,dataValidation,chipRuns))))').execute()
    sheet_ids = {s['properties']['title']: s['properties']['sheetId'] for s in grid['sheets']}
    cells = {(s['properties']['title'], d.get('startRow', 0)+i): row.get('values', [])
             for s in grid['sheets'] for d in s.get('data', [])
             for i, row in enumerate(d.get('rowData', []))}
    requests = []
    for update in prepared:
        table, n, headers = update['table'], update['row'], update['headers']
        raw = cells.get((table, n), [])
        current = row_dict(headers, [c.get('formattedValue', '') for c in raw])
        if current != snapshots[(table, update['id'])]:
            raise ValueError('Master змінився під час читання. Перечитай запис перед зміною.')
        for field, value in update['changes'].items():
            col = headers.index(field)
            cell = raw[col] if col < len(raw) else {}
            if 'formulaValue' in cell.get('userEnteredValue', {}) or cell.get('chipRuns'):
                raise ValueError('Не можна перезаписувати формулу або smart chip.')
            rule = cell.get('dataValidation', {}).get('condition')
            if rule and value:
                if rule['type'] != 'ONE_OF_LIST':
                    raise ValueError('Поле має обмеження; ця зміна потребує іншого інструмента.')
                allowed = [v['userEnteredValue'] for v in rule.get('values', [])]
                if value not in allowed:
                    raise ValueError('Допустимі значення для ' + field + ': ' + ', '.join(allowed))
            if current[field] != value:
                requests.append(cell_write(sheet_ids[table], n, col, value))
    receipt_updates = [{k: u[k] for k in ('table', 'id', 'changes')} for u in prepared]
    receipt = json.dumps({'key': key, 'updates': receipt_updates}, ensure_ascii=False)
    meta = store.client.get(spreadsheetId=store.id, fields='sheets.properties').execute()
    journal_id = next(s['properties']['sheetId'] for s in meta['sheets']
                      if s['properties']['title'] == 'Журнал')
    values = [datetime.now(ZoneInfo('Europe/Kyiv')).isoformat(),
              ', '.join(u['id'] for u in prepared), reason, source, receipt]
    requests.append({'appendCells': {'sheetId': journal_id,
        'rows': [{'values': [{'userEnteredValue': {'stringValue': v}} for v in values]}],
        'fields': 'userEnteredValue'}})
    store._invalidate()
    # Changes and receipt commit together. A timeout is recoverable by receipt lookup.
    store.client.batchUpdate(spreadsheetId=store.id, body={'requests': requests}).execute()
    return verify(store, receipt_updates, key)


def verify(store, updates, key, duplicate=False):
    store._invalidate()
    records = []
    matches = True
    for update in updates:
        rows = store.data(update['table'], None)
        found = [row_dict(rows[0], r) for r in rows[1:] if r and r[0] == update['id']]
        same = len(found) == 1 and all(found[0].get(k) == v for k, v in update['changes'].items())
        matches = matches and same
        records.append({'table': update['table'], 'id': update['id'],
                        'values': {k: found[0].get(k) for k in update['changes']} if len(found) == 1 else {},
                        'matches': same})
    return {'ok': matches, 'committed': True, 'verified': matches, 'duplicate': duplicate,
            'receipt': key, 'records': records,
            'error': '' if matches else 'Запис був застосований, але поточні значення відрізняються. Перечитай Master; не оголошуй успіх.'}
