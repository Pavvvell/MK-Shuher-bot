import copy
import unittest
from unittest.mock import patch
from agent_fixture import Harness
from agent_tools import AgentTools
from dialogue import new_dialogue, normalize_dialogue
from invoice_updates import prepare_invoice_update,commit_invoice_update

class Tools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.h=Harness();self.r=self.h.runtime()
    async def asyncTearDown(self):self.h.close()
    async def test_read_only_and_write_only_permissions(self):
        self.h.store.grant(456,'Заявки','write_only',123)
        r=self.h.runtime(456)
        with self.assertRaises(ValueError):await r.read_records('Заявки','',[],0,10)
        self.assertIn('Найменування',(await r.table_schema('Заявки'))['columns'])
        with self.assertRaises(ValueError):await r.read_records('_Shuher_Users','',[],0,10)
        self.h.store.grant(456,'Заявки','read',123)
        with self.assertRaises(ValueError):await r.prepare_record('Заявки',[{'name':'Найменування','value':'a'}],[],[])
    async def test_files_failure_keeps_other_invoices_and_no_ledger_write(self):
        before=copy.deepcopy(self.h.store.client.data['Рахунки'])
        async def fake(row,*args):
            if row['ID']=='I1':raise ValueError('missing')
            return 1
        with patch('agent_tools.send_invoice',fake):result=await self.r.send_invoices([])
        self.assertEqual(result['delivered'],['I0','I2']);self.assertEqual(result['failed'],['I1'])
        self.assertEqual(before,self.h.store.client.data['Рахунки'])
    async def test_stock_overspend_is_checked_again_at_confirmation(self):
        d=await self.r.prepare_record('Склад',[{'name':k,'value':v} for k,v in {
            'Найменування':'болти М8','Кількість':'101','Од. вим.':'шт','Тип операції':'Видача'}.items()],[],[])
        with self.assertRaisesRegex(ValueError,'Недостатньо'):await self.r.confirm(d['draft_id'],True)
        self.assertEqual(len(self.h.store.client.data['Склад']),2)
    async def test_selected_invoice_cannot_expand_to_unknown_records(self):
        with self.assertRaises(ValueError):await self.r.prepare_invoice_payment(['I0'],'Оплачено','')
    async def test_stale_invoice_batch_is_atomic(self):
        changes=prepare_invoice_update(self.h.store,['I0','I1'],'На оплаті','завтра')
        headers,_,row=self.h.store._lookup('Рахунки','I1')
        row[headers.index('Сума')]='999'
        self.h.store.client.data['Рахунки'][2]=row
        before=copy.deepcopy(self.h.store.client.data['Рахунки'])
        with self.assertRaises(ValueError):commit_invoice_update(self.h.store,changes)
        self.assertEqual(before,self.h.store.client.data['Рахунки'])
    async def test_duplicate_ids_cannot_double_count_update(self):
        with self.assertRaises(ValueError):prepare_invoice_update(self.h.store,['I0','I0'],'На оплаті','')
    async def test_draft_replacement_requires_explicit_cancellation(self):
        first=await self.r.prepare_project_request('Нове місце','Тест')
        with self.assertRaises(ValueError):await self.r.prepare_project_request('Інше місце','Тест')
        self.assertEqual(self.h.store.get_pending(123)['payload']['token'],first['draft_id'])
        await self.r.cancel_draft()
        second=await self.r.prepare_project_request('Інше місце','Тест')
        with self.assertRaises(ValueError):await self.r.confirm(first['draft_id'],True)
        self.assertNotEqual(first['draft_id'],second['draft_id'])
    async def test_no_silent_truncated_confirmation(self):
        with self.assertRaises(ValueError):await self.r.draft('Заявки',{'action':'append'},'x'*3501)
        self.assertIsNone(self.h.store.get_pending(123))
    async def test_invoice_attachments_must_belong_to_actor(self):
        with self.assertRaises(ValueError):self.r.attachments(['someone-elses-photo'],[])
    async def test_csv_only_to_authorized_current_user(self):
        result=await self.r.export_table('Заявки')
        self.assertTrue(result['sent'])
        m,p,f=self.h.sent[-1];self.assertEqual(m,'sendDocument');self.assertEqual(p['chat_id'],123)
        self.assertIn('document',f)
    async def test_report_permission_failure_is_not_false_zero(self):
        read=self.h.store.read_table
        def fail(table,*args):
            if table=='Заявки':raise TimeoutError('synthetic')
            return read(table,*args)
        with patch.object(self.h.store,'read_table',fail):result=await self.r.overview('')
        self.assertIn('Заявки: не вдалося',result['report'])
        self.assertNotIn('Заявки: 0',result['report'])
    async def test_complete_context_migrates_without_shared_memory(self):
        s=new_dialogue();s['history']=[{'role':'user','content':'Проєкт Тестовий'},{'role':'assistant','content':'Прийняв'}]
        n=normalize_dialogue(s)
        self.assertEqual(n['turns'][0]['user'],'Проєкт Тестовий')
        self.assertEqual(n['turns'][0]['tools'],[])
    async def test_oversized_tool_history_retains_current_user_and_last_result(self):
        s=new_dialogue();s['turns']=[{'user':'покажи','answer':'видано','tools':[
            {'tool':'old','result':'x'*30000},{'tool':'send_invoices','result':{'delivered':['I0']}}]}]
        n=normalize_dialogue(s)
        self.assertEqual(n['turns'][-1]['tools'][-1]['result']['delivered'],['I0'])
        self.assertEqual(n['turns'][-1]['omitted_tool_results'],1)
