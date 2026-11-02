"""Real task tools and notifications with synthetic storage/Telegram."""
import unittest
from agent_fixture import Harness

class TaskDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=Harness();self.h.store.client.data['Доручення']=self.h.store.client.data['Доручення'][:1]
    async def asyncTearDown(self):self.h.close()
    async def draft(self,uid=123,assignee='Yuriy'):
        r=self.h.runtime(uid)
        d=await r.prepare_record('Доручення',[{'name':'Завдання','value':'ТЕСТ — виконувати не потрібно'},
                   {'name':'Виконавець','value':assignee}],[],[])
        return r,d
    async def test_task_confirmation_delivery_and_idempotency(self):
        r,d=await self.draft()
        self.assertEqual(self.h.texts(456),'')
        result=await r.confirm(d['draft_id'],True)
        self.assertIn('Telegram прийняв',result['notification'])
        self.assertIn('Нове доручення',self.h.texts(456))
        await r.confirm(d['draft_id'],True)
        self.assertEqual(len(self.h.store.client.data['Доручення']),2)
        self.assertEqual(await self.h.app.sync_task_notifications(),0)
    async def test_failed_delivery_then_retry(self):
        self.h.failed.add(456);r,d=await self.draft()
        result=await r.confirm(d['draft_id'],True)
        self.assertIn('не доставлено',result['notification'])
        self.assertEqual((await r.retry_task_notifications())['undelivered'],1)
        self.h.failed.clear()
        self.assertEqual((await r.retry_task_notifications())['telegram_accepted'],1)
        self.assertEqual(await self.h.app.sync_task_notifications(),0)
    async def test_partial_direct_message_failure_and_dedup(self):
        self.h.failed.add(456);r=self.h.runtime()
        result=await r.send_staff_message([456,789],'ТЕСТ — виконувати не потрібно')
        self.assertEqual(result['failed'],[456]);self.assertEqual(result['telegram_accepted'],[789])
        self.h.failed.clear()
        await r.send_staff_message([456,789],'ТЕСТ — виконувати не потрібно')
        self.assertEqual(self.h.texts(789).count('ТЕСТ'),1)
    async def test_assignee_revocation_rechecked_at_commit(self):
        r,d=await self.draft();self.h.store.grant(456,'Доручення','none',123)
        with self.assertRaises(ValueError):await r.confirm(d['draft_id'],True)
        self.assertEqual(len(self.h.store.client.data['Доручення']),1)
    async def test_employee_cannot_delegate_or_message(self):
        with self.assertRaises(ValueError):await self.draft(456)
        with self.assertRaises(ValueError):await self.h.runtime(456).send_staff_message([789],'Тест')
        with self.assertRaises(ValueError):await self.h.runtime(456).retry_task_notifications()
    async def test_cancel_no_delivery_and_stale_button_cannot_commit(self):
        r,d=await self.draft();await r.confirm(d['draft_id'],False)
        with self.assertRaises(ValueError):await r.confirm(d['draft_id'],True)
        self.assertEqual(self.h.texts(456),'')
    async def test_unknown_or_ambiguous_assignee_refused(self):
        for name in ('невідомий','Yuriy та Sergey'):
            with self.assertRaises(ValueError):await self.draft(assignee=name)
    async def test_other_user_cannot_use_owners_token(self):
        _,d=await self.draft()
        with self.assertRaises(ValueError):await self.h.runtime(456).confirm(d['draft_id'],True)
