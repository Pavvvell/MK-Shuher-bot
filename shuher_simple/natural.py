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
