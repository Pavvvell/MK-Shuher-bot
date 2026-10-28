"""Offline tests for user-friendly status commands without ID or separator."""
import unittest
from natural import status_candidates, status_selection_text, interpret_natural


class NaturalLookup(unittest.TestCase):
    def setUp(self):
        self.headers=["ID", "Дата", "ID заявки", "Проєкт", "Найменування",
                      "Кількість", "Од. вим.", "Статус замовлення"]
        self.rows=[
            ["ZK-1", "28.10.2026", "REQ-1", "ТЕСТ", "Болти М8×30", "10", "шт.", "До замовлення"],
            ["ZK-2", "28.10.2026", "REQ-2", "СТФ", "Болти М8×40", "20", "шт.", "До замовлення"],
            ["ZK-3", "28.10.2026", "REQ-3", "ТЕСТ", "Гайка М12", "7", "шт.", "Замовлено"],
        ]

    def test_dimension_must_match(self):
        result=status_candidates(self.headers,self.rows,"болти М8х30")
        self.assertEqual([r["id"] for r in result],["ZK-1"])

    def test_vague_name_requires_disambiguation(self):
        result=status_candidates(self.headers,self.rows,"болти")
        self.assertEqual(len(result),2)
        self.assertIn("1.",status_selection_text(result))
        self.assertIn("2.",status_selection_text(result))

    def test_exact_id(self):
        result=status_candidates(self.headers,self.rows,"ZK-3")
        self.assertEqual([r["id"] for r in result],["ZK-3"])

    def test_no_guess(self):
        self.assertEqual(status_candidates(self.headers,self.rows,"труба 54×6"),[])


class NaturalPermissions(unittest.IsolatedAsyncioTestCase):
    async def test_denies_status_without_permission(self):
        async def model(*args,**kwargs):
            return '{"intent":"status","table":"Закупка","field":"Статус замовлення","value":"Замовлено","target":"болти"}'
        got=await interpret_natural("болти замовили",{"Закупка":"read"},False,model)
        self.assertEqual(got["intent"],"other")

    async def test_owner_summary_private(self):
        async def model(*args,**kwargs):
            return '{"intent":"today"}'
        self.assertEqual((await interpret_natural("що за день у закупці",{"Закупка":"read"},False,model))["intent"],"other")
        self.assertEqual((await interpret_natural("що за день у закупці",{"Закупка":"write"},True,model))["intent"],"today")

    async def test_status_update_targets_allowed_field(self):
        async def model(*args,**kwargs):
            return '{"intent":"status","table":"Закупка","field":"Статус оплати","value":"Оплачено","target":"гайка М12"}'
        got=await interpret_natural("гайку М12 оплатили",{"Закупка":"write"},False,model)
        self.assertEqual(got,{"intent":"status","table":"Закупка","field":"Статус оплати","value":"Оплачено","target":"гайка М12"})


if __name__=="__main__":
    unittest.main()
