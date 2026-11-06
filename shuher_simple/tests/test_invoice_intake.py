import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from openai import BadRequestError

from agent import run_agent
from agent_fixture import Harness, call, client
from invoice_intake import InvoiceIntake
from payable_invoices import payable_rows


def args(fid='pdf1', **changes):
    return dict(file_id=fid,supplier='Демо',invoice_no='25',amount='3449,88',currency='UAH',
                document_type='Рахунок',concerns=[],**changes)


class InvoiceCards(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=Harness();self.r=self.h.runtime(456)
        self.r.state['documents']=[{'file_id':'pdf1','file_name':'one.pdf','unique_id':'unique1'}]
        await self.r.save()
        self.flow=InvoiceIntake(self.r)

    async def asyncTearDown(self):self.h.close()

    def owner_media(self):
        return [(m,p) for m,p,_ in self.h.sent if m in ('sendPhoto','sendDocument') and p['chat_id']==123]

    async def test_single_project_click_commits_and_sends_without_model_confirmation(self):
        result=await self.flow.offer(**args())
        self.assertFalse(self.owner_media())
        self.assertIsNone(self.h.store.get_pending(456))
        card=await self.flow.load(result['card_id'])
        callback={'id':'cb','data':f'inv:{card["token"]}:p:0','from':{'id':456},
                  'message':{'message_id':card['message_id'],'chat':{'id':456,'type':'private'}}}
        with patch.object(self.h.app,'run_agent',AsyncMock(side_effect=AssertionError('Must not call model'))):
            await self.h.app.handle_callback({'update_id':24,'callback_query':callback})
            await self.h.app.handle_callback({'update_id':25,'callback_query':callback})
        self.assertEqual(len(self.owner_media()),1)
        self.assertEqual(self.owner_media()[0][1]['caption'],'Катана\nТестовий проєкт\nШакін')
        self.assertEqual(len(self.h.store.read_table('Рахунки')[1]),4)
        self.assertTrue((await self.flow.load(card['token']))['complete'])
        self.assertNotIn('Підтверд',self.h.texts(456))

    async def test_unknown_price_sends_only_file_and_keeps_concerns_in_ledger(self):
        a=args();a.update(amount='',supplier='',document_type='Замовлення')
        result=await self.flow.offer(**a)
        await self.flow.select(result['card_id'],0)
        self.assertEqual(len(self.owner_media()),1)
        notices=[p for m,p,_ in self.h.sent if m=='sendMessage' and p['chat_id']==123]
        self.assertEqual(notices,[])
        self.assertEqual(len([p for m,p,_ in self.h.sent if p.get('chat_id')==123]),1)
        self.assertNotIn('суму',self.h.texts(456))
        headers,rows=self.h.store.read_table('Рахунки');row=dict(zip(headers,rows[-1]))
        self.assertEqual(row['Сума'],'');self.assertEqual(row['Тип документа'],'Замовлення')
        self.assertIn('суму',row['Коментар'])
        self.assertIn(row['ID'],[x['ID'] for x in payable_rows(headers,rows)])

    async def test_batch_cards_keep_files_and_projects_separate_after_restart(self):
        self.h.store.client.data['Проєкти'].append(['P2','','Інший проєкт','Активний',''])
        first=await self.flow.offer(**args())
        r=self.h.runtime(456);r.state['photos']=[{'file_id':'photo2','unique_id':'u2'}];await r.save()
        second=await InvoiceIntake(r).offer(**args('photo2'))
        self.h.store.clear_dialogue(456)
        resumed=InvoiceIntake(self.h.runtime(456))
        await resumed.select(second['card_id'],1)
        await resumed.select(first['card_id'],0)
        media=self.owner_media()
        self.assertEqual([m for m,p in media],['sendPhoto','sendDocument'])
        self.assertEqual(media[0][1]['photo'],'photo2')
        self.assertIn('Інший проєкт',media[0][1]['caption'])
        self.assertEqual(media[1][1]['document'],'pdf1')

    async def test_reupload_same_telegram_identity_does_not_duplicate(self):
        first=await self.flow.offer(**args());await self.flow.select(first['card_id'],0)
        r=self.h.runtime(456)
        r.state['documents']=[{'file_id':'pdfNew','file_name':'renamed.pdf','unique_id':'unique1'}]
        second=await InvoiceIntake(r).offer(**args('pdfNew'))
        self.assertTrue(second['already_submitted'])
        self.assertEqual(first['card_id'],second['card_id'])
        self.assertEqual(len(self.owner_media()),1)

    async def test_old_partial_delivery_completes_without_resending_or_warning(self):
        a=args();a['amount']='';result=await self.flow.offer(**a)
        await self.flow.select(result['card_id'],0)
        card=await self.flow.load(result['card_id'])
        card['complete']=False  # Previously delivered file, old warning step unfinished.
        await self.flow.save(card)
        sent_before=len(self.h.sent)
        await self.flow.select(result['card_id'],0)
        self.assertEqual(len(self.h.sent),sent_before)
        self.assertTrue((await self.flow.load(result['card_id']))['complete'])
        self.assertEqual(len(self.h.store.read_table('Рахунки')[1]),4)

    async def test_other_user_and_revoked_permission_cannot_submit(self):
        result=await self.flow.offer(**args())
        with self.assertRaises(ValueError):await InvoiceIntake(self.h.runtime(789)).select(result['card_id'],0)
        self.h.store.client.data['_Shuher_Access']=[r for r in self.h.store.client.data['_Shuher_Access']
                                                   if not (r[0]=='456' and r[1]=='Рахунки')]
        with self.assertRaises(ValueError):await self.flow.select(result['card_id'],0)
        self.assertFalse(self.owner_media())

    async def test_model_card_has_no_duplicate_text_response(self):
        c=client(([call('offer_invoice_projects',args())],''),([], 'Long repeated card text'))
        answer=await run_agent(c,'gpt-6.1-sol',self.r,'На оплату')
        self.assertIsNone(answer)
        self.assertEqual(len([m for m,p,f in self.h.sent if p.get('chat_id')==456]),1)
        self.assertEqual(self.h.texts(456),'')

    async def test_empty_model_answer_after_card_is_success_not_error(self):
        c=client(([call('offer_invoice_projects',args())],''),([], ''))
        with patch.object(self.h.app,'openai',c):
            await self.h.app.handle_message({'update_id':70,'message':{
                'from':{'id':456},'chat':{'id':456,'type':'private'},'text':'На оплату'}})
        self.assertEqual(self.h.texts(456),'')
        self.assertEqual(len([m for m,p,f in self.h.sent if m=='sendDocument']),1)

    async def test_pdf_content_reaches_model_but_not_persisted_state(self):
        c=client(([call('inspect_documents',{'ids':['pdf1']})],''),
                 ([call('offer_invoice_projects',args())],''),([], 'Готово'))
        with patch('agent_tools.telegram_file_bytes',AsyncMock(return_value=b'%PDF-synthetic')):
            await run_agent(c,'gpt-6.1-sol',self.r,'На оплату')
        transcript=c.responses.calls[1]['input']
        parts=[p for x in transcript if isinstance(x.get('content'),list) for p in x['content']]
        self.assertTrue(any(p.get('type')=='input_file' for p in parts))
        self.assertNotIn('base64',json.dumps(self.h.store.get_dialogue(456)))

    async def test_rejected_pdf_still_gets_project_buttons_without_worker_questions(self):
        responses=[SimpleNamespace(output=[call('inspect_documents',{'ids':['pdf1']})],output_text='',status='completed',usage=None),
            BadRequestError('Invalid PDF',response=httpx.Response(400,request=httpx.Request('POST','https://api.openai.com/v1/responses')),body={'code':'invalid_file'}),
            SimpleNamespace(output=[call('offer_invoice_projects',{**args(),'amount':'','concerns':['PDF не прочитано.']})],output_text='',status='completed',usage=None),
            SimpleNamespace(output=[],output_text='',status='completed',usage=None)]
        c=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=responses)))
        with patch('agent_tools.telegram_file_bytes',AsyncMock(return_value=b'%PDF-damaged')):
            self.assertIsNone(await run_agent(c,'gpt-6.1-sol',self.r,'На оплату'))
        self.assertEqual(self.h.texts(456),'')
        self.assertEqual(len([m for m,p,f in self.h.sent if m=='sendDocument']),1)

    async def test_uploaded_photo_keyboard_is_encoded_for_telegram_multipart(self):
        bodies=[]
        def respond(request):
            bodies.append(request.content)
            return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
        transport=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        markup={'inline_keyboard':[[{'text':'Проєкт','callback_data':'inv:token:p:0'}]]}
        with patch('app.httpx.AsyncClient',return_value=transport):
            await self.h.real_bot('sendPhoto',{'chat_id':456,'reply_markup':markup},
                                  {'photo':('invoice.jpg',b'fake-image','image/jpeg')})
        self.assertIn(json.dumps(markup,ensure_ascii=False).encode(),bodies[0])

    async def test_album_photo_is_not_silently_queued_until_another_message(self):
        c=client(([call('offer_invoice_projects',args('album1'))],''),([], 'Готово'))
        with patch.object(self.h.app,'openai',c):
            await self.h.app.handle_message({'update_id':30,'message':{
                'from':{'id':456},'chat':{'id':456,'type':'private'},'media_group_id':'album',
                'photo':[{'file_id':'album1','file_unique_id':'u-album'}]}})
        self.assertEqual(len(c.responses.calls),2)
        self.assertEqual(len([p for m,p,_ in self.h.sent if m=='sendPhoto']),1)

    async def test_failed_delivery_does_not_report_success(self):
        result=await self.flow.offer(**args());self.h.failed.add(123)
        with self.assertRaises(RuntimeError):await self.flow.select(result['card_id'],0)
        self.assertFalse((await self.flow.load(result['card_id']))['complete'])
        self.h.failed.clear();await self.flow.select(result['card_id'],0)
        self.assertEqual(len(self.owner_media()),1)
