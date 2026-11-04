import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import RateLimitError
from agent_fixture import Harness, call
from api_errors import quota_notice


def quota_error(code='credit_balance_exhausted', kind='insufficient_quota'):
    return RateLimitError('private provider diagnostic',
        response=httpx.Response(429, request=httpx.Request('POST', 'https://api.openai.com/v1/responses')),
        body={'code': code, 'type': kind, 'message': 'private provider diagnostic'})


class QuotaFailures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()

    async def asyncTearDown(self):
        self.h.close()

    async def test_photo_and_followups_survive_exhausted_balance_without_invoice_write(self):
        before = copy.deepcopy(self.h.store.client.data['Рахунки'])
        c = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=quota_error())))
        with patch.object(self.h.app, 'openai', c):
            for n, fields in enumerate([
                {'photo': [{'file_id': 'saved-photo', 'file_size': 100}]},
                {'text': 'На оплату'}, {'text': 'Проект мет обробка'}, {'text': '/start'}
            ]):
                await self.h.app.handle_message({'update_id': n+100, 'message': {
                    'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, **fields}})
        state = self.h.store.get_dialogue(123)
        self.assertEqual(state['photos'][0]['file_id'], 'saved-photo')
        self.assertEqual(len(state['turns']), 4)
        self.assertEqual(before, self.h.store.client.data['Рахунки'])
        self.assertEqual(self.h.texts(123).count('закінчився баланс OpenAI API'), 4)
        self.assertNotIn('private provider diagnostic', self.h.texts(123))

    async def test_failure_after_tool_preserves_delivery_evidence_without_replay(self):
        response = SimpleNamespace(output=[call('send_invoices', {'ids': ['I0']})],
                                   output_text='', status='completed', usage=None)
        c = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=[response, quota_error()])))
        with patch.object(self.h.app, 'openai', c):
            await self.h.app.handle_message({'update_id': 10, 'message': {
                'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': 'Рахунки'}})
        self.assertEqual(len([m for m, _, _ in self.h.sent if m == 'sendDocument']), 1)
        evidence = self.h.store.get_dialogue(123)['turns'][-1]['tools'][-1]['result']
        self.assertEqual(evidence['delivered'], ['I0'])
        self.assertIn('закінчився баланс', self.h.texts(123))

    async def test_confirmation_keeps_committed_result_when_explanation_fails(self):
        r = self.h.runtime()
        d = await r.prepare_project_request('Тест новий', 'test')
        with patch.object(self.h.app, 'run_agent', AsyncMock(side_effect=quota_error())):
            await self.h.app.handle_callback({'update_id': 20, 'callback_query': {
                'id': 'cb', 'from': {'id': 123}, 'message': {'chat': {'id': 123, 'type': 'private'}},
                'data': 'act:yes:' + d['draft_id']}})
        p = self.h.store.get_pending(123)['payload']
        self.assertTrue(p['completed'])
        self.assertIn('закінчився баланс', self.h.texts(123))
        self.assertIn('могла вже виконатися', self.h.texts(123))
        self.assertEqual(await r.confirm(d['draft_id'], True), p['result'])

    def test_rate_limit_is_not_misreported_as_empty_balance(self):
        self.assertIsNone(quota_notice(quota_error('rate_limit_exceeded', 'tokens')))
        self.assertIsNone(quota_notice(RuntimeError('credit_balance_exhausted')))
        notice = quota_notice(quota_error('insufficient_quota'))
        self.assertIn('бюджет або квоту', notice)
        self.assertNotIn('закінчився баланс', notice)


if __name__ == '__main__':
    unittest.main()
