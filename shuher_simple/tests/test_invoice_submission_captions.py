"""Initial invoice forwarding must match the owner's payable-file captions."""
import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from concurrency import SheetsWorker
from sheet_fixtures import make_store
from purchase_bundle import validate_package


class InvoiceSubmissionCaptions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env=dict(TELEGRAM_BOT_TOKEN='test',TELEGRAM_WEBHOOK_SECRET='test',
                 OPENAI_API_KEY='test',SPREADSHEET_ID='test',OWNER_IDS='123')
        with patch.dict(os.environ,env),patch('store.SheetsStore'),patch('openai.AsyncOpenAI'):
            self.app=importlib.import_module('app')
        self.store=make_store()
        self.worker=SheetsWorker()
        self.bot=AsyncMock(return_value={'ok':True})
        self.patches=[patch.object(self.app,'store',self.store),
                      patch.object(self.app,'sheet_worker',self.worker),
                      patch.object(self.app,'owners',{123}),
                      patch.object(self.app,'bot_call',self.bot)]
        for p in self.patches:p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()
        self.worker.close()

    async def test_initial_photo_and_pdf_have_the_same_three_line_caption(self):
        await self.app.notify_owner_invoice(456,'TEST-INVOICE',
            {'Проєкт':'Тестовий офіс','Постачальник':'Тестовий постачальник','Автор':'@worker'},
            {'photos':[{'file_id':'photo-one'}],'documents':[{'file_id':'pdf-one'}]})
        media=[c.args for c in self.bot.await_args_list if c.args[0] in ('sendPhoto','sendDocument')]
        self.assertEqual(len(media),2)
        self.assertEqual([m[0] for m in media],['sendPhoto','sendDocument'])
        for method,payload in media:
            self.assertEqual(payload['caption'],'Катана\nТестовий офіс\nШакін')
            self.assertEqual(payload['chat_id'],123)
        self.assertEqual(media[0][1]['photo'],'photo-one')
        self.assertEqual(media[1][1]['document'],'pdf-one')

    async def test_confirmed_bundle_keeps_each_invoice_project_and_photo_association(self):
        for i,name in enumerate(('Проєкт А','Проєкт Б')):
            self.store.append_record('Проєкти',{'Назва проєкту':name,'Статус':'Активний'},f'CAP-P{i}','test')
        package=validate_package({'images':[{'index':i,'type':'invoice'} for i in range(1,4)],'groups':[
            {'type':'invoice','images':[1,2],'supplier':'Постачальник А',
             'project':'Проєкт А','amount':'10','currency':'UAH','invoice_no':'A-1'},
            {'type':'invoice','images':[3],'supplier':'Постачальник Б',
             'project':'Проєкт Б','amount':'20','currency':'UAH','invoice_no':'B-1'},
        ]},3)
        # These projects have been explicitly selected by the submitting worker.
        for group,name in zip(package['groups'],('Проєкт А','Проєкт Б')):
            group['project']=name
        payload={'bundle_id':'TEST-BUNDLE','package':package,
                 'photos':[{'file_id':f'photo-{i}'} for i in range(1,4)]}
        self.store.save_pending(456,'Рахунки',{'kind':'purchase_bundle',**payload})
        await self.app.handle_message({'message':{'from':{'id':456,'first_name':'Yuriy','username':'worker'},
                                      'chat':{'id':456,'type':'private'},'text':'так'}})
        photos=[c.args[1] for c in self.bot.await_args_list if c.args[0]=='sendPhoto']
        self.assertEqual([(p['photo'],p['caption']) for p in photos],[
            ('photo-1','Катана\nПроєкт А\nШакін'),
            ('photo-2','Катана\nПроєкт А\nШакін'),
            ('photo-3','Катана\nПроєкт Б\nШакін')])
        self.assertTrue(all(p['chat_id']==123 for p in photos))
        self.assertIsNone(self.store.get_pending(456))
        headers,rows=self.store.read_table('Рахунки')
        added=[dict(zip(headers,r)) for r in rows if r[0].startswith('TEST-BUNDLE')]
        self.assertEqual(len(added),2)
        self.assertEqual([r['Telegram файли'] for r in added],['photo-1, photo-2','photo-3'])
        self.assertEqual([r['Постачальник'] for r in added],['Постачальник А','Постачальник Б'])
        self.assertTrue(all(r['Автор']=='@worker' for r in added))
