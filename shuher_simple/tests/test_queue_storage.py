import asyncio
import unittest
from concurrency import SheetsWorker
from projects import decide_project
from store import request_reads, GRANTS
from sheet_fixtures import make_store


class StorageConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker=SheetsWorker();self.store=make_store()

    async def asyncTearDown(self):self.worker.close()

    async def test_two_cached_stock_issues_cannot_overspend(self):
        ready=0;gate=asyncio.Event()
        async def issue(rid):
            nonlocal ready
            with request_reads():
                await self.worker.run(self.store.read_table,'Склад')
                ready+=1
                if ready==2:gate.set()
                await gate.wait()
                return await self.worker.run(self.store.append_workflow,'Склад',{
                    'Тип операції':'Видача','Найменування':'болти М8',
                    'Кількість':'70','Од. вим.':'шт'},rid,'Test')
        results=await asyncio.gather(issue('OUT-1'),issue('OUT-2'),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results),1)
        self.assertEqual(sum(r==(True,False) for r in results),1)
        self.assertEqual(len(self.store.client.data['Склад']),3)

    async def test_other_request_revocation_invalidates_cached_permissions(self):
        ready=asyncio.Event();revoked=asyncio.Event()
        async def employee():
            with request_reads():
                before=await self.worker.run(self.store.permissions,456,{123})
                self.assertIn('Склад',before);ready.set();await revoked.wait()
                after=await self.worker.run(self.store.permissions,456,{123})
                self.assertNotIn('Склад',after)
        async def owner():
            await ready.wait()
            with request_reads():
                await self.worker.run(self.store.grant,456,'Склад','none',123)
            revoked.set()
        await asyncio.gather(employee(),owner())

    async def test_competing_owner_decisions_cannot_reject_an_approved_project(self):
        with request_reads():
            await self.worker.run(self.store.read_table,'Запити на проєкти')
            approve=await self.worker.run(decide_project,self.store,'PR1','Owner A',True,'now')
            with self.assertRaises(ValueError):
                await self.worker.run(decide_project,self.store,'PR1','Owner B',False,'later')
        self.assertTrue(approve[0])
        headers,_,row=self.store._lookup('Запити на проєкти','PR1')
        self.assertEqual(dict(zip(headers,row))['Статус'],'Погоджено')

    async def test_simultaneous_registration_creates_one_record_per_user(self):
        async def register(uid):
            with request_reads():
                return await self.worker.run(self.store.register,{'id':uid,'first_name':'Test'})
        results=await asyncio.gather(register(789),register(789),register(790))
        self.assertEqual(results,[True,False,True])

    async def test_independent_workbooks_do_not_invalidate_each_other(self):
        other=make_store();other.id='history'
        with request_reads():
            before=await self.worker.run(self.store.data,GRANTS)
            await self.worker.run(other.update,GRANTS,2,['456','Склад','none','123'])
            after=await self.worker.run(self.store.data,GRANTS)
        self.assertEqual(before,after)
