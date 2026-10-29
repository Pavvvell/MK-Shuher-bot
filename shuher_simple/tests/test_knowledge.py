"""Ensure historical retrieval cannot leak unauthorized tables or alter stock."""
import unittest
from knowledge import search_history, render_owner_rules


class FakeHistory:
    def __init__(self):
        self.reads=[]
    def read_table(self,table):
        self.reads.append(table)
        data={
          "Заявки":(["ID позиції","Проєкт","Найменування"],
                     [["Z-1","STF-02","Труба 54×6"],["Z-2","ПА-31","Гайка М12"]]),
          "Закупка":(["ID закупівлі","Проєкт","Постачальник","Найменування"],
                      [["P-1","STF-02","Постачальник А","Труба 54×6"]]),
          "Склад":(["ID операції","Проєкт","Матеріал"],
                    [["S-1","ВК-1000","Мішок"]]),
          "Доручення":(["ID доручення","Проєкт","Виконавець","Суть доручення"],
                       [["D-1","STF-02","Юра","Замовити трубу"]]),
          "Уточнення відомостей":(["ID","Позиція"],[]),
          "Інвентаризація":(["ID","Матеріал"],[]),
        }
        return data[table]


class HistoryTests(unittest.TestCase):
    def test_worker_reads_only_granted_archive(self):
        source=FakeHistory()
        result=search_history(source,{"Заявки":"read"},False,"STF-02 труба")
        self.assertEqual(source.reads,["Заявки"])
        self.assertEqual([x["tab"] for x in result["results"]],["Заявки"])
        self.assertEqual(result["results"][0]["values"]["Найменування"],"Труба 54×6")

    def test_no_access_no_read(self):
        source=FakeHistory()
        result=search_history(source,{},False,"STF-02")
        self.assertEqual(source.reads,[])
        self.assertFalse(result["results"])

    def test_owner_can_search_across_authorized_archive(self):
        source=FakeHistory()
        result=search_history(source,{},True,"STF-02 труба")
        self.assertTrue(any(x["tab"]=="Закупка" for x in result["results"]))
        self.assertTrue(any(x["tab"]=="Заявки" for x in result["results"]))
        self.assertTrue(result["results"][0]["row"]>=2)

    def test_no_term_means_no_unrestricted_dump(self):
        source=FakeHistory()
        result=search_history(source,{"Закупка":"read"},False,"що було раніше")
        self.assertEqual(source.reads,[])
        self.assertEqual(result["results"],[])

    def test_rules_mark_unimplemented_features(self):
        rules=render_owner_rules([
          ["001","Закупівля","Юрій отримує заявку","Чинне правило"],
          ["002","Рахунки","Автоматично пересилати фото власнику","Потребує маршрутизації"],
        ])
        self.assertIn("НЕ готовими функціями",rules)
        self.assertIn("Автоматично пересилати",rules)


if __name__=="__main__":
    unittest.main()
