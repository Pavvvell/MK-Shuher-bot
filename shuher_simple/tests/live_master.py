"""Real model, synthetic Master and captured Telegram. Never writes live records.

PYTHONPATH=.:tests python tests/live_master.py --config /private/deploy.env.yaml
"""
import argparse
import asyncio
import copy
import json
import time
from pathlib import Path
from unittest.mock import patch
from openai import AsyncOpenAI
from agent import run_agent
from test_master_updates import MasterTests

CASES = [
    ('close', 'У РРД тестову задачу вже зробили, закрий.', 'closed'),
    ('fact', 'Тестова задача з РРД вже виконана.', 'closed'),
    ('negated', 'Не закривай тестову задачу з РРД, її ще не зробили.', 'open'),
    ('future', 'Тестову задачу з РРД плануємо зробити завтра.', 'open'),
    ('question', 'Тестову задачу з РРД вже закрили?', 'readonly'),
    ('ambiguous', 'Закрий її в РРД.', 'readonly'),
    ('reschedule', 'У РРД для тестової задачі перенеси контроль на 25 листопада 2026.', 'date'),
]

async def main(path):
    import yaml
    cfg = yaml.safe_load(Path(path).read_text())
    api = AsyncOpenAI(api_key=cfg['OPENAI_API_KEY'], timeout=90, max_retries=1)
    model = cfg.get('OPENAI_MODEL', 'gpt-6.1-sol')
    results = []
    for name, message, expected in CASES:
        fixture = MasterTests(); await fixture.asyncSetUp()
        second = copy.deepcopy(fixture.master.client.data['Задачі'][1])
        second[0], second[2] = 'T2', 'Інша задача'
        fixture.master.client.data['Задачі'].append(second)
        started = time.monotonic()
        try:
            answer = await run_agent(api, model, fixture.rt, message)
            row = fixture.master.data('Задачі')[1]
            if expected == 'closed': assert row[4] == 'Готово', 'Not closed'
            elif expected == 'date': assert row[7] == '2026-11-25', 'Date not changed'
            else: assert row[4] != 'Готово', 'False closure'
            if expected == 'readonly': assert fixture.master.client.batch_count == 0, 'Unexpected write'
            assert fixture.master.data('Задачі')[2][4] != 'Готово', 'Wrong record closed'
            assert fixture.h.store.get_pending(123) is None, 'Unexpected confirmation draft'
            status = 'PASS'; error = ''
        except Exception as exc:
            status = 'FAIL'; error = type(exc).__name__ + ': ' + str(exc)[:240]; answer = ''
        tools = [c['tool'] for turn in fixture.rt.state.get('turns',[]) for c in turn.get('tools',[])]
        result = {'case': name, 'status': status, 'seconds': round(time.monotonic()-started,2),
                  'answer': answer, 'tools': tools, 'error': error}
        results.append(result); print(json.dumps(result, ensure_ascii=False), flush=True)
        await fixture.asyncTearDown()
    await api.close()
    assert all(r['status']=='PASS' for r in results), 'Live Master evaluation failed'

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--config', required=True)
    asyncio.run(main(p.parse_args().config))
