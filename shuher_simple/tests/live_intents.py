"""Opt-in real-model routing check with synthetic prompts; no Sheets/Telegram writes.

Run from shuher_simple: python tests/live_intents.py /tmp/shuher-intents.json
Uses the existing private deploy.env.yaml locally; never prints credentials.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from natural import interpret_natural
from reports import is_report_request
from payable_invoices import is_payable_request
from llm import generate

CASES = [
    ('а як у нас взагалі справи? що в блоці що в заявках? стисло і по суті', 'overview'),
    ('так у тебе до таблиць доступ є', 'overview'),
    ('Що зависло?', 'overview'),
    ('Які доручення ще не виконані?', 'overview'),
    ('Що у нас прострочено?', 'overview'),
    ('Дай загальну картину по підприємству', 'overview'),
    ('Як просуваються наші роботи?', 'overview'),
    ('Що потребує мого рішення зараз?', 'overview'),
    ('мені скинь рахунки', 'payable_invoices'),
    ('то скинеш пдф файли і фото з підписом, що куди на оплату?', 'payable_invoices'),
    ('покажи всі рахунки які я мав кинути на оплату', 'payable_invoices'),
    ('покажи залишки', 'balance'),
    ('скільки болтів М8 є на складі?', 'balance'),
    ('що сьогодні у закупці?', 'today'),
    ('яку трубу раніше купували для STF-02', 'history'),
    ('що Юрі доручали раніше', 'history'),
    ('болти М8×30 вже замовили', 'status'),
    ('гайки М12 оплатили', 'status'),
    ('завдання з вентиляції виконали', 'status'),
    ('треба купити 10 болтів М8 на STF', 'append'),
    ('оформляй заявку', 'append'),
    ('привезли 20 гайок М12 на склад', 'append'),
    ('видав зі складу 2 коробки', 'append'),
    ('це рахунок на оплату, проєкт STF', 'invoice'),
    ('який диск краще для сталі 3 мм?', 'other'),
    ('що думаєш про цей варіант?', 'other'),
    ('привіт', 'other'),
]


async def main():
    import yaml
    from openai import AsyncOpenAI
    cfg = yaml.safe_load(Path('deploy.env.yaml').read_text())
    client = AsyncOpenAI(api_key=cfg['OPENAI_API_KEY'], timeout=90, max_retries=1)
    access = {t: 'write' for t in ('Заявки', 'Закупка', 'Доручення', 'Склад', 'Рахунки')}
    semaphore = asyncio.Semaphore(3)
    results = []

    async def run(text, expected, repeat):
        async with semaphore:
            start = time.perf_counter()
            async def model(messages, max_tokens=350, json_mode=True):
                return await generate(client, cfg['OPENAI_MODEL'], messages,
                                      max_tokens=max_tokens, json_mode=json_mode)
            route = 'deterministic'
            try:
                if is_payable_request(text):
                    action = {'intent': 'payable_invoices'}
                elif is_report_request(text):
                    action = {'intent': 'overview'}
                else:
                    route = 'real-model'
                    action = await interpret_natural(text, access, True, model,
                        'Обговорили заявку: 10 болтів М8, проєкт STF. Запис ще не виконаний.')
                result = dict(text=text, expected=expected, action=action,
                              passed=action['intent'] == expected, route=route, repeat=repeat,
                              seconds=round(time.perf_counter() - start, 2))
            except Exception as exc:
                result = dict(text=text, expected=expected, passed=False, error=type(exc).__name__)
            results.append(result)
            Path(sys.argv[1]).write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False), flush=True)

    await asyncio.gather(*(run(text, expected, repeat) for repeat in range(2) for text, expected in CASES))
    await client.close()
    print('SUMMARY', json.dumps({'passed': sum(r['passed'] for r in results), 'total': len(results),
                                'real_model_calls': sum(r.get('route') == 'real-model' for r in results)}))
    if not all(r['passed'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
