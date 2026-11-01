import importlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from concurrency import SheetsWorker
from sheet_fixtures import make_store
from task_notifications import task_fields, task_marker


class TaskDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = dict(TELEGRAM_BOT_TOKEN='test', TELEGRAM_WEBHOOK_SECRET='test',
                   OPENAI_API_KEY='test', SPREADSHEET_ID='test', OWNER_IDS='123')
        with patch.dict(os.environ, env), patch('store.SheetsStore'), patch('openai.AsyncOpenAI'):
            self.app = importlib.import_module('app')
        self.store = make_store()
        self.store.client.data['Доручення'] = [self.store.client.data['Доручення'][0]]
        self.store.client.data['_Shuher_Users'].append(['789', 'worker2', 'Sergey', '', ''])
        self.store.client.data['_Shuher_Access'].append(['789', 'Доручення', 'read', '123'])
        self.worker = SheetsWorker()
        self.sent = []
        self.failed = set()
        async def bot(method, payload=None, files=None):
            if method == 'sendMessage' and payload['chat_id'] in self.failed:
                raise RuntimeError('Simulated Telegram rejection')
            self.sent.append((method, payload))
            return {'ok': True, 'result': {'message_id': len(self.sent)}}
        self.model = AsyncMock(return_value=json.dumps({'Завдання': 'ТЕСТ: перевірити доставку доручення',
                                                        'Проєкт': '', 'Дедлайн': ''}))
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

    async def message(self, text, uid=123):
        await self.app.handle_message({'message': {'from': {'id': uid, 'first_name': 'Test'},
            'chat': {'id': uid, 'type': 'private'}, 'text': text}})

    def texts(self, uid):
        return '\n'.join(p['text'] for m, p in self.sent if m == 'sendMessage' and p['chat_id'] == uid)

    async def test_task_draft_confirmation_delivery_and_no_duplicate(self):
        await self.message('Доручи Юрію перевірити доставку тестового повідомлення')
        self.assertEqual(self.texts(456), '')
        self.assertEqual(len(self.store.client.data['Доручення']), 1)
        await self.message('так')
        self.assertEqual(len(self.store.client.data['Доручення']), 2)
        self.assertIn('Нове доручення', self.texts(456))
        self.assertIn('Telegram прийняв', self.texts(123))
        before = len(self.sent)
        self.assertEqual(await self.app.sync_task_notifications(), 0)
        self.assertEqual(len(self.sent), before)

    async def test_failed_delivery_is_visible_and_retry_sends_once(self):
        self.failed.add(456)
        await self.message('Доручи Юрію перевірити доставку тестового повідомлення')
        await self.message('так')
        self.assertEqual(len(self.store.client.data['Доручення']), 2)
        self.assertEqual(self.texts(456), '')
        self.assertIn('не доставлено', self.texts(123))
        await self.message('/tasksync')
        self.assertIn('залишилось: 1', self.texts(123))
        self.failed.clear()
        await self.message('/tasksync')
        self.assertIn('Нове доручення', self.texts(456))
        self.assertEqual(await self.app.sync_task_notifications(), 0)

    async def test_direct_message_partial_failure_does_not_stop_other_recipient(self):
        self.failed.add(456)
        await self.message('Напиши Юрію та Сергію: ТЕСТ — виконувати не потрібно')
        self.assertIn('ТЕСТ', self.texts(789))
        self.assertIn('❌', self.texts(123))
        self.assertIn('✅', self.texts(123))

    async def test_negated_task_does_not_create_draft_or_call_model(self):
        self.assertFalse(await self.app.stage_owner_task_order(123, 123, {'id': 123},
            'Не доручи Юрію перевірити доставку'))
        self.assertFalse(await self.app.stage_owner_task_order(123, 123, {'id': 123},
            'Ти вже поставив завдання Юрію?'))
        self.model.assert_not_awaited()
        self.assertIsNone(self.store.get_pending(123))

    async def test_unknown_recipient_has_actionable_error_not_chat_fallback(self):
        with self.assertRaisesRegex(ValueError, 'виконавця'):
            await self.app.stage_owner_task_order(123, 123, {'id': 123},
                'Доручи Невідомому перевірити доставку')

    async def test_revoked_assignee_is_not_silently_saved_at_confirmation(self):
        await self.message('Доручи Юрію перевірити доставку тестового повідомлення')
        self.store.grant(456, 'Доручення', 'none', 123)
        await self.message('так')
        self.assertEqual(len(self.store.client.data['Доручення']), 1)
        self.assertIn('права читання', self.texts(123))

    async def test_unrelated_draft_is_not_overwritten_by_new_task(self):
        self.store.save_pending(123, 'Заявки', {'record_id': 'KEEP', 'values': {'Найменування': 'болти'}})
        before = self.store.get_pending(123)
        await self.message('Доручи Юрію перевірити доставку тестового повідомлення')
        self.assertEqual(self.store.get_pending(123), before)
        self.assertIn('/cancel', self.texts(123))

    async def test_employee_cannot_trigger_owner_task_or_retry_all_notifications(self):
        self.assertFalse(await self.app.stage_owner_task_order(456, 456, {'id': 456},
            'Доручи Сергію перевірити доставку'))
        await self.message('/tasksync', uid=456)
        self.assertNotIn('Нове доручення', self.texts(789))

    async def test_multiple_assignees_do_not_fall_through_to_plain_message(self):
        await self.message('Доручи Юрію та Сергію перевірити доставку тестового повідомлення')
        self.assertIn('одного виконавця', self.texts(123))
        self.assertIsNone(self.store.get_pending(123))
        self.assertEqual(self.texts(456), '')
        self.assertEqual(self.texts(789), '')

    async def test_cancel_draft_sends_nothing_to_employee(self):
        await self.message('Доручи Юрію перевірити доставку тестового повідомлення')
        await self.message('ні')
        self.assertIsNone(self.store.get_pending(123))
        self.assertEqual(len(self.store.client.data['Доручення']), 1)
        self.assertEqual(self.texts(456), '')

    async def test_invalid_model_output_does_not_create_task_or_claim_delivery(self):
        self.model.return_value='[]'
        await self.message('Доручи Юрію перевірити доставку тестового повідомлення')
        self.assertIn('Не вдалося розібрати', self.texts(123))
        self.assertIsNone(self.store.get_pending(123))
        self.assertEqual(self.texts(456), '')
