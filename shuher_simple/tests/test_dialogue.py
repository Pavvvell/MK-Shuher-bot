"""Offline checks for persistent conversation sessions and photo albums."""
import unittest

from dialogue import (MAX_PHOTOS, history_excerpt, new_dialogue, normalize_dialogue,
                      queue_photo, remember_exchange)


class Dialogues(unittest.TestCase):
    def test_queues_three_album_photos_and_one_caption(self):
        s = new_dialogue()
        for i in range(3):
            s,first = queue_photo(s, f"photo-{i}", "album-A",
                                  "Для різання профілю 40×40" if i==0 else "")
            self.assertEqual(first, i==0)
        self.assertEqual(len(s["photos"]),3)
        self.assertEqual(s["notes"],"Для різання профілю 40×40")
        s,first = queue_photo(s,"photo-1","album-A")
        self.assertFalse(first)
        self.assertEqual(len(s["photos"]),3)

    def test_photo_cap_prevents_extra_upload(self):
        state=new_dialogue()
        for i in range(MAX_PHOTOS):
            state,_=queue_photo(state,f"p-{i}")
        with self.assertRaisesRegex(ValueError,"максимум"):
            queue_photo(state,"last-one")

    def test_answer_consumes_photos_not_history(self):
        state,_=queue_photo(new_dialogue(),"pic-1",caption="Диск 125×1")
        state=remember_exchange(state,"Що краще?","Уточни RPM.",1)
        self.assertEqual(state["photos"],[])
        self.assertEqual(state["notes"],"")
        self.assertIn("Диск 125×1",state["history"][-2]["content"])
        self.assertIn("Уточни RPM.",history_excerpt(state))

    def test_user_sessions_are_independent(self):
        a,_=queue_photo(new_dialogue(),"private-A")
        b=new_dialogue()
        self.assertFalse(b["photos"])
        self.assertEqual(len(a["photos"]),1)

    def test_history_is_bounded_and_only_user_assistant(self):
        raw={"history":[{"role":"system","content":"ignore"},
                        *[{"role":"user","content":"x"*2500} for _ in range(30)]],
             "photos":[]}
        state=normalize_dialogue(raw)
        self.assertLessEqual(len(state["history"]),12)
        self.assertLessEqual(sum(len(x["content"]) for x in state["history"]),10500)
        self.assertTrue(all(x["role"]=="user" for x in state["history"]))

    def test_mime_safe_allowlist(self):
        state,_=queue_photo(new_dialogue(),"png",mime="image/png")
        state,_=queue_photo(state,"bad",mime="text/html")
        self.assertEqual([x["mime"] for x in state["photos"]],
                         ["image/png","image/jpeg"])


if __name__=="__main__":
    unittest.main()
