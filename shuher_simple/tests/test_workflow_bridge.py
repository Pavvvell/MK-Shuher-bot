import asyncio, json, unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import HTTPException
from agent_fixture import Harness
from sheet_fixtures import make_store
from workflow_bridge import WorkflowBridge, Notice, STAGING, INBOX, OUTBOX, now

def update(uid=123):
    return {'update_id':800,'message':{'from':{'id':uid},'chat':{'id':uid,'type':'private'},
        'message_id':12,'text':'=IMPORTXML("untrusted")','document':{'file_id':'f1','file_unique_id':'u1','mime_type':'application/pdf'}}}

class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=Harness();self.master=make_store();self.master.id='master'
        self.cfg=SimpleNamespace(codex_owner_id=123,codex_bridge_secret='x'*40,
            telegram_webhook_secret='test',openai_model='test',codex_test_delivery=False,codex_live_delivery=False)
        self.bp=patch.object(self.h.app,'settings',self.cfg);self.bp.start()
        self.b=WorkflowBridge(self.h.app,self.master)
    async def asyncTearDown(self):self.bp.stop();self.h.close()
    def notice(self,**kw):
        return Notice(**({'kind':'mail','source_id':'gmail123','text':'Потрібна відповідь.',
            'expires_at':now()+timedelta(hours=1)}|kw))
    async def queue_test(self):
        self.cfg.codex_test_delivery=True
        return (await self.b.enqueue(self.notice(mode='test')))['key']
    async def test_private_owner_only_attachments_and_dedup(self):
        await self.b.capture(update(456));u=update();u['message']['chat']['type']='group';await self.b.capture(u)
        self.assertNotIn(STAGING,self.h.store.titles())
        await self.b.capture(update());await self.b.capture(update())
        rows=self.h.store.data(STAGING);self.assertEqual(len(rows),2)
        item=json.loads(rows[1][1]);self.assertEqual(item['attachments'][0]['file_id'],'f1')
        self.assertEqual(item['text'],'=IMPORTXML("untrusted")')
    async def test_stage_ignores_master_outage_and_sync_is_idempotent(self):
        with patch.object(self.master,'titles',side_effect=RuntimeError('down')):await self.b.capture(update())
        self.assertEqual(await self.b.sync(),{'exported':1});self.assertEqual(await self.b.sync(),{'exported':0})
        result=await self.b.inbox(0,100);self.assertEqual(result['next_cursor'],1)
        self.assertEqual((await self.b.inbox(1,100))['events'],[])
    async def test_export_lost_ack_does_not_duplicate(self):
        await self.b.capture(update())
        with patch.object(self.h.store,'update',side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):await self.b.sync()
        await self.b.sync();self.assertEqual(len(self.master.data(INBOX)),2)
    async def test_shadow_cannot_send_even_after_live_enabled(self):
        n=self.notice();q=await self.b.enqueue(n);again=await self.b.enqueue(n)
        self.assertTrue(again['duplicate']);self.assertEqual(q['key'],again['key'])
        self.cfg.codex_live_delivery=True;self.cfg.codex_test_delivery=True
        self.assertFalse((await self.b.deliver(q['key']))['sent']);self.assertEqual(self.h.sent,[])
    async def test_mode_gates_and_content_conflict(self):
        for mode in ('live','test'):
            with self.assertRaises(ValueError):await self.b.enqueue(self.notice(mode=mode))
        await self.b.enqueue(self.notice())
        with self.assertRaises(ValueError):await self.b.enqueue(self.notice(text='changed'))
    async def test_concurrent_delivery_and_restart_send_once_to_fixed_owner(self):
        key=await self.queue_test()
        result=await asyncio.gather(self.b.deliver(key),self.b.deliver(key))
        self.assertEqual(sum(x['sent'] for x in result),1);self.assertEqual(len(self.h.sent),1)
        p=self.h.sent[0][1];self.assertEqual(p['chat_id'],123);self.assertTrue(p['text'].startswith('🧪 ТЕСТ · 📬 Пошта'))
        await WorkflowBridge(self.h.app,self.master).deliver(key);self.assertEqual(len(self.h.sent),1)
    async def test_ambiguous_telegram_response_never_retried(self):
        key=await self.queue_test()
        async def ambiguous(*a):await self.h.bot(*a);raise TimeoutError('lost response')
        with patch.object(self.h.app,'bot_call',ambiguous):
            self.assertEqual((await self.b.deliver(key))['status'],'uncertain')
        await self.b.deliver(key);self.assertEqual(len(self.h.sent),1)
    async def test_lost_sent_receipt_blocks_retry(self):
        key=await self.queue_test();original=self.master.update
        def fail(tab,n,row):
            if row[6]=='sent':raise RuntimeError('down')
            return original(tab,n,row)
        with patch.object(self.master,'update',fail):
            with self.assertRaises(RuntimeError):await self.b.deliver(key)
        self.assertEqual((await self.b.deliver(key))['status'],'sending');self.assertEqual(len(self.h.sent),1)
    async def test_claim_failure_sends_nothing(self):
        key=await self.queue_test()
        with patch.object(self.master,'update',side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):await self.b.deliver(key)
        self.assertEqual(self.h.sent,[])
    async def test_expiry_and_invalid_input(self):
        key=await self.queue_test();row=self.master.data(OUTBOX)[1];row[5]=(now()-timedelta(seconds=1)).isoformat()
        self.master.update(OUTBOX,2,row);self.assertEqual((await self.b.deliver(key))['status'],'expired')
        for kw in ({'expires_at':now()-timedelta(seconds=1)},{'expires_at':now().replace(tzinfo=None)},
                   {'kind':'morning','source_id':'bad-date'},{'source_id':'bad/id'}):
            with self.assertRaises(ValueError):await self.b.enqueue(self.notice(**kw))
        self.assertEqual(self.h.sent,[])
    async def test_route_auth_disabled_and_validation(self):
        with self.assertRaises(HTTPException):self.b.authorize('wrong')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app.app),base_url='https://test') as c:
            with patch.object(self.h.app,'workflow_bridge',None):self.assertEqual((await c.get('/workflow/inbox')).status_code,404)
            with patch.object(self.h.app,'workflow_bridge',self.b):
                self.assertEqual((await c.get('/workflow/inbox')).status_code,403)
                await self.b.capture(update());await self.b.sync();headers={'X-Codex-Bridge-Secret':'x'*40}
                self.assertEqual((await c.get('/workflow/inbox',headers=headers)).status_code,200)
                self.assertEqual((await c.get('/workflow/inbox?cursor=-1',headers=headers)).status_code,422)
    async def test_webhook_stages_once_and_preserves_existing_reply_and_auth(self):
        with patch.object(self.h.app,'workflow_bridge',self.b),patch.object(self.h.app,'run_agent',AsyncMock(return_value='Existing reply')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app.app),base_url='https://test') as c:
                self.assertEqual((await c.post('/telegram/webhook',json=update())).status_code,403)
                headers={'X-Telegram-Bot-Api-Secret-Token':'test'}
                self.assertEqual((await c.post('/telegram/webhook',json=update(),headers=headers)).status_code,200)
                self.assertTrue((await c.post('/telegram/webhook',json=update(),headers=headers)).json()['duplicate'])
        self.assertEqual(len(self.h.store.data(STAGING)),2);self.assertEqual(self.h.texts(123),'Existing reply')
        self.assertFalse(any(m=='setWebhook' for m,p,f in self.h.sent))
    async def test_stage_failure_before_model_returns_retryable_503(self):
        with patch.object(self.h.app,'workflow_bridge',self.b),patch.object(self.b,'capture',AsyncMock(side_effect=RuntimeError('down'))),patch.object(self.h.app,'run_agent',AsyncMock()) as model:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app.app),base_url='https://test') as c:
                r=await c.post('/telegram/webhook',json=update(),headers={'X-Telegram-Bot-Api-Secret-Token':'test'})
                self.assertEqual(r.status_code,503)
            model.assert_not_called();self.assertFalse(self.h.store.processed(800))

    async def test_full_shadow_then_test_roundtrip_via_http(self):
        with patch.object(self.h.app,'workflow_bridge',self.b),patch.object(self.h.app,'run_agent',AsyncMock(return_value='OK')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.h.app.app),base_url='https://test') as c:
                h={'X-Codex-Bridge-Secret':'x'*40}
                await c.post('/telegram/webhook',json=update(),headers={'X-Telegram-Bot-Api-Secret-Token':'test'})
                self.assertEqual((await c.post('/workflow/sync',headers=h)).json()['exported'],1)
                self.assertEqual(len((await c.get('/workflow/inbox',headers=h)).json()['events']),1)
                payload=self.notice().model_dump(mode='json')
                q=(await c.post('/workflow/outbox',headers=h,json=payload)).json()
                self.assertEqual(q['status'],'shadow')
                before=len(self.h.sent)
                self.assertFalse((await c.post('/workflow/outbox/'+q['key']+'/deliver',headers=h)).json()['sent'])
                self.assertEqual(len(self.h.sent),before)
                payload['mode']='test'
                self.assertEqual((await c.post('/workflow/outbox',headers=h,json=payload)).status_code,409)
                self.cfg.codex_test_delivery=True
                q=(await c.post('/workflow/outbox',headers=h,json=payload)).json()
                path='/workflow/outbox/'+q['key']+'/deliver'
                self.assertTrue((await c.post(path,headers=h)).json()['sent'])
                self.assertFalse((await c.post(path,headers=h)).json()['sent'])
                self.assertEqual(len(self.h.sent),before+1)

    async def test_notice_has_no_arbitrary_recipient_and_fits_one_message(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):self.notice(chat_id=456)
        with self.assertRaises(ValueError):await self.b.enqueue(self.notice(text='😀'*2500))
        with self.assertRaises(ValueError):await self.b.enqueue(self.notice(kind='morning',source_id='20261106'))

    async def test_morning_keys_deduplicate_per_day_and_differ_from_mail(self):
        first=await self.b.enqueue(self.notice(kind='morning',source_id='2026-11-06'))
        again=await self.b.enqueue(self.notice(kind='morning',source_id='2026-11-06'))
        other=await self.b.enqueue(self.notice())
        self.assertEqual(first['key'],again['key']);self.assertNotEqual(first['key'],other['key'])
        self.assertTrue(self.master.data(OUTBOX)[1][4].startswith('📋 РРД'))
