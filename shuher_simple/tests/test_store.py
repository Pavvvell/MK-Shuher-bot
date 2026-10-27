"""Offline smoke tests that do not contact Google, OpenAI or Telegram."""
import unittest
from store import quoted, row_values, SheetsStore, DEFAULT

class Helpers(unittest.TestCase):
    def test_quoted_sheet_name(self):
        self.assertEqual(quoted("Юр'їв склад"),"'Юр''їв склад'")
    def test_row_padding(self):
        self.assertEqual(row_values(["a"],3),["a","",""])
    def test_default_columns(self):
        self.assertIn("Найменування",DEFAULT)

class NewSheetRules(unittest.TestCase):
    def test_create_requires_owner_command_style_headers(self):
        obj=SheetsStore.__new__(SheetsStore)
        obj.id="TEST_SPREADSHEET_ID"
        obj.resolve=lambda name:None
        self.sent=[]
        class FakeValues:
            def update(inner,**kwargs):
                self.sent.append(kwargs)
                return type("Result",(),{"execute":lambda _:None})()
        class FakeClient:
            def batchUpdate(inner,**kwargs):
                self.sent.append(kwargs)
                return type("Result",(),{"execute":lambda _:None})()
            def values(inner):return FakeValues()
        obj.client=FakeClient()
        result=obj.create_table("Тест",["Найменування","Кількість"])
        self.assertEqual(result,["ID","Дата","Найменування","Кількість","Автор"])
    def test_forbid_reserved_name(self):
        obj=SheetsStore.__new__(SheetsStore)
        obj.resolve=lambda name:None
        with self.assertRaises(ValueError):obj.create_table("_Shuher_Users")
    def test_forbid_bad_sheet_title(self):
        obj=SheetsStore.__new__(SheetsStore)
        obj.resolve=lambda name:None
        with self.assertRaises(ValueError):obj.create_table("bad/name")

    def test_numeric_quantity_and_legacy_id(self):
        obj=SheetsStore.__new__(SheetsStore)
        obj.read_table=lambda _: (["ID закупівлі","Найменування","Кількість замовлено","Автор"],[])
        captured=[]
        obj.append=lambda name,values:captured.append((name,values))
        result=obj.append_record("Закупка",{"Найменування":"гайки","Кількість замовлено":"7"},
                                 "SH-TEST-01","Контролер")
        self.assertTrue(result)
        self.assertEqual(captured[0][1],["SH-TEST-01","гайки",7,"Контролер"])

if __name__=="__main__":
    unittest.main()
