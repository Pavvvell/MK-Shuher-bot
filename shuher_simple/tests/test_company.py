"""Offline checks for permanent MK Sklad context and role privacy."""
import unittest

from company import build_company_context


class CompanyContextTests(unittest.TestCase):
    def test_owner_gets_real_admin_instructions(self):
        prompt=build_company_context(True,{
            "Заявки":"write","Закупка":"write",
            "Склад":"write","Доручення":"write",
        })
        self.assertIn("МК Склад",prompt)
        self.assertIn("/grant",prompt)
        self.assertIn("ВЛАСНИК",prompt)
        self.assertIn("Заявки",prompt)
        self.assertIn("Закупка",prompt)
        self.assertIn("Склад",prompt)
        self.assertIn("Доручення",prompt)
        self.assertIn("без погодження",prompt)
        self.assertIn("Telegram ID",prompt)
        self.assertIn("«так»",prompt)
        self.assertIn("PDF",prompt)

    def test_worker_cannot_see_ungranted_business_tabs(self):
        prompt=build_company_context(False,{"Склад":"read"})
        self.assertIn("Склад (лише читання)",prompt)
        self.assertIn("УПОВНОВАЖЕНИЙ ПРАЦІВНИК",prompt)
        self.assertNotIn("Закупка (читання й запис)",prompt)
        self.assertNotIn("може бачити всі вкладки",prompt)
        self.assertIn("Права видає власник",prompt)

    def test_no_telegram_ids_or_secret_keys(self):
        prompt=build_company_context(False,{"Заявки":"write"})
        self.assertNotIn("metal.unit.llc@gmail.com",prompt)
        self.assertNotIn("shuher-sheets@",prompt)
        self.assertNotIn("OPENAI_API_KEY",prompt)

if __name__=="__main__":
    unittest.main()
