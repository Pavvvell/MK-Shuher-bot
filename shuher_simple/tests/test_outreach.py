"""Owner-to-staff Telegram DMs: grounded identities, separate deliveries and no drafts."""
import unittest
from outreach import (
    is_send_request, choose_recipients, delivery_plan,
)

# Synthetic identities only, never real staff Telegram IDs.
USERS=[
    ["100000001","owner_demo","Pavlo","",""],
    ["100000002","buyer_demo","Yuriy","",""],
    ["100000003","warehouse_demo","Sergey","",""],
]
OWNERS={100000001}


class OutreachTests(unittest.TestCase):
    def test_direct_owner_ask_is_a_command_not_a_draft(self):
        command="напиши їм просто що вони можуть починати працювати і надсилати тобі все що треба"
        self.assertTrue(is_send_request(command))
        self.assertTrue(is_send_request("можеш тепер сам написати Юрі та Сергію коротку інструкцію?"))
        for message in ("напиши мені інструкцію", "як написати Юрі?",
                        "не надсилай Сергію", "я написав Сергію вчора",
                        "треба, щоб Сергій написав мені"):
            with self.subTest(message=message):
                self.assertFalse(is_send_request(message))

    def test_two_individual_messages_from_previous_owner_turn(self):
        history=[
            {"role":"user","content":"можеш тепер сам написати Юрі та Сергію коротку інструкцію?"},
            {"role":"assistant","content":"Ось текст; можу надіслати їм у Telegram, чи треба?"},
        ]
        text="напиши їм просто що вони можуть починати працювати і надсилати тобі все що треба"
        plan=delivery_plan(text,history,USERS,OWNERS)
        self.assertEqual([x["id"] for x in plan],[100000002,100000003])
        self.assertEqual(len(plan),2)
        self.assertIn("Привіт, Юрію!",plan[0]["text"])
        self.assertIn("Привіт, Сергію!",plan[1]["text"])
        self.assertNotIn("Юрію та Сергію",plan[0]["text"])
        self.assertNotIn("Юрію та Сергію",plan[1]["text"])
        self.assertIn("Можеш починати працювати",plan[0]["text"])

    def test_direct_short_instructions_are_different_for_roles(self):
        plan=delivery_plan("можеш тепер сам написати Юрі та Сергію коротку інструкцію?",[],USERS,OWNERS)
        self.assertEqual(len(plan),2)
        self.assertIn("рахунки",plan[0]["text"])
        self.assertIn("видаткові",plan[1]["text"])
        self.assertNotEqual(plan[0]["text"],plan[1]["text"])

    def test_explicit_name_only_before_body(self):
        plan=delivery_plan("напиши Юрі, що Сергій почав зварювати",[],USERS,OWNERS)
        self.assertEqual([item["id"] for item in plan],[100000002])
        self.assertIn("Сергій почав зварювати",plan[0]["text"])

    def test_reference_uses_only_owner_messages(self):
        history=[
            {"role":"user","content":"привіт"},
            {"role":"assistant","content":"Я можу написати Юрі та Сергію"},
        ]
        self.assertEqual(choose_recipients("напиши їм",history,USERS),[])
        with self.assertRaisesRegex(ValueError,"Кому саме"):
            delivery_plan("напиши їм",history,USERS,OWNERS)

    def test_missing_body_requests_essential_detail(self):
        with self.assertRaisesRegex(ValueError,"Що саме"):
            delivery_plan("напиши Юрі",[],USERS,OWNERS)

    def test_targets_must_exist_in_users(self):
        with self.assertRaisesRegex(ValueError,"Кому саме"):
            delivery_plan("напиши Петру, що завтра робота",[],USERS,OWNERS)
        with self.assertRaisesRegex(ValueError,"власнику"):
            delivery_plan("напиши Pavlo що завтра робота",[],USERS,OWNERS)

    def test_plain_chat_is_not_forwarded(self):
        self.assertIsNone(delivery_plan(
            "можеш розповісти, як працюють повідомлення?",
            [],USERS,OWNERS))
        self.assertIsNone(delivery_plan(
            "сформулюй два повідомлення, які я сам скопіюю",
            [],USERS,OWNERS))

    def test_previous_explicit_recipients_override_old_reference(self):
        history=[
            {"role":"user","content":"повідом Юрі що вже є рахунок"},
            {"role":"assistant","content":"Добре"},
            {"role":"user","content":"можеш тепер сам написати Юрі та Сергію коротку інструкцію?"},
            {"role":"assistant","content":"Ось текст для Юрія і Сергія"},
        ]
        plan=delivery_plan("напиши їм просто що вони можуть починати працювати",
                           history,USERS,OWNERS)
        self.assertEqual(len(plan),2)


if __name__=="__main__":
    unittest.main()
