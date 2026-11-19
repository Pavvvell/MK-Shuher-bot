import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from agent_fixture import Harness, client, call
from agent import run_agent
from master_updates import JOURNAL_HEADERS, row_dict
from sheet_fixtures import API, Req, make_store
from workflow_bridge import WorkflowBridge

HEADERS = ['Task ID', 'Project ID', 'Задача', 'Пріоритет', 'Статус', 'Owner',
           'Дедлайн', 'Контрольна дата', 'Залежить від', 'Чекаємо від',
           'Наступний крок', 'Останній апдейт', 'Нотатки']

class MasterAPI(API):
    def __init__(self):
        super().__init__({'Задачі': [HEADERS, ['T1', '', 'Тестова задача', 'P1', 'В роботі',
            'Тест', '2026-10-20', '2026-10-18', '', 'Підрядник', 'Чекати', '2026-10-17', 'Історія']],
            'Журнал': [JOURNAL_HEADERS]})
        self.batch_count = 0
        self.lose_ack = False
        self.fail_before = False
        self.formula = False
    def get(self, **kw):
        if 'range' in kw: return super().get(**kw)
        def go():
            sheets = []
            for sid, (title, rows) in enumerate(self.data.items(), 1):
                sheet = {'properties': {'title': title, 'sheetId': sid}, 'data': []}
                for ran in kw.get('ranges', []):
                    tab, row, _ = self.loc(ran)
                    if title != tab: continue
                    cells = [{'formattedValue': v, 'userEnteredValue': {'stringValue': v}} for v in rows[row]]
                    cells[4]['dataValidation'] = {'condition': {'type': 'ONE_OF_LIST',
                        'values': [{'userEnteredValue': v} for v in ['В роботі', 'Готово', 'Скасовано', 'Очікування']]}}
                    if self.formula: cells[12]['userEnteredValue'] = {'formulaValue': '=1'}
                    sheet['data'].append({'startRow': row, 'rowData': [{'values': cells}]})
                sheets.append(sheet)
            return {'sheets': sheets}
        return Req(go)
    def batchUpdate(self, **kw):
        def go():
            if self.fail_before: raise RuntimeError('before commit')
            data = copy.deepcopy(self.data)
            names = list(data)
            for req in kw['body']['requests']:
                if 'updateCells' in req:
                    u = req['updateCells']; start = u['start']
                    row = data[names[start['sheetId']-1]][start['rowIndex']]
                    row[start['columnIndex']] = u['rows'][0]['values'][0]['userEnteredValue']['stringValue']
                else:
                    u = req['appendCells']
                    data[names[u['sheetId']-1]].extend([[v['userEnteredValue']['stringValue'] for v in r['values']] for r in u['rows']])
            self.data = data
            self.batch_count += 1
            if self.lose_ack:
                self.lose_ack = False
                raise TimeoutError('committed but response lost')
            return {}
        return Req(go)

class MasterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.master = make_store(); self.master.id = 'master'; self.master.client = MasterAPI()
        cfg = SimpleNamespace(codex_owner_id=123, codex_bridge_secret='x'*40, telegram_webhook_secret='test')
        self.patches = [patch.object(self.h.app, 'settings', cfg)]
        self.patches[0].start()
        self.bridge = WorkflowBridge(self.h.app, self.master)
        self.patches.append(patch.object(self.h.app, 'workflow_bridge', self.bridge)); self.patches[-1].start()
        self.rt = self.h.runtime()
        self.updates = [{'table':'Задачі', 'id':'T1', 'changes':[{'field':'Статус','value':'Готово'}]}]
    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.h.close()
    async def read(self): return await self.rt.execute('workflow_read_master', {'table':'Задачі','offset':0,'limit':10})
    async def write(self): return await self.rt.execute('workflow_update_master', {'updates':self.updates,'reason':'Власник підтвердив виконання.'})
    async def test_close_clears_reminders_preserves_notes_and_checks_readback(self):
        await self.read(); result = await self.write()
        self.assertTrue(result['verified'])
        row = row_dict(HEADERS, self.master.data('Задачі')[1])
        self.assertEqual(row['Статус'], 'Готово')
        for f in ['Дедлайн','Контрольна дата','Чекаємо від']: self.assertEqual(row[f], '')
        self.assertEqual(row['Нотатки'], 'Історія')
        self.assertEqual(self.master.client.batch_count, 1)
        self.assertEqual(len(self.master.data('Журнал')), 2)
        self.assertEqual(self.h.sent, [])
    async def test_no_read_no_write(self):
        with self.assertRaises(ValueError): await self.write()
        self.assertEqual(self.master.client.batch_count, 0)
    async def test_nonowner_and_owner_group_denied(self):
        for uid, chat in [(456,456),(123,-1)]:
            rt = self.h.runtime(uid); rt.chat = chat
            with self.assertRaises(ValueError): await rt.workflow_update_master(self.updates,'test')
    async def test_stale_read_rejected_then_fresh_read_works(self):
        await self.read(); self.master.client.data['Задачі'][1][12] = 'Змінено зовні'
        with self.assertRaises(ValueError): await self.write()
        await self.read(); self.assertTrue((await self.write())['verified'])
        self.assertEqual(self.master.data('Задачі')[1][12], 'Змінено зовні')
    async def test_lost_ack_retry_uses_receipt_once(self):
        await self.read(); self.master.client.lose_ack = True
        with self.assertRaises(TimeoutError): await self.write()
        self.rt = self.h.runtime()
        result = await self.write()
        self.assertTrue(result['verified']); self.assertTrue(result['duplicate'])
        self.assertEqual(self.master.client.batch_count, 1)
    async def test_failed_batch_no_success_or_partial_changes(self):
        await self.read(); self.master.client.fail_before = True
        with self.assertRaises(RuntimeError): await self.write()
        self.assertEqual(self.master.data('Задачі')[1][4], 'В роботі')
        self.assertEqual(len(self.master.data('Журнал')), 1)
    async def test_replay_does_not_override_newer_status(self):
        await self.read(); await self.write()
        self.master.client.data['Задачі'][1][4] = 'В роботі'
        result = await self.write()
        self.assertFalse(result['verified']); self.assertTrue(result['duplicate'])
        self.assertEqual(self.master.client.batch_count, 1)
    async def test_invalid_status_and_formula_rejected(self):
        await self.read(); self.updates[0]['changes'][0]['value'] = 'Пауза'
        with self.assertRaises(ValueError): await self.write()
        self.updates[0]['changes'] = [{'field':'Нотатки','value':'перезаписати'}]
        self.master.client.formula = True
        with self.assertRaises(ValueError): await self.write()
        self.assertEqual(self.master.client.batch_count, 0)
    async def test_missing_second_record_prevents_whole_batch(self):
        await self.read(); self.updates.append({'table':'Задачі','id':'missing','changes':[{'field':'Статус','value':'Готово'}]})
        with self.assertRaises(ValueError): await self.write()
        self.assertEqual(self.master.data('Задачі')[1][4], 'В роботі')
    async def test_model_tool_loop_closes_without_confirmation_ui(self):
        c = client(([call('workflow_read_master',{'table':'Задачі','offset':0,'limit':10})],''),
            ([call('workflow_update_master',{'updates':self.updates,'reason':'Задачу завершено.'},'c2')],''),
            ([], 'Закрив тестову задачу.'))
        answer = await run_agent(c,'test',self.rt,'Цю тестову задачу вже зробили, закрий.')
        self.assertEqual(answer,'Закрив тестову задачу.')
        self.assertEqual(self.master.data('Задачі')[1][4],'Готово')
        self.assertFalse(self.rt.ui_presented)
        self.assertEqual(self.h.sent,[])

if __name__ == '__main__': unittest.main()
