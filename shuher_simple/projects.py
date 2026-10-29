"""Confirmed project catalogue and owner-only approval workflow.

Projects are approved only when present in the current 'Проєкти' tab.
New requests do not grant project validity until the owner approves them.
"""
import re


def _norm(value):
    return " ".join(str(value or "").strip().casefold().split())


def approved_projects(store):
    headers,rows=store.read_table("Проєкти")
    if "Назва проєкту" not in headers:
        raise ValueError("У довіднику «Проєкти» немає колонки «Назва проєкту».")
    col=headers.index("Назва проєкту")
    entries=[]
    seen=set()
    for row in rows:
        name=str(row[col]).strip() if col<len(row) else ""
        if name and _norm(name) not in seen:
            entries.append(name)
            seen.add(_norm(name))
    return entries


def canonical_project(store,requested):
    candidate=_norm(requested)
    if not candidate or candidate in ("null","none","невідомо","-"):
        return None
    return next((s for s in approved_projects(store) if _norm(s)==candidate),None)


def explicit_project_in_message(store,text):
    """Accept project only when named as a project, never by substring guess."""
    message=str(text or "")
    for name in sorted(approved_projects(store),key=len,reverse=True):
        pattern=(r"(?:\bпроєкт(?:у|а|і|ом)?\b|"
                 r"\bпроект(?:у|а|і|ом)?\b)"
                 r"\s*[:\-—]?\s*"+re.escape(name)+r"(?=$|[\s,.;!?])")
        if re.search(pattern,message,re.IGNORECASE):
            return name
    return None



def unique_explicit_project(store,text):
    """Return project only if ONE distinct approved project is explicitly named.

    Multi-document messages may mention different projects. Never silently
    assign the project of a paid-card purchase to an unrelated invoice.
    """
    message=str(text or "")
    matches=[]
    for name in approved_projects(store):
        pattern=(r"(?:\bпроєкт(?:у|а|і|ом)?\b|"
                 r"\bпроект(?:у|а|і|ом)?\b)"
                 r"\s*[:\-—]?\s*"+re.escape(name)+r"(?=$|[\s,.;!?])")
        if re.search(pattern,message,re.IGNORECASE):
            matches.append(name)
    return matches[0] if len(matches)==1 else None

def select_project_message(store,purpose="запису"):
    names=approved_projects(store)
    return (
        "На який проєкт віднести "+purpose+"?\n"
        + "\n".join(f"{i}. {name}" for i,name in enumerate(names,1))
        + "\n\nНадішли номер або точну назву проєкту. "
        "Якщо потрібного немає, напиши «новий: назва проєкту». "
        "Новий проєкт затверджує тільки власник."
    )


def select_project_answer(store,text):
    options=approved_projects(store)
    answer=str(text or "").strip()
    if answer.isdigit():
        index=int(answer)-1
        return options[index] if 0<=index<len(options) else None
    return next((name for name in options if _norm(name)==_norm(answer)),None)


def requested_new_project(text):
    s=str(text or "").strip()
    if not re.match(r"^новий(?:\s+проєкт)?\s*[:：-]\s*",s,re.IGNORECASE):
        return None
    name=re.sub(r"^новий(?:\s+проєкт)?\s*[:：-]\s*","",s,flags=re.IGNORECASE).strip()
    if not name or len(name)>90:
        raise ValueError("Напиши назву нового проєкту (до 90 символів).")
    return name


def approve_new_project(store,record_id,owner):
    headers,_,row=store._lookup("Запити на проєкти",record_id)
    data={h:str(row[i]).strip() if i<len(row) else "" for i,h in enumerate(headers)}
    name=data.get("Назва проєкту","")
    if not name:
        raise ValueError("У запиті немає назви проєкту.")
    status=data.get("Статус","")
    if status=="Відхилено":
        raise ValueError("Відхилений запит не можна затверджувати повторно.")
    if status=="Погоджено":
        return False,name
    if status!="Очікує погодження":
        raise ValueError("Цей запит не очікує погодження.")
    found=canonical_project(store,name)
    if found:
        raise ValueError("Проєкт «"+found+"» уже існує. Перевір довідник.")
    # Deterministic ID prevents duplicate projects if Telegram retries.
    project_id="PR-"+record_id
    created=store.append_record("Проєкти",{
        "Назва проєкту":name,"Статус":"Діючий",
        "Примітка":"Погоджено власником за запитом "+record_id,
    },project_id,owner)
    try:
        store.update_field("Запити на проєкти",record_id,"Статус","Погоджено")
        store.update_field("Запити на проєкти",record_id,"Погодив",owner)
    except Exception:
        raise ValueError("Проєкт додано, але статус запиту не оновлено. "
                         "Перевір «Проєкти» перед повторною дією.")
    return created,name
