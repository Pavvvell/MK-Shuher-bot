import copy
import importlib
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from sheet_fixtures import make_store
from dialogue import new_dialogue, queue_photo, queue_document
from concurrency import SheetsWorker

SCREENSHOT = 'а як у нас взагалі справи? що в блоці що в заявках? стисло і по суті'


class ReportRules(unittest.TestCase):


    def test_explicit_blockers_deadlines_and_unknowns(self):
        from reports import build_report
        s = make_store()
        s.client.data['Доручення'] = [
            ['ID', 'Завдання', 'Проєкт', 'Статус', 'Дедлайн', 'Блокер / потрібне рішення'],
            ['T1', 'Робота 1', 'STF', 'У роботі', '31.10.2026', 'Немає матеріалу'],
            ['T2', 'Робота 2', 'STF', 'Виконано', '30.10.2026', 'Старий блокер'],
            ['T3', 'Робота 3', 'STF', '', 'завтра', ''],
            ['T4', 'Робота 4', 'STF', 'Очікує', '01.11.2026', ''],
        ]
        answer = build_report(s, {'Доручення': 'read'}, SCREENSHOT,
                              datetime(2026, 11, 1, 19, tzinfo=ZoneInfo('Europe/Kyiv')))
        self.assertIn('активні — 2', answer)
        self.assertIn('без статусу — 1', answer)
        self.assertIn('прострочені — 1', answer)
        self.assertIn('Немає матеріалу', answer)
        self.assertNotIn('Старий блокер', answer)
        self.assertIn('Строк відсутній або нерозбірливий: 1', answer)

    def test_project_filter_and_no_history_substitution(self):
        from reports import build_report
        s = make_store()
        answer = build_report(s, {'Заявки': 'read'}, 'Що по заявках проєкту НЕІСНУЮЧИЙ?')
        self.assertIn('не знайдено', answer)
        self.assertNotIn('Заявки: 1', answer)

    def test_reads_beyond_old_4000_row_limit(self):
        from reports import build_report
        s = make_store()
        sample = s.client.data['Заявки'][1]
        s.client.data['Заявки'] += [[f'R{i}'] + sample[1:] for i in range(2, 4102)]
        answer = build_report(s, {'Заявки': 'read'}, SCREENSHOT)
        self.assertIn('Заявки: 4101', answer)
        self.assertIn('Показано', answer)
