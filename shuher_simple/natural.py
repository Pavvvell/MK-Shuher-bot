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


async def interpret_natural(text, permissions, is_owner, ask_model):
    """AI interprets intent; caller must verify target against actual rows."""
    writable={table:fields for table,fields in STATUSES.items()
              if permissions.get(table)=="write"}
    can_balance="Склад" in permissions
    if not (writable or can_balance or is_owner):
        return {"intent":"other"}
    system=(
        "Ти класифікуєш природні повідомлення українською у складському боті. "
        "Поверни ЛИШЕ JSON: intent (status|balance|today|other), table, field, value, target. "
        "status — ТІЛЬКИ якщо користувач повідомляє, що статус ВЖЕ ІСНУЮЧОЇ "
        "заявки, закупівлі чи доручення змінився. "
        "Приклади: 'болти М8×30 вже замовили' -> "
        "status, Закупка, Статус замовлення, Замовлено, болти М8×30; "
        "'гайки М12 оплатили' -> status, Закупка, Статус оплати, Оплачено, гайки М12; "
        "'завдання з вентиляції виконали' -> status, Доручення, Статус, Виконано, вентиляції. "
        "target містить тільки назву, розмір, проєкт чи ID позиції БЕЗ дієслів зміни статусу. "
        "Не губи числа або технічні розміри. "
        "Запит про залишки на складі -> balance, target=назва товару або пусто. "
        "'Що сьогодні пішло в закупку' -> today (тільки для власника). "
        "Нові заявки ('треба купити'), додавання складських рухів, питання, "
        "пошук або невизначені формулювання -> other. "
        "Не вигадуй ID, не створюй записів і не змінюй дані. "
        "Дозволені тільки статуси: "+json.dumps(writable,ensure_ascii=False)+". "
        "Можна запитувати складські залишки: "+str(can_balance)+". "
        "Можна бачити денний звіт: "+str(is_owner)+". "
        "Якщо є сумнів — other."
    )
    try:
        raw=await ask_model(
            [{"role":"system","content":system},{"role":"user","content":text}],
            max_tokens=300,json_mode=True)
        data=json.loads(raw)
    except (TypeError,ValueError):
        return {"intent":"other"}
    if not isinstance(data,dict):
        return {"intent":"other"}
    intent=data.get("intent")
    if intent=="today" and is_owner:
        return {"intent":"today"}
    if intent=="balance" and can_balance:
        return {"intent":"balance","target":str(data.get("target") or "").strip()[:160]}
    if intent=="status":
        table=next((x for x in writable
                    if x.casefold()==str(data.get("table") or "").strip().casefold()),None)
        if not table:
            return {"intent":"other"}
        try:
            field,value=allowed_status(table,data.get("field"),data.get("value"))
        except ValueError:
            return {"intent":"other"}
        return {"intent":"status","table":table,"field":field,"value":value,
                "target":str(data.get("target") or "").strip()[:180]}
    return {"intent":"other"}
