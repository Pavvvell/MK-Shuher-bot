import unittest

from task_notifications import (plain_telegram_text, authorized_recipient,
                                task_marker, task_notice)

USERS = [["1769804789", "Octavia_Yur", "Yuriy", "", ""],
         ["1983334624", "", "Sergey", "", ""]]
GRANTS = [["Telegram ID", "Таблиця", "Доступ", "Змінив"],
          ["1769804789", "Доручення", "read", "431633504"],
          ["1983334624", "Доручення", "read", "431633504"]]


class TaskNoticeTests(unittest.TestCase):
    def test_literal_markdown_removed(self):
        self.assertEqual(plain_telegram_text("**Текст** та **далі**"),
                         "Текст та далі")

    def test_registered_and_read_authorized(self):
        target = authorized_recipient("Юрій", USERS, GRANTS)
        self.assertEqual(target[0], 1769804789)
        self.assertIsNone(authorized_recipient("невідомий", USERS, GRANTS))
        revoked = GRANTS + [["1769804789", "Доручення", "none", "431633504"]]
        self.assertIsNone(authorized_recipient("Юрій", USERS, revoked))

    def test_task_change_generates_new_marker(self):
        data = {"Завдання": "Перевірити прибуття", "Виконавець": "Юрій",
                "Статус": "Нове"}
        original = task_marker("SH-1", data, 1769804789)
        data["Статус"] = "У роботі"
        self.assertNotEqual(original, task_marker("SH-1", data, 1769804789))

    def test_message_contains_task_not_markdown(self):
        body = task_notice({"ID": "SH-1", "Завдання": "**Зателефонувати**",
                            "Проєкт": "Купянськ/Бахмут"})
        self.assertIn("SH-1", body)
        self.assertNotIn("**", body)


if __name__ == "__main__":
    unittest.main()
