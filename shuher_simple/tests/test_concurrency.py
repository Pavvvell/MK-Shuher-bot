import asyncio
import importlib
import os
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from concurrency import KeyedQueues, SheetsWorker, user_queue_key
from store import request_reads


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_fifo_other_key_proceeds_and_registry_cleans_up(self):
        q=KeyedQueues();started=asyncio.Event();release=asyncio.Event();order=[]
        async def first():
            async with q.hold(1):
                started.set();await release.wait();order.append(1)
        async def next_item(key,value):
            async with q.hold(key):order.append(value)
        a=asyncio.create_task(first());await started.wait()
        b=asyncio.create_task(next_item(1,2));await asyncio.sleep(0)
        c=asyncio.create_task(next_item(1,3));await asyncio.sleep(0)
        await next_item(2,4)
        self.assertEqual(order,[4])
        release.set();await asyncio.gather(a,b,c)
        self.assertEqual(order,[4,1,2,3]);self.assertEqual(q.entries,{})

    async def test_cancelled_waiter_and_failed_holder_release_keys(self):
        q=KeyedQueues()
        async with q.hold(1):
            async def wait():
                async with q.hold(1):self.fail('cancelled waiter entered')
            task=asyncio.create_task(wait());await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(q.entries[1][1],1)
        with self.assertRaises(ValueError):
            async with q.hold(1):raise ValueError
        self.assertEqual(q.entries,{})

    def test_callback_and_messages_use_same_user_even_across_chats(self):
        message={'update_id':1,'message':{'from':{'id':123},'chat':{'id':1}}}
        callback={'update_id':2,'callback_query':{'from':{'id':123}}}
        self.assertEqual(user_queue_key(message),user_queue_key(callback))

    async def test_cancelled_worker_call_does_not_allow_overlapping_sheet_operations(self):
        worker=SheetsWorker();entered=threading.Event();release=threading.Event();seen=[]
        def first():
            entered.set();release.wait(2);seen.append(('first',threading.get_ident()))
        def second():seen.append(('second',threading.get_ident()))
        try:
            a=asyncio.create_task(worker.run(first))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            a.cancel()
            with self.assertRaises(asyncio.CancelledError):await a
            b=asyncio.create_task(worker.run(second));await asyncio.sleep(.02)
            self.assertEqual(seen,[])
            release.set();await b
            self.assertEqual([x[0] for x in seen],['first','second'])
            self.assertEqual(len({x[1] for x in seen}),1)
        finally:release.set();worker.close()


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env={'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_WEBHOOK_SECRET':'test',
             'OPENAI_API_KEY':'test','SPREADSHEET_ID':'test','OWNER_IDS':'123'}
        with patch.dict(os.environ,env),patch('store.SheetsStore'),patch('openai.AsyncOpenAI'):
            self.app=importlib.import_module('app')
        self.worker=SheetsWorker();self.marked=set()
        self.fake=Mock();self.fake.processed.side_effect=lambda uid:uid in self.marked
        self.fake.mark_processed.side_effect=self.marked.add
        self.patches=[patch.object(self.app,'queues',KeyedQueues()),
                      patch.object(self.app,'sheet_worker',self.worker),
                      patch.object(self.app,'store',self.fake)]
        for p in self.patches:p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        self.worker.close()

    async def call(self,uid,update,callback=False):
        body={'update_id':update,
              'callback_query' if callback else 'message':{'from':{'id':uid}}}
        request=Mock();request.json=AsyncMock(return_value=body)
        return await self.app.webhook(request,'test')

    async def test_other_user_finishes_while_first_waits_and_same_user_callback_waits(self):
        started=asyncio.Event();release=asyncio.Event();seen=[]
        async def message(body):
            uid=body['message']['from']['id']
            if uid==1:started.set();await release.wait()
            seen.append(uid)
        async def callback(body):seen.append('callback')
        with patch.object(self.app,'handle_message',message),patch.object(self.app,'handle_access_callback',callback):
            a=asyncio.create_task(self.call(1,11));await started.wait()
            b=asyncio.create_task(self.call(1,12,True));await asyncio.sleep(0)
            await asyncio.wait_for(self.call(2,13),1)
            self.assertEqual(seen,[2]);self.assertFalse(b.done())
            release.set();await asyncio.gather(a,b)
        self.assertEqual(seen,[2,1,'callback'])
        self.assertEqual(self.marked,{11,12,13})
        self.assertEqual(self.app.queues.entries,{})

    async def test_concurrent_duplicate_update_runs_handler_once(self):
        handler=AsyncMock()
        with patch.object(self.app,'handle_message',handler):
            results=await asyncio.gather(self.call(1,14),self.call(1,14),self.call(2,14))
        self.assertEqual(handler.await_count,1)
        self.assertEqual(sum(r.get('duplicate',False) for r in results),2)

    async def test_failure_releases_user_queue_and_does_not_mark_update(self):
        with patch.object(self.app,'handle_message',AsyncMock(side_effect=RuntimeError)):
            with self.assertRaises(Exception):await self.call(1,15)
        self.assertNotIn(15,self.marked);self.assertEqual(self.app.queues.entries,{})
        with patch.object(self.app,'handle_message',AsyncMock()):await self.call(1,15)
        self.assertIn(15,self.marked)

    async def test_permission_is_rechecked_inside_serialized_commit(self):
        mutation=Mock()
        with patch.object(self.app,'permission',return_value={}):
            with self.assertRaises(ValueError):
                await self.app.write_as(456,'Склад',mutation)
        mutation.assert_not_called()
        with patch.object(self.app,'permission',return_value={'Склад':'write_only'}):
            with self.assertRaises(ValueError):
                await self.app.write_as(456,'Склад',mutation,require_read=True)
            await self.app.write_as(456,'Склад',mutation)
        mutation.assert_called_once()

    async def test_notification_scans_cannot_overlap(self):
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def scan():
            calls.append('scan');started.set();await release.wait();return 1
        with patch.object(self.app,'_sync_task_notifications',scan):
            a=asyncio.create_task(self.app.sync_task_notifications());await started.wait()
            b=asyncio.create_task(self.app.sync_task_notifications());await asyncio.sleep(0)
            self.assertEqual(calls,['scan']);release.set();await asyncio.gather(a,b)
        self.assertEqual(calls,['scan','scan'])
