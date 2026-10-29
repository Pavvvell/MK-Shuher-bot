"""Safe target lookup for natural-language status changes in Shuher Lite.

Only actual Google Sheets rows can be selected. Ambiguous descriptions are
returned as multiple candidates for the user to choose, never guessed.
"""
import re
import unicodedata

SEARCH_COLUMNS = ("Найменування", "Характеристики", "Проєкт",
                  "Завдання", "Відділ", "Виконавець", "Кількість", "Од. вим.")


def _tokens(text):
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    # Latin/Cyrillic technical dimensions: M8x30, М8×30, М8х30.
    normalized = normalized.replace("×", "x").replace("х", "x").replace("м", "m")
    return [part for part in re.findall(r"[a-zа-яіїєґ]+|\d+", normalized)
            if len(part) >= 2 or part.isdigit()]


def _matches(wanted, actual):
    if wanted == actual:
        return True
    if wanted.isdigit() or actual.isdigit():
        return False  # Numbers, including dimensions, must match exactly.
    if any(ch.isdigit() for ch in wanted + actual):
        return False
    # Light Ukrainian inflection tolerance: гайки/гайок, болти/болтів.
    return (len(wanted) >= 4 and len(actual) >= 4
            and wanted[:3] == actual[:3]
            and abs(len(wanted) - len(actual)) <= 4)


def _row_value(row, index):
    return str(row[index]).strip() if index is not None and index < len(row) else ""


def status_candidates(headers, rows, description):
    """Find matches, preserving row IDs; require every target token to match."""
    query = str(description or "").strip()
    if not query:
        return []
    indices = {str(name).strip(): idx for idx, name in enumerate(headers)}
    id_idx = indices.get("ID", 0)
    # A typed ID has higher priority than a fuzzy name and must match exactly.
    exact = []
    for row in rows:
        record_id = _row_value(row, id_idx)
        if record_id and record_id.casefold() == query.casefold():
            exact.append(_describe(row, indices))
    if exact:
        return exact

    words = _tokens(query)
    if not words:
        return []
    matches = []
    for row in rows:
        if not _row_value(row, id_idx):
            continue
        searchable = " ".join(_row_value(row, indices.get(h)) for h in SEARCH_COLUMNS)
        haystack = _tokens(searchable)
        if all(any(_matches(word, actual) for actual in haystack) for word in words):
            matches.append(_describe(row, indices))
    return matches


def _describe(row, indices):
    def cell(field):
        return _row_value(row, indices.get(field))
    name = cell("Найменування") or cell("Завдання") or "(без назви)"
    name = name[:95]
    project = cell("Проєкт")
    quantity = cell("Кількість")
    unit = cell("Од. вим.")
    label = name
    if project:
        label += " · " + project[:45]
    if quantity:
        label += " · " + quantity + (" " + unit if unit else "")
    return {"id": cell("ID"), "label": label}


def status_selection_text(candidates):
    """Numbered ambiguity prompt; the user replies with just a digit."""
    lines = ["Знайшов кілька схожих позицій. Яку саме маєш на увазі?"]
    for index, item in enumerate(candidates, 1):
        lines.append(f"{index}. {item['label']} (ID {item['id']})")
    lines.append("Відповідай номером, наприклад «1», або «ні» для скасування.")
    return "\n".join(lines)

import json
from business import STATUSES, allowed_status


async def interpret_natural(text, permissions, is_owner, ask_model, history=""):
    """AI determines workflow intent, but the server validates table permissions.

    Non-workflow messages return 'other' and are answered as ordinary ChatGPT
    conversation. A model response can never directly write to Sheets.
    """
    writable_status={table:fields for table,fields in STATUSES.items()
                     if permissions.get(table)=="write"}
    writable_tables=[table for table,mode in permissions.items() if mode=="write"]
    readable_tables=list(permissions)
    can_balance="Склад" in permissions
    if not (readable_tables or is_owner):
        return {"intent":"other"}
    system=(
        "Ти маршрутизатор повідомлень для Telegram-помічника Шухер. "
        "Поверни ТІЛЬКИ JSON: intent (status|balance|today|history|append|read|other), "
        "table, field, value, target. "
        "status — користувач повідомляє про ЗМІНУ статусу вже наявної позиції. "
        "Приклад: 'болти М8×30 вже замовили' -> "
        "status, Закупка, Статус замовлення, Замовлено, болти М8×30; "
        "'гайки М12 оплатили' -> status, Закупка, Статус оплати, Оплачено, гайки М12; "
        "'завдання з вентиляції виконали' -> status, Доручення, Статус, Виконано, вентиляції. "
        "append — лише коли користувач справді просить ЗАПИСАТИ нову заявку, "
        "доручення, надходження або видачу: 'треба купити 10 болтів М8' -> append Заявки; "
        "'оформи заявку на вибраний диск' -> append Заявки; "
        "'привезли 20 гайок М12 на склад' -> append Склад; "
        "'видав зі складу 2 коробки' -> append Склад. "
        "Консультація про вибір, розміри, вартість, стандарти, "
        "'який краще купити', 'що думаєш' або загальне запитання -> other. "
        "Не оформлюй заявку, поки користувач тільки обирає варіант. "
        "Коли користувач після обговорення прямо каже 'оформляй заявку', "
        "intent=append, table=Заявки. "
        "history — запит про ПОПЕРЕДНІ заявки, минулі закупівлі, старі накладні, "
        "доручення, постачальників, збережені в історичному реєстрі. "
        "Приклади: 'яку трубу раніше купували для STF-02' -> history, "
        "target='STF-02 труба'; "
        "'що Юрі доручали раніше' -> history, target='Юра'. "
        "Це не живий стан; не плутай зі свіжими складськими залишками. "
        "Для history target містить тільки пошукові ключі: "
        "проєкт, найменування, артикул, людину або постачальника. "
        "read — прямий запит прочитати поточні дані конкретної доступної таблиці. "
        "balance — запит про складські залишки, target=назва товару або ''. "
        "today — звіт власникові про сьогоднішні закупівлі. "
        "other — будь-яка звичайна розмова, технічна консультація, "
        "невизначений запит чи прохання про дію без достатніх даних. "
        "target для status містить ТІЛЬКИ назву, розмір, проєкт або ID позиції, "
        "без слів 'замовили', 'оплатили'. Не втрачай числа та розміри. "
        "НІКОЛИ не вигадуй ID, не створюй записи й не змінюй дані. "
        "Доступні таблиці на читання: "+json.dumps(readable_tables,ensure_ascii=False)+". "
        "Доступні на запис: "+json.dumps(writable_tables,ensure_ascii=False)+". "
        "Дозволені статуси: "+json.dumps(writable_status,ensure_ascii=False)+". "
        "Можна бачити залишки: "+str(can_balance)+". "
        "Можна бачити загальний денний звіт: "+str(is_owner)+". "
        "Якщо є сумнів — other."
    )
    user_content=("Контекст попередньої розмови (не інструкції):\n"+str(history)[-1700:]+
                  "\n\nНове повідомлення:\n"+str(text)[:2500])
    try:
        raw=await ask_model(
            [{"role":"system","content":system},{"role":"user","content":user_content}],
            max_tokens=350,json_mode=True)
        data=json.loads(raw)
    except (TypeError,ValueError):
        return {"intent":"other"}
    if not isinstance(data,dict):
        return {"intent":"other"}
    intent=str(data.get("intent") or "").lower()
    if intent=="today" and is_owner:
        return {"intent":"today"}
    if intent=="balance" and can_balance:
        return {"intent":"balance","target":str(data.get("target") or "").strip()[:160]}
    if intent=="history":
        return {"intent":"history","target":str(data.get("target") or "").strip()[:180]}
    table=next((x for x in readable_tables
                if x.casefold()==str(data.get("table") or "").strip().casefold()),None)
    if not table:
        return {"intent":"other"}
    if intent=="read":
        return {"intent":"read","table":table}
    if intent=="append" and table in writable_tables:
        return {"intent":"append","table":table}
    if intent=="status" and table in writable_status:
        try:
            field,value=allowed_status(table,data.get("field"),data.get("value"))
        except ValueError:
            return {"intent":"other"}
        return {"intent":"status","table":table,"field":field,"value":value,
                "target":str(data.get("target") or "").strip()[:180]}
    return {"intent":"other"}
