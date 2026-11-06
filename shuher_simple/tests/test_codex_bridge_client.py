import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('bridge_client',Path(__file__).resolve().parents[2]/'tools/codex_bridge_client.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class ClientTests(unittest.TestCase):
    def test_mail_batch_cannot_create_overlapping_digest_keys(self):
        result={'job':'mail','status':'ok','notify':True,'title':'Mail','summary':'Action','processed_message_ids':['a','b']}
        with self.assertRaises(ValueError):c.notice_from_result(result,'a','shadow','expiry')
        result['processed_message_ids']=['a']
        self.assertEqual(c.notice_from_result(result,'a','shadow','expiry')['source_id'],'a')
        result['notify']=False;self.assertIsNone(c.notice_from_result(result,'a','shadow','expiry'))
    def test_pull_crash_before_cursor_write_replays_without_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/'inbox.jsonl';cursor=Path(d)/'cursor';original=c.atomic_write
            def fail_cursor(path,text):
                if path==cursor:raise OSError('crash')
                original(path,text)
            response={'events':[{'key':'k','text':'hello'}],'next_cursor':1}
            with patch.object(c,'request',return_value=response):
                with patch.object(c,'atomic_write',fail_cursor):
                    with self.assertRaises(OSError):c.pull(output,cursor)
                self.assertFalse(cursor.exists());c.pull(output,cursor)
            self.assertEqual(len(output.read_text().splitlines()),1);self.assertEqual(cursor.read_text(),'1')
    def test_redirect_refused(self):
        with self.assertRaises(ValueError):c.NoRedirect().redirect_request(None,None,None,None,None,None)
