"""Offline tests: generic Shuher identity and permission-scoped context."""
import unittest

from company import build_company_context


class CompanyContextTests(unittest.TestCase):
    def test_owner_receives_admin_context(self):
        prompt=build_company_context(True,{
            "Заявки":"write","Закупка":"write",
            "Склад":"write","Доручення":"write",
        })
        for word in ("ШУХЕР","/grant","ВЛАСНИК","Заявки","Закупка",
                     "Склад","Доручення","«так»","PDF"):
            self.assertIn(word,prompt)
        self.assertNotIn("МК Склад",prompt)

    def test_worker_has_only_actual_grants(self):
        prompt=build_company_context(False,{"Склад":"read"})
        self.assertIn("Склад (лише читання)",prompt)
        self.assertIn("ПРАЦІВНИК",prompt)
        self.assertNotIn("Закупка (читання й запис)",prompt)
        self.assertNotIn("Власник може додавати користувачів",prompt)
        self.assertIn("Доступ до розділів видає тільки власник",prompt)

    def test_no_secrets_or_private_ids(self):
        prompt=build_company_context(False,{"Заявки":"write"})
        for secret in ("metal.unit.llc@gmail.com","shuher-sheets@","OPENAI_API_KEY"):
            self.assertNotIn(secret,prompt)


if __name__=="__main__":
    unittest.main()
