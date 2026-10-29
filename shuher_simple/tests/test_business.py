"""Offline tests for purchasing linkage, stock arithmetic and status safety."""
import unittest
from decimal import Decimal
from unittest.mock import Mock

from business import (allowed_status, positive_quantity, stock_balances,
                      procurement_from_request, today_procurement)
from store import SheetsStore


class PureRules(unittest.TestCase):
    def test_positive_quantity(self):
        self.assertEqual(positive_quantity("2,5"), Decimal("2.5"))
        for item in ("0", "-2", "", "NaN", "Infinity", True):
            with self.subTest(item=item), self.assertRaises(ValueError):
                positive_quantity(item)

    def test_stock_counts_receipts_less_issues(self):
        headers = ["ID", "Тип операції", "Найменування", "Характеристики", "Кількість", "Од. вим."]
        rows = [
            ["S1", "Надходження", "Гайка", "М12", "10", "шт."],
            ["S2", "Видача", "гайка", "м12", "3,5", "шт."],
            ["S3", "Видача", "гайка", "м12", "bad", "шт."],
        ]
        balances, labels, skipped = stock_balances(headers, rows)
        self.assertEqual(balances[("гайка", "м12", "шт.")], Decimal("6.5"))
        self.assertEqual(len(labels), 1)
        self.assertEqual(skipped, 1)

    def test_fixed_status_vocabulary(self):
        self.assertEqual(allowed_status("Закупка", "статус оплати", "оплачено"),
                         ("Статус оплати", "Оплачено"))
        with self.assertRaises(ValueError):
            allowed_status("Закупка", "Автор", "хтось")
        with self.assertRaises(ValueError):
            allowed_status("Заявки", "Статус", "Видалено")

    def test_purchase_has_same_request_id(self):
        payload = procurement_from_request(
            {"Найменування": "Гайка", "Кількість": "4", "Од. вим.": "шт."}, "REQ-1")
        self.assertEqual(payload["ID заявки"], "REQ-1")
        self.assertEqual(payload["Статус замовлення"], "До замовлення")

    def test_owner_summary_counts_only_today(self):
        headers = ["Дата", "Найменування", "Кількість", "Од. вим.", "ID заявки", "Статус замовлення"]
        rows = [["28.10.2026 15:00", "гайки", 7, "шт.", "REQ-1", "До замовлення"],
                ["27.10.2026 19:00", "шайби", 2, "шт.", "REQ-2", "Отримано"]]
        report = today_procurement(headers, rows, "28.10.2026")
        self.assertIn("1 позицій", report)
        self.assertIn("гайки", report)
        self.assertNotIn("шайби", report)


class IntegrationRules(unittest.TestCase):
    def new_store(self):
        obj = SheetsStore.__new__(SheetsStore)
        obj.id = "TEST_SPREADSHEET_ID"
        obj.resolve = lambda title: title
        original_read = getattr(obj,"read_table",None)
        obj.read_table = lambda tab: (
            (["ID","Назва проєкту"],[["PR-002","STF"]])
            if tab=="Проєкти" else ([],[])
        )
        return obj

    def test_request_creates_linked_procurement_once(self):
        obj = self.new_store()
        saved = {}
        def fake_append(table, values, rid, author):
            key = (table, rid)
            if key in saved:
                return False
            saved[key] = dict(values)
            return True
        obj.append_record = fake_append
        values = {"Проєкт": "STF", "Найменування": "Гайки М12", "Кількість": "7", "Од. вим.": "шт."}
        self.assertEqual(obj.append_workflow("Заявки", values, "REQ-1", "Петро"), (True, True))
        self.assertEqual(obj.append_workflow("Заявки", values, "REQ-1", "Петро"), (False, False))
        self.assertEqual(saved[("Закупка", "ZK-REQ-1")]["ID заявки"], "REQ-1")
        self.assertEqual(saved[("Заявки", "REQ-1")]["Статус"], "У закупці")

    def test_request_requires_project(self):
        obj=self.new_store()
        obj.append_record=Mock()
        with self.assertRaisesRegex(ValueError, "проєкт"):
            obj.append_workflow("Заявки", {
                "Найменування":"Гайка","Кількість":"10","Од. вим.":"шт."
            },"R-1","Комірник")
        obj.append_record.assert_not_called()

    def test_issue_blocked_if_stock_is_insufficient(self):
        obj = self.new_store()
        headers = ["ID", "Тип операції", "Найменування", "Характеристики", "Кількість", "Од. вим."]
        obj.read_table = lambda tab: (headers, [["S1", "Надходження", "Гайка", "М12", "3", "шт."]])
        obj.append_record = Mock()
        with self.assertRaisesRegex(ValueError, "Недостатньо"):
            obj.append_workflow("Склад", {
                "Тип операції": "Видача", "Найменування": "Гайка",
                "Характеристики": "М12", "Кількість": "4", "Од. вим.": "шт.",
            }, "S2", "Комірник")
        obj.append_record.assert_not_called()

    def test_status_updates_one_cell_after_confirm(self):
        obj = self.new_store()
        headers = ["ID", "Дата", "Статус"]
        obj.read_table = lambda tab: (headers, [["REQ-1", "28.10.2026", "Нова"]])
        fake_execute = Mock()
        fake_update = Mock(return_value=Mock(execute=fake_execute))
        obj.client = Mock()
        obj.client.values.return_value.update = fake_update
        self.assertEqual(obj.prepare_status("Заявки", "REQ-1", "Статус", "у закупці"),
                         ("Статус", "У закупці", "Нова"))
        with self.assertRaisesRegex(ValueError, "уже змінився"):
            obj.update_status("Заявки", "REQ-1", "Статус", "У закупці", "Погоджена")
        self.assertTrue(obj.update_status("Заявки", "REQ-1", "Статус", "У закупці", "Нова"))
        self.assertEqual(fake_update.call_args.kwargs["range"], "'Заявки'!C2")
        self.assertEqual(fake_update.call_args.kwargs["body"], {"values": [["У закупці"]]})


if __name__ == "__main__":
    unittest.main()
