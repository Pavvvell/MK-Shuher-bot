"""Read-only retrieval from Shuher's historical register and owner-authored rules.

Historical rows are never inserted into current stock or purchasing. All
access control is performed before searching the archive. The old workbook is
accessed with SheetsStore credentials scoped to the archive spreadsheet ID.
"""
import re
import unicodedata


HISTORY_TABS = (
    "Заявки", "Закупка", "Склад", "Доручення",
    "Уточнення відомостей", "Інвентаризація",
)

STOP = {
    "який","яка","яке","які","яку","чим","чому","було","була","були","був",
    "мені","треба","потрібно","раніше","минулого","минулі","попередні",
    "знайди","покажи","перевір","можна","робили","купували","купили",
    "замовляли","замовили","скільки","звідки","куди","що","де","коли",
    "чи","у","в","і","на","для","від","по","про","з","із","та","ще",
    "the","was","past","old","about","where","how",
}


def _terms(text):
    normalized=unicodedata.normalize("NFKC",str(text or "")).casefold()
    tokens=re.findall(r"[^\W_]+", normalized, re.UNICODE)
    return [t for t in tokens if t not in STOP and len(t)>=2][:14]


def _term_match(term, content):
    if term in content:
        return True
    # Light tolerance for Ukrainian inflections such as болти/болтів,
    # plus project codes separated by hyphens/spaces.
    if len(term)>=5:
        stem=term[:4]
        return any(word.startswith(stem) for word in re.findall(r"[^\W_]+",content))
    return False


def search_history(source, access, is_owner, query, max_hits=26):
    """Return only source rows in tabs the caller is permitted to read."""
    authorized=[name for name in HISTORY_TABS if is_owner or access.get(name) in ("read","write")]
    if not authorized:
        return {"query":str(query)[:180],"results":[],"error":"Немає доступу до історичних розділів."}
    terms=_terms(query)
    if not terms:
        return {"query":str(query)[:180],"results":[],"error":"Вкажи назву, артикул, постачальника, проєкт або працівника."}
    rows=[]
    for tab in authorized:
        headers,records=source.read_table(tab)
        if not headers:
            continue
        for spreadsheet_row,record in enumerate(records,2):
            if not record or not any(str(v).strip() for v in record):
                continue
            full=" ".join(str(v) for v in record).casefold()
            score=sum(1 for term in terms if _term_match(term,full))
            if not score:
                continue
            values={str(h):str(record[i])[:320] for i,h in enumerate(headers)
                    if i<len(record) and str(record[i]).strip() not in ("","null","None")}
            rows.append({"score":score,"tab":tab,"row":spreadsheet_row,
                         "values":values})
    if not rows:
        return {"query":str(query)[:180],"results":[],
                "error":"У дозволених історичних вкладках збігів не знайдено."}
    # Prefer all-term matches. If none, return partial matches explicitly.
    max_score=max(item["score"] for item in rows)
    exact=max_score==len(terms)
    finalists=[item for item in rows if item["score"]==max_score]
    finalists.sort(key=lambda r:(HISTORY_TABS.index(r["tab"]),r["row"]))
    return {"query":str(query)[:180],"results":finalists[:max_hits],
            "partial":not exact,"total_matched":len(finalists),
            "searched_tabs":authorized}


def render_owner_rules(rows, max_chars=8500):
    """Render live rules, explicitly separating active policies and plans."""
    parts=[]
    size=0
    for row in rows:
        if len(row)<4:
            continue
        category,rule,status=map(lambda x:str(x).strip(),row[1:4])
        if not category or not rule:
            continue
        line=f"[{status}] {category}: {rule}"
        if size+len(line)>max_chars:
            break
        parts.append(line)
        size+=len(line)
    if not parts:
        return ""
    return (
        "ПРАВИЛА З РОБОЧОЇ GOOGLE-ТАБЛИЦІ «Правила Шухера». "
        "Власник може оновлювати їх прямо в таблиці. "
        "Рядки зі статусами «Потребує…», «План розвитку», «Вимагає…» "
        "є вимогами або задачами, а НЕ готовими функціями. "
        "Не заявляй, що автоматичне пересилання, імпорт PDF/XLSX або "
        "генерація бухгалтерських файлів уже працюють, доки це окремо "
        "не підтверджено технічно. "
        "Історичні відомості не дорівнюють сьогоднішньому стану.\n"
        + "\n".join(parts)
    )
