"""Projects and invoice safety: owner-only additions and project-required bills."""
import unittest
from unittest.mock import Mock

from projects import (approved_projects, canonical_project, select_project_answer,
                      requested_new_project, explicit_project_in_message,
                      approve_new_project)
from store import SheetsStore


class Fixture:
    def __init__(self):
        self.projects=[["PR-001","STF"],["PR-002","ДП"],["PR-003","Мет обробка"]]
        self.request=["NPR-123","29.10.2026","Новий завод","Юрій","Потрібно","Очікує погодження"]
        self.changed=[]
        self.added=[]
    def read_table(self,table):
        if table=="Проєкти":
            return ["ID","Назва проєкту"],self.projects
        raise AssertionError("Forbidden data read")
    def _lookup(self,table,rid):
        assert table=="Запити на проєкти" and rid=="NPR-123"
        return ["ID","Дата","Назва проєкту","Ініціатор","Обґрунтування","Статус"],2,self.request
    def append_record(self,table,values,rid,author):
        self.added.append((table,values,rid,author))
        if table=="Проєкти":
            self.projects.append([rid,values["Назва проєкту"]])
        return True
    def update_field(self,table,rid,field,value):
        self.changed.append((field,value))
        if field=="Статус":
            self.request[5]=value


class ProjectTests(unittest.TestCase):
    def test_project_catalogue_and_selection(self):
        db=Fixture()
        self.assertEqual(approved_projects(db),["STF","ДП","Мет обробка"])
        self.assertEqual(canonical_project(db,"мЕт ОБРОБКА"),"Мет обробка")
        self.assertEqual(select_project_answer(db,"2"),"ДП")
        self.assertIsNone(select_project_answer(db,"4"))
        self.assertIsNone(canonical_project(db,"Новий завод"))

    def test_project_must_be_explicit_in_invoice_text(self):
        db=Fixture()
        self.assertEqual(explicit_project_in_message(db,"Рахунок на проєкт ДП"),"ДП")
        self.assertEqual(explicit_project_in_message(db,"проєкт: Мет обробка"),"Мет обробка")
        self.assertIsNone(explicit_project_in_message(db,"Рахунок з підписом"))
        self.assertIsNone(explicit_project_in_message(db,"Може ДП?"))
        self.assertIsNone(explicit_project_in_message(db,"Мет обробка за замовчуванням"))

    def test_new_request_parser(self):
        self.assertEqual(requested_new_project("новий: Другий цех"),"Другий цех")
        self.assertEqual(requested_new_project("новий проєкт: Другий цех"),"Другий цех")
        self.assertIsNone(requested_new_project("STF"))

    def test_owner_approval_creates_catalogue_entry(self):
        db=Fixture()
        created,name=approve_new_project(db,"NPR-123","Власник")
        self.assertTrue(created)
        self.assertEqual(name,"Новий завод")
        self.assertEqual(db.added[0][2],"PR-NPR-123")
        self.assertIn(("Статус","Погоджено"),db.changed)

    def test_worker_cannot_bypass_approval_or_put_bill_without_project(self):
        store=SheetsStore.__new__(SheetsStore)
        store.resolve=lambda title:title
        store.read_table=Fixture().read_table
        store.append_record=Mock(return_value=True)
        with self.assertRaisesRegex(ValueError,"заборонено"):
            store.append_workflow("Проєкти",{"Назва проєкту":"Несанкціонований"},"S1","Юрій")
        with self.assertRaisesRegex(ValueError,"Проєкт"):
            store.append_workflow("Рахунки",{"Постачальник":"Тест"},"S2","Юрій")
        store.append_record.assert_not_called()
        result=store.append_workflow("Рахунки",{"Проєкт":"STF","Постачальник":"Тест"},"S3","Юрій")
        self.assertEqual(result,(True,False))
        self.assertEqual(store.append_record.call_args.args[1]["Проєкт"],"STF")


if __name__=="__main__":
    unittest.main()
