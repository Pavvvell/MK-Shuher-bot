import copy
import importlib
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from sheet_fixtures import make_store
from dialogue import new_dialogue, queue_photo, queue_document
from concurrency import SheetsWorker

SCREENSHOT = 'а як у нас взагалі справи? що в блоці що в заявках? стисло і по суті'


class ReportRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = dict(TELEGRAM_BOT_TOKEN='test', TELEGRAM_WEBHOOK_SECRET='test',
                   OPENAI_API_KEY='test', SPREADSHEET_ID='test', OWNER_IDS='123')
        with patch.dict(os.environ, env), patch('store.SheetsStore'), patch('openai.AsyncOpenAI'):
            self.app = importlib.import_module('app')
        self.store = make_store()
        self.worker = SheetsWorker()
        self.sent = []
        async def bot(method, payload=None, files=None):
            self.sent.append((method, payload))
            return {'ok': True, 'result': {'message_id': 1}}
        self.model = AsyncMock(side_effect=AssertionError('Live report must not use a model'))
        self.patches = [patch.object(self.app, 'store', self.store),
                        patch.object(self.app, 'sheet_worker', self.worker),
                        patch.object(self.app, 'bot_call', bot),
                        patch.object(self.app, 'ask_model', self.model)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.worker.close()

    async def message(self, text=SCREENSHOT, uid=123):
        await self.app.handle_message({'message': {'from': {'id': uid, 'first_name': 'Test'},
            'chat': {'id': uid, 'type': 'private'}, 'text': text}})
        return '\n'.join(p.get('text', '') for m, p in self.sent if m == 'sendMessage')

    async def test_exact_screenshot_reads_live_rows_despite_old_photos_and_false_history(self):
        state, _ = queue_photo(new_dialogue(), 'old_photo', '', '', 'image/jpeg')
        state['history'] = [{'role': 'assistant', 'content': 'У таблицях 999 заявок, доступу немає.'}]
        self.store.save_dialogue(123, state)
        before = copy.deepcopy(self.store.client.data)
        answer = await self.message()
        self.assertIn('Google Sheets', answer)
        self.assertIn('Заявки: 1', answer)
        self.assertNotIn('999', answer)
        self.assertNotIn('Помилка', answer)
        self.model.assert_not_awaited()
        for table in ('Заявки', 'Закупка', 'Доручення', 'Рахунки'):
            self.assertEqual(before[table], self.store.client.data[table])
        self.assertEqual(self.store.get_dialogue(123)['photos'], state['photos'])

    async def test_report_preserves_pending_and_pdf(self):
        state = queue_document(new_dialogue(), 'pdf1', 'demo.pdf', '')
        self.store.save_dialogue(123, state)
        self.store.save_pending(123, 'Заявки', {'record_id': 'DRAFT', 'values': {}})
        pending = self.store.get_pending(123)
        answer = await self.message('/summary')
        self.assertIn('Google Sheets', answer)
        self.assertEqual(self.store.get_pending(123), pending)
        self.assertEqual(self.store.get_dialogue(123)['documents'], state['documents'])

    async def test_access_objection_reads_current_data(self):
        answer = await self.message('так у тебе до таблиць доступ є')
        self.assertIn('Google Sheets', answer)
        self.model.assert_not_awaited()

    async def test_write_only_user_cannot_read_reports(self):
        self.store.client.data['_Shuher_Access'] = [self.store.client.data['_Shuher_Access'][0],
                                                   ['456', 'Заявки', 'write_only', '123']]
        answer = await self.message(uid=456)
        self.assertIn('Немає права', answer)
        self.assertNotIn('болти', answer)

    async def test_partial_failure_is_not_zero_or_full_success(self):
        read = self.store.read_table
        def fail(table, *args, **kwargs):
            if table == 'Заявки':
                raise TimeoutError('test')
            return read(table, *args, **kwargs)
        with patch.object(self.store, 'read_table', fail):
            answer = await self.message()
        self.assertIn('Заявки: не вдалося прочитати', answer)
        self.assertNotIn('Заявки: 0', answer)
        self.assertIn('Рахунки:', answer)


class ReportRules(unittest.TestCase):
    def test_read_queries_and_non_queries(self):
        from reports import is_report_request
        for text in (SCREENSHOT, 'Що зависло?', 'Покажи блокери', 'Стан справ',
                     'Що по заявках?', 'Що у нас прострочено?', 'Дай зведення по підприємству',
                     'так у тебе до таблиць доступ є', 'Які доручення ще не виконані?'):
            with self.subTest(text=text):
                self.assertTrue(is_report_request(text))
        for text in ('оформляй заявку', 'болти вже замовили', 'що думаєш про диск?',
                     'покажи всі рахунки до оплати', 'напиши Юрі що в блоці', 'так',
                     'як зробити блок живлення?', 'заявку виконали', 'що раніше купували?'):
            with self.subTest(text=text):
                self.assertFalse(is_report_request(text))

    def test_explicit_blockers_deadlines_and_unknowns(self):
        from reports import build_report
        s = make_store()
        s.client.data['Доручення'] = [
            ['ID', 'Завдання', 'Проєкт', 'Статус', 'Дедлайн', 'Блокер / потрібне рішення'],
            ['T1', 'Робота 1', 'STF', 'У роботі', '31.10.2026', 'Немає матеріалу'],
            ['T2', 'Робота 2', 'STF', 'Виконано', '30.10.2026', 'Старий блокер'],
            ['T3', 'Робота 3', 'STF', '', 'завтра', ''],
            ['T4', 'Робота 4', 'STF', 'Очікує', '01.11.2026', ''],
        ]
        answer = build_report(s, {'Доручення': 'read'}, SCREENSHOT,
                              datetime(2026, 11, 1, 19, tzinfo=ZoneInfo('Europe/Kyiv')))
        self.assertIn('активні — 2', answer)
        self.assertIn('без статусу — 1', answer)
        self.assertIn('прострочені — 1', answer)
        self.assertIn('Немає матеріалу', answer)
        self.assertNotIn('Старий блокер', answer)
        self.assertIn('Строк відсутній або нерозбірливий: 1', answer)

    def test_project_filter_and_no_history_substitution(self):
        from reports import build_report
        s = make_store()
        answer = build_report(s, {'Заявки': 'read'}, 'Що по заявках проєкту НЕІСНУЮЧИЙ?')
        self.assertIn('не знайдено', answer)
        self.assertNotIn('Заявки: 1', answer)

    def test_reads_beyond_old_4000_row_limit(self):
        from reports import build_report
        s = make_store()
        sample = s.client.data['Заявки'][1]
        s.client.data['Заявки'] += [[f'R{i}'] + sample[1:] for i in range(2, 4102)]
        answer = build_report(s, {'Заявки': 'read'}, SCREENSHOT)
        self.assertIn('Заявки: 4101', answer)
        self.assertIn('Показано', answer)
