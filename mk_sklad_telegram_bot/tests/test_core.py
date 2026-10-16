"""Offline regression checks; no real Telegram, Google or Postgres secrets."""
import hashlib
import hmac
import json
import os
import time
import unittest
from urllib.parse import urlencode

os.environ.update({
    "TELEGRAM_BOT_TOKEN":"12345:TEST_TOKEN_FOR_OFFLINE_TESTS",
    "TELEGRAM_WEBHOOK_SECRET":"testwebhooksecret",
    "PUBLIC_BASE_URL":"https://example.org",
    "OPENAI_API_KEY":"not-a-key",
    "DATABASE_URL":"postgresql://ignored",
    "GOOGLE_SERVICE_ACCOUNT_JSON_B64":"e30=",
    "OWNER_IDS":"123", "BUYER_IDS":"456", "STOREKEEPER_IDS":"789",
})

from fastapi import HTTPException
from app.pgstore import record_key,facets
from app.webapp import _authorized,accessible,task_visible
from app.config import Settings

class SecurityTests(unittest.TestCase):
    def sign(self,user_id,age=0,token=None):
        values={"auth_date":str(int(time.time())-age),
                "user":json.dumps({"id":user_id,"first_name":"Тест"},ensure_ascii=False)}
        secret=hmac.new(b"WebAppData",(token or os.environ["TELEGRAM_BOT_TOKEN"]).encode(),
                        hashlib.sha256).digest()
        values["hash"]=hmac.new(secret,"\n".join(f"{k}={v}" for k,v in sorted(values.items())).encode(),
                                hashlib.sha256).hexdigest()
        return urlencode(values)

    def test_roles(self):
        self.assertEqual(_authorized(self.sign(123))["role"],"owner")
        self.assertEqual(_authorized(self.sign(456))["role"],"buyer")
        self.assertEqual(_authorized(self.sign(789))["role"],"storekeeper")

    def test_reject_tampered(self):
        with self.assertRaises(HTTPException):
            _authorized(self.sign(123).replace("123","456"))

    def test_reject_expired(self):
        with self.assertRaises(HTTPException):
            _authorized(self.sign(123,age=90000))

    def test_reject_unknown_user(self):
        with self.assertRaises(HTTPException):
            _authorized(self.sign(999))

    def test_role_sheet_access(self):
        self.assertIsNone(accessible("owner","Закупка"))
        with self.assertRaises(HTTPException):
            accessible("storekeeper","Закупка")
        with self.assertRaises(HTTPException):
            accessible("buyer","Склад")

    def test_task_scoping(self):
        buyer=[""]*20
        buyer[4]="Юра"
        keeper=[""]*20
        keeper[4]="Комірник"
        self.assertTrue(task_visible("buyer",buyer))
        self.assertFalse(task_visible("buyer",keeper))
        self.assertTrue(task_visible("storekeeper",keeper))

class DataTests(unittest.TestCase):
    def test_preserve_sheet_ids(self):
        self.assertEqual(record_key("Закупка",["METK-1206-01"],7),"METK-1206-01")
        self.assertEqual(record_key("Інвентаризація",["ПА-31"]+[""]*10+["INV-PA31-012"],8),"INV-PA31-012")
        self.assertEqual(record_key("Уточнення відомостей",["ПА-31"],8),"ROW-8")

    def test_facets(self):
        p=["A","R","АР-26","Труба",3,"м"]
        self.assertEqual(facets("Закупка",p)[:2],("АР-26","Труба"))

if __name__=="__main__":
    unittest.main()
