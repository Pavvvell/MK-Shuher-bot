"""Offline safety checks for the owner's single-screen permission editor."""
import unittest
from unittest.mock import Mock

from access_ui import (
    MODE_CODE, MODE_LABEL, READ_ONLY_TABS, can_read, can_write,
    list_view, user_view, title_for, token_for, preview_view,
)
from store import SheetsStore, GRANTS


class AccessUITests(unittest.TestCase):
    def test_single_list_with_three_explicit_permission_buttons(self):
        tabs=["Заявки","Закупка","Склад","Доручення","Рахунки",
              "Накладні","Проєкти","Уточнення відомостей"]
        user=["1769804789","Octavia_Yur","Юрій","",""]
        text,markup=user_view(user,{"Заявки":"read","Закупка":"write",
                                    "Рахунки":"write_only"},tabs)
        self.assertIn("@Octavia_Yur",text)
        self.assertNotIn("🛒 Закупівельник",str(markup))
        rows=markup["inline_keyboard"][:-1]
        self.assertEqual(len(rows),len(tabs))
        self.assertTrue(all(len(row)==4 for row in rows))
        self.assertEqual(rows[0][1]["text"],"✓Читання")
        self.assertEqual(rows[1][3]["text"],"✓Ч+З")
        self.assertEqual(rows[4][2]["text"],"✓Запис")
        self.assertTrue(all(len(b["callback_data"].encode("utf-8"))<=64
                            for row in markup["inline_keyboard"] for b in row))
        self.assertIn("🔒",str(rows[6]))
        self.assertNotIn("Перегляд і запис",str(rows[6]))
        self.assertIn("Уточнення",str(rows[-1][0]))

    def test_row_tokens_are_stable_after_reordering(self):
        titles=["Склад","Заявки","Уточнення відомостей"]
        token=token_for("Уточнення відомостей")
        self.assertEqual(title_for(list(reversed(titles)),token),"Уточнення відомостей")
        with self.assertRaises(ValueError):
            title_for(titles,"this_is_not_a_tab")

    def test_read_write_are_separate(self):
        cases={
          "none":(False,False),
          "read":(True,False),
          "write_only":(False,True),
          "write":(True,True),
        }
        for mode,(reading,writing) in cases.items():
            self.assertEqual(can_read(mode),reading)
            self.assertEqual(can_write(mode),writing)
        self.assertEqual(MODE_LABEL["write_only"],"Запис")

    def test_preview_requires_explicit_confirmation(self):
        text,markup=preview_view(
            "1769804789","Юрій",[("Склад","none","write_only")],
            "su:r:1769804789:abc:w:n",
        )
        self.assertIn("Підтвердити зміну",text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"],
                         "su:r:1769804789:abc:w:n")

    def test_user_pagination(self):
        users=[[str(100000+i),"","Працівник"+str(i),"",""] for i in range(19)]
        text,markup=list_view(users,1)
        self.assertIn("Сторінка 2/3",text)
        self.assertEqual(len(markup["inline_keyboard"]),8)


class StoreAccessTests(unittest.TestCase):
    def fixture(self):
        store=SheetsStore.__new__(SheetsStore)
        store.id="SPREADSHEET"
        store.user=lambda uid:["1769804789","user","Юрій","",""] if uid==1769804789 else None
        store.tables=lambda:["Заявки","Закупка","Склад","Рахунки",
                             "Накладні","Доручення","Проєкти","Кадри"]
        store.resolve=lambda name:name if name in store.tables() else None
        values=Mock()
        values.update.return_value.execute.return_value={}
        store.client=Mock()
        store.client.values.return_value=values
        rows=[
            ["Telegram ID","Таблиця","Доступ","Змінив"],
            ["1769804789","Заявки","write","123"],
            ["1769804789","Кадри","write_only","123"],
            ["456","Доручення","read","123"],
            ["1769804789","Рахунки","read","123"],
        ]
        store.data=lambda name:rows if name==GRANTS else []
        store._upsert=Mock()
        return store,values

    def test_store_accepts_distinct_read_write_combined(self):
        store,values=self.fixture()
        for mode in ("read","write_only","write","none"):
            store.grant(1769804789,"Заявки",mode,42)
        self.assertEqual(store._upsert.call_count,4)
        self.assertEqual(
            [args.args[3][2] for args in store._upsert.call_args_list],
            ["read","write_only","write","none"],
        )

    def test_revoke_all_includes_write_only_and_preserves_others(self):
        store,values=self.fixture()
        with self.assertRaisesRegex(ValueError,"/start"):
            store.revoke_all_access(999,42)
        values.update.assert_not_called()
        self.assertEqual(store.revoke_all_access(1769804789,42),3)
        rows=values.update.call_args.kwargs["body"]["values"]
        self.assertIn(["1769804789","Кадри","none","42"],rows)
        self.assertIn(["456","Доручення","read","123"],rows)

    def test_protected_tables_cannot_get_any_write(self):
        store,values=self.fixture()
        store.tables=lambda:["Проєкти","Правила Шухера"]
        for mode in ("write","write_only"):
            with self.assertRaisesRegex(ValueError,"тільки для читання"):
                store.grant(1769804789,"Правила Шухера",mode,42)
        store._upsert.assert_not_called()


if __name__=="__main__":
    unittest.main()
