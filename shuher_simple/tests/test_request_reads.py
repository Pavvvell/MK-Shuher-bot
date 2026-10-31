import asyncio
import unittest
from unittest.mock import Mock
from store import SheetsStore, request_reads, GRANTS, META


def store(book='current'):
    result=SheetsStore.__new__(SheetsStore)
    result.id=book
    result.client=Mock()
    result.client.values().get().execute.return_value={'values':[['header'],['old']]}
    result.client.get().execute.return_value={'sheets':[{'properties':{'title':'Заявки'}}]}
    return result


class ReadScopeTests(unittest.TestCase):
    def test_bootstrap_existing_and_missing_tabs(self):
        for complete in (True,False):
            s=store()
            s.client.get().execute.return_value={'sheets':[
                {'properties':{'title':t}} for t in (META if complete else [])]}
            with request_reads():s.bootstrap()
            self.assertEqual(s.client.batchUpdate.call_count,0 if complete else 1)
            self.assertEqual(s.client.values().update.call_count,0 if complete else len(META))

    def test_reuse_and_defensive_copy(self):
        s=store()
        with request_reads():
            s.data('Заявки')[1][0]='mutated'
            self.assertEqual(s.data('Заявки')[1][0],'old')
            s.titles().append('fake')
            self.assertEqual(s.titles(),['Заявки'])
        self.assertEqual(s.client.values().get().execute.call_count,1)
        self.assertEqual(s.client.get().execute.call_count,1)

    def test_next_request_observes_edits_and_revocation(self):
        s=store()
        with request_reads():s.data(GRANTS)
        s.client.values().get().execute.return_value={'values':[['revoked']]}
        with request_reads():self.assertEqual(s.data(GRANTS),[['revoked']])
        self.assertEqual(s.client.values().get().execute.call_count,2)

    def test_no_cache_outside_scope(self):
        s=store();s.data('Заявки');s.data('Заявки')
        self.assertEqual(s.client.values().get().execute.call_count,2)

    def test_workbooks_and_ranges_are_isolated(self):
        a=store();b=store('history')
        b.client.values().get().execute.return_value={'values':[['history']]}
        with request_reads():
            a.data('Заявки');a.data('Заявки',20)
            self.assertEqual(b.data('Заявки'),[['history']])
            self.assertEqual(a.data('Заявки')[1][0],'old')
        self.assertEqual(a.client.values().get().execute.call_count,2)

    def test_writes_refresh_all_limits_but_keep_other_tabs_and_metadata(self):
        for operation in ('append','update'):
            s=store()
            with request_reads():
                s.data('Заявки');s.data('Заявки',20);s.data('Склад');s.titles()
                if operation=='append':s.append('Заявки',['new'])
                else:s.update('Заявки',2,['new'])
                s.client.values().get().execute.return_value={'values':[['new']]}
                self.assertEqual(s.data('Заявки'),[['new']])
                self.assertEqual(s.data('Заявки',20),[['new']])
                self.assertEqual(s.data('Склад')[1][0],'old');s.titles()
            self.assertEqual(s.client.values().get().execute.call_count,5)
            self.assertEqual(s.client.get().execute.call_count,1)

    def test_failed_write_also_invalidates(self):
        s=store()
        with request_reads():
            s.data('Заявки')
            s.client.values().append().execute.side_effect=TimeoutError
            with self.assertRaises(TimeoutError):s.append('Заявки',['maybe committed'])
            s.data('Заявки')
        self.assertEqual(s.client.values().get().execute.call_count,2)

    def test_failed_reads_not_cached_and_scope_resets_on_error(self):
        s=store()
        with self.assertRaises(ValueError):
            with request_reads():
                s.client.values().get().execute.side_effect=[TimeoutError,{'values':[['ok']]}]
                with self.assertRaises(TimeoutError):s.data('Заявки')
                self.assertEqual(s.data('Заявки'),[['ok']])
                raise ValueError
        s.client.values().get().execute.side_effect=None
        s.data('Заявки')
        self.assertEqual(s.client.values().get().execute.call_count,3)

    def test_new_table_refreshes_titles(self):
        s=store()
        with request_reads():
            s.titles();s.create_table('Нова')
            s.client.get().execute.return_value={'sheets':[{'properties':{'title':'Нова'}}]}
            self.assertEqual(s.resolve('Нова'),'Нова')
        self.assertEqual(s.client.get().execute.call_count,2)

    def test_revoke_refreshes_access(self):
        s=store();s.user=Mock(return_value=['456'])
        s.client.values().get().execute.return_value={'values':[['id','table','mode'],['456','Заявки','write']]}
        with request_reads():
            s.data(GRANTS)
            self.assertEqual(s.revoke_all_access(456,123),1)
            s.data(GRANTS)
        self.assertEqual(s.client.values().get().execute.call_count,2)

    def test_targeted_writes_refresh_cached_rows(self):
        from access_ui import token_for
        for kind,tab in [('status','Заявки'),('project','Запити на проєкти'),('access',GRANTS)]:
            s=store()
            s._lookup=Mock(return_value=(['Статус'],2,['Нова']))
            s.access_panel=Mock(return_value={'titles':['Заявки'],
                'access':{'Заявки':'write'},'grants':[['header'],['456','Заявки','write']]})
            with request_reads():
                s.data(tab)
                if kind=='status':s.update_status(tab,'demo','Статус','У закупці','Нова')
                elif kind=='project':s.update_field(tab,'demo','Статус','Погоджено')
                else:s.grant_from_button(456,token_for('Заявки'),'none',123)
                s.data(tab)
            self.assertEqual(s.client.values().get().execute.call_count,2)


class ThreadScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_to_thread_shares_request_cache_but_other_tasks_do_not(self):
        s=store()
        async def request():
            with request_reads():
                await asyncio.to_thread(s.data,'Заявки')
                await asyncio.to_thread(s.data,'Заявки')
        await asyncio.gather(request(),request())
        self.assertEqual(s.client.values().get().execute.call_count,2)
