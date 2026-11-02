import copy
import json
import unittest
from unittest.mock import patch
from agent import run_agent
from agent_schema import SCHEMAS
from agent_fixture import Harness,client,call

class AgentConversation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.h=Harness()
    async def asyncTearDown(self):self.h.close()

    async def test_message_goes_to_model_then_real_tool_then_results_back(self):
        reasoning={'type':'reasoning','id':'reason1','summary':[],'encrypted_content':'opaque'}
        c=client(([reasoning,call('send_invoices',{'ids':[]})],''),([], 'Надіслано 3 рахунки.'))
        r=self.h.runtime()
        answer=await run_agent(c,'gpt-6.1-sol',r,'скинь ще раз всі рахунки')
        self.assertEqual(answer,'Надіслано 3 рахунки.')
        self.assertEqual(c.responses.calls[0]['input'][-1]['content'],'скинь ще раз всі рахунки')
        self.assertIn(reasoning,c.responses.calls[1]['input'])
        output=next(x for x in c.responses.calls[1]['input'] if x.get('type')=='function_call_output')
        self.assertEqual(json.loads(output['output'])['delivered'],['I0','I1','I2'])
        self.assertEqual(len([m for m,p,f in self.h.sent if m=='sendDocument']),3)
        saved=self.h.store.get_dialogue(123)
        self.assertEqual(saved['turns'][-1]['tools'][0]['result']['delivered'],['I0','I1','I2'])

    async def test_invoice_followup_uses_exact_previous_selection_and_needs_click(self):
        r=self.h.runtime()
        c=client(([call('send_invoices',{'ids':[]})],''),([], 'Файли надіслано.'))
        await run_agent(c,'gpt-6.1-sol',r,'скинь рахунки')
        r=self.h.runtime(event=2)
        args={'ids':['I0','I1','I2'],'status':'На оплаті','note':'Очікувана оплата: 03.11.2026 до вечора'}
        c=client(([call('prepare_invoice_payment',args)],''),([], 'Підтвердь ці три рахунки.'))
        before=copy.deepcopy(self.h.store.client.data['Рахунки'])
        await run_agent(c,'gpt-6.1-sol',r,'Я ці всі рахунки кинув на оплату. Оплатять завтра до вечора')
        self.assertEqual(before,self.h.store.client.data['Рахунки'])
        context=json.dumps(c.responses.calls[0]['input'],ensure_ascii=False)
        self.assertIn('I0',context)
        p=self.h.store.get_pending(123)['payload']
        result=await r.confirm(p['token'],True)
        self.assertEqual(result['updated'],3)
        h,rows=self.h.store.read_table('Рахунки')
        self.assertTrue(all(row[h.index('Статус оплати')]=='На оплаті' for row in rows))
        self.assertTrue(all('03.11.2026' in row[h.index('Коментар')] for row in rows))
        self.assertEqual(await r.confirm(p['token'],True),result)

    async def test_model_cannot_commit_even_if_it_invents_a_tool(self):
        c=client(([call('confirm_action',{'accept':True})],''),([], 'Потрібне підтвердження.'))
        await run_agent(c,'gpt-6.1-sol',self.h.runtime(),'так')
        outputs=[x for x in c.responses.calls[1]['input'] if x.get('type')=='function_call_output']
        self.assertFalse(json.loads(outputs[0]['output'])['ok'])
        self.assertNotIn('confirm_action',[x['name'] for x in SCHEMAS])

    async def test_permission_error_returns_to_model_without_private_rows(self):
        c=client(([call('send_invoices',{'ids':[]})],''),([], 'Доступ лише власнику.'))
        await run_agent(c,'gpt-6.1-sol',self.h.runtime(456),'надішли рахунки')
        out=[x for x in c.responses.calls[1]['input'] if x.get('type')=='function_call_output'][0]
        self.assertFalse(json.loads(out['output'])['ok'])
        self.assertEqual([m for m,p,f in self.h.sent if m=='sendDocument'],[])

    async def test_model_exception_after_tool_does_not_erase_effects(self):
        c=client(([call('send_invoices',{'ids':[]})],''))
        with self.assertRaises(IndexError):await run_agent(c,'gpt-6.1-sol',self.h.runtime(),'рахунки')
        self.assertEqual(self.h.store.get_dialogue(123)['turns'][-1]['tools'][0]['result']['delivered'],['I0','I1','I2'])

    async def test_direct_answer_no_tools_and_separate_user_memory(self):
        c=client(([],'Привіт!'))
        await run_agent(c,'gpt-6.1-sol',self.h.runtime(),'привіт')
        self.assertEqual(self.h.store.get_dialogue(456)['turns'],[])
        self.assertEqual(self.h.sent,[])

    async def test_plain_negation_reaches_model_unchanged(self):
        c=client(([],'Не надсилатиму.'))
        with patch.object(self.h.app,'openai',c):
            await self.h.app.handle_message({'update_id':25,'message':{'from':{'id':123},'chat':{'id':123,'type':'private'},'text':'Не доручай Юрію нічого, я лише запитую'}})
        self.assertEqual(c.responses.calls[0]['input'][-1]['content'],'Не доручай Юрію нічого, я лише запитую')
        self.assertIsNone(self.h.store.get_pending(123))
        self.assertEqual(self.h.texts(456),'')

    async def test_topic_switch_preserves_pending_and_media(self):
        self.h.store.save_pending(123,'Заявки',{'kind':'agent','token':'x','action':'append'})
        r=self.h.runtime();r.state['documents']=[{'file_id':'pdf1','file_name':'one.pdf','mime':'application/pdf'}]
        before=self.h.store.get_pending(123)
        c=client(([call('overview',{'project':''})],''),([], 'Є одна заявка.'))
        await run_agent(c,'gpt-6.1-sol',r,'А що взагалі по заявках?')
        self.assertEqual(self.h.store.get_pending(123),before)
        self.assertEqual(len(self.h.store.get_dialogue(123)['documents']),1)
        self.assertEqual(len(self.h.store.client.data['Заявки']),2)

    async def test_bad_arguments_do_not_execute(self):
        c=client(([call('send_staff_message',{'recipient_ids':[456],'text':'oops','unapproved':True})],''),([], 'Уточню.'))
        await run_agent(c,'gpt-6.1-sol',self.h.runtime(),'питання')
        self.assertEqual(self.h.sent,[])

    async def test_strict_schemas_complete(self):
        from jsonschema import Draft202012Validator
        def check(s):
            if s.get('type')=='object':
                self.assertEqual(set(s['required']),set(s['properties']))
                self.assertFalse(s['additionalProperties'])
                for p in s['properties'].values():check(p)
            if s.get('type')=='array':check(s['items'])
        for tool in SCHEMAS:
            Draft202012Validator.check_schema(tool['parameters']);check(tool['parameters'])

    async def test_unregistered_access_does_not_consume_model_or_call_tools(self):
        c=client(([],'unexpected'))
        with patch.object(self.h.app,'openai',c):
            await self.h.app.handle_message({'update_id':99,'message':{'from':{'id':999},'chat':{'id':999,'type':'private'},'text':'розкажи про космос'}})
        self.assertEqual(c.responses.calls,[])
        self.assertIn('Потрібен доступ',self.h.texts(999))
