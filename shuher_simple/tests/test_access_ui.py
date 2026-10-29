"""Offline tests for owner permission menus and Sheets access edits."""
import unittest
from unittest.mock import Mock

from access_ui import (
    PRESETS, MANAGED_TABS, READ_ONLY_TABS, list_view, user_view,
    managed_changes, manual_view, preview_view, modes_view,
)
from store import SheetsStore, GRANTS


class AccessUITests(unittest.TestCase):
    def test_new_user_has_owner_action_and_no_text_commands(self):
        text,markup=user_view(["1769804789","testuser","Юрій","",""],{})
        self.assertIn("@testuser",text)
        data=[b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertIn("su:p:1769804789:buy",data)
        self.assertIn("su:m:1769804789:0",data)
        self.assertIn("su:z:1769804789",data)
        self.assertTrue(all(len(d.encode("utf8"))<=64 for d in data))

    def test_preset_privileges_are_least_privileged(self):
        self.assertEqual(PRESETS["buy"][1]["Рахунки"],"write")
        self.assertEqual(PRESETS["stock"][1]["Накладні"],"write")
        self.assertNotIn("Рахунки",PRESETS["stock"][1])
        self.assertEqual(PRESETS["pm"][1]["Закупка"],"read")
        self.assertTrue(all("Правила Шухера" not in v[1] for v in PRESETS.values()))

    def test_changes_preview_only_managed_modules(self):
        name,changes=managed_changes({"Заявки":"write","Рахунки":"read",
                                      "Кадри":"write"},
                     ["Заявки","Рахунки","Кадри","Склад"],"stock")
        self.assertEqual(name,"Комірник")
        self.assertIn(("Заявки","write","write"),[("Заявки","write","write")])
        self.assertNotIn("Кадри",[c[0] for c in changes])
        self.assertIn(("Рахунки","read","none"),changes)
        text,markup=preview_view(176,"test",changes,"su:c:176:stock")
        self.assertIn("Підтвердити",text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"],
                         "su:c:176:stock")

    def test_manual_read_only_section_no_write_button(self):
        text,markup=modes_view(177,"Проєкти",0,0,"read")
        self.assertNotIn(":write:",str(markup))
        text2,markup2=modes_view(177,"Склад",0,0,"read")
        self.assertIn(":write:",str(markup2))
        self.assertIn("Проєкти",READ_ONLY_TABS)

    def test_pagination(self):
        users=[[str(100000+i),"","Користувач"+str(i),"",""] for i in range(19)]
        text,markup=list_view(users,1)
        self.assertIn("Сторінка 2/3",text)
        self.assertEqual(len(markup["inline_keyboard"]),8)
        self.assertTrue(any(
            b["callback_data"]=="su:l:2" for row in markup["inline_keyboard"]
            for b in row))
        text2,markup2=manual_view(112,["Таблиця"+str(i) for i in range(20)],{},2)
        self.assertIn("3/3",text2)


class StoreAccessTests(unittest.TestCase):
    def fixture(self):
        store=SheetsStore.__new__(SheetsStore)
        store.id="SPREADSHEET"
        store.user=lambda uid:["1769804789","user","Юрій","",""] if uid==1769804789 else None
        store.tables=lambda:["Заявки","Закупка","Склад","Рахунки","Накладні",
                             "Доручення","Проєкти","Кадри"]
        store.resolve=lambda name:name if name in store.tables() else None
        values=Mock()
        values.update.return_value.execute.return_value={}
        store.client=Mock()
        store.client.values.return_value=values
        rows=[
            ["Telegram ID","Таблиця","Доступ","Змінив"],
            ["1769804789","Заявки","write","123"],
            ["1769804789","Кадри","write","123"],
            ["456","Доручення","read","123"],
            ["1769804789","Рахунки","read","123"],
        ]
        store.data=lambda name:rows if name==GRANTS else []
        return store,values

    def test_profile_update_preserves_future_hr_and_other_users(self):
        store,values=self.fixture()
        changed=store.apply_access_profile(1769804789,"buy",42)
        self.assertGreater(changed,0)
        kwargs=values.update.call_args.kwargs
        self.assertEqual(kwargs["range"],"'_Shuher_Access'!A1:D8")
        rows=kwargs["body"]["values"]
        self.assertIn(["1769804789","Кадри","write","123"],rows)
        self.assertIn(["456","Доручення","read","123"],rows)
        self.assertIn(["1769804789","Заявки","read","42"],rows)
        self.assertIn(["1769804789","Рахунки","write","42"],rows)
        self.assertIn(["1769804789","Закупка","write","42"],rows)

    def test_revoke_all_requires_registered_user(self):
        store,values=self.fixture()
        with self.assertRaisesRegex(ValueError,"/start"):
            store.revoke_all_access(999,42)
        values.update.assert_not_called()
        self.assertEqual(store.revoke_all_access(1769804789,42),3)
        rows=values.update.call_args.kwargs["body"]["values"]
        self.assertIn(["1769804789","Кадри","none","42"],rows)
        self.assertIn(["456","Доручення","read","123"],rows)

    def test_protected_tables_cannot_get_write(self):
        store,values=self.fixture()
        store.tables=lambda:["Проєкти","Правила Шухера"]
        with self.assertRaisesRegex(ValueError,"тільки для читання"):
            store.grant(1769804789,"Правила Шухера","write",42)
        values.update.assert_not_called()


if __name__=="__main__":
    unittest.main()
