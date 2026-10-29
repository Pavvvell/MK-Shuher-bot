"""Compact Telegram permission matrix. Only the owner may use the callbacks.

Legacy "write" grants remain read+write. New "write_only" grants permit
submitting records but not browsing or exporting existing records.
"""
import hashlib

PAGE_SIZE=7
READ_ONLY_TABS=frozenset(("Проєкти","Запити на проєкти","Правила Шухера"))
MODES=("read","write_only","write","none")
MODE_CODE={"read":"r","write_only":"w","write":"b","none":"n"}
CODE_MODE={code:mode for mode,code in MODE_CODE.items()}
MODE_LABEL={
    "read":"Читання","write_only":"Запис","write":"Читання + запис",
    "none":"Без доступу",
}
MODE_SHORT={"read":"Читання","write_only":"Запис","write":"Ч+З","none":"✕"}


def can_read(mode):
    return mode in ("read","write")


def can_write(mode):
    return mode in ("write","write_only")


def button(text,callback):
    if len(callback.encode("utf-8"))>64:
        raise ValueError("Telegram callback_data too long")
    return {"text":text,"callback_data":callback}


def keyboard(rows):
    return {"inline_keyboard":rows}


def user_name(row):
    if not row:
        return "Невідомий користувач"
    return ("@"+str(row[1])) if str(row[1]).strip() else str(row[2] or row[0])


def token_for(title):
    return hashlib.sha256(str(title).encode("utf-8")).hexdigest()[:12]


def title_for(titles,token):
    found=[title for title in titles if token_for(title)==token]
    if len(found)!=1:
        raise ValueError("Вкладку не знайдено. Відкрий список заново.")
    return found[0]


def list_view(users,page):
    total=len(users)
    pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    page=min(max(0,int(page)),pages-1)
    subset=users[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    rows=[[button("👤 "+user_name(record)[:40],"su:u:"+str(record[0]))]
          for record in subset]
    nav=[]
    if page>0:nav.append(button("◀️ Назад",f"su:l:{page-1}"))
    if page+1<pages:nav.append(button("Далі ▶️",f"su:l:{page+1}"))
    if nav:rows.append(nav)
    text=f"👥 Користувачі Шухера: {total}\nСторінка {page+1}/{pages}.\nНатисни на людину, щоб керувати доступом."
    return text,keyboard(rows)


def _short_title(title):
    # Telegram buttons must fit on phones. Full names also appear in
    # numbered table list above, so truncation never hides identity.
    return title if len(title)<=15 else title[:14]+"…"


def user_view(record,access,titles):
    """One row per live table: revoke/name, read, write-only and read+write."""
    uid=str(record[0])
    rows=[]
    for title in titles:
        current=access.get(title,"none")
        token=token_for(title)
        first=("✕ " if current!="none" else "— ")+_short_title(title)
        row=[button(first,f"su:q:{uid}:{token}:n")]
        for mode in ("read","write_only","write"):
            if title in READ_ONLY_TABS and mode!="read":
                row.append(button("🔒",f"su:d:{uid}"))
            else:
                marker="✓" if current==mode else ""
                row.append(button(marker+MODE_SHORT[mode],
                                  f"su:q:{uid}:{token}:{MODE_CODE[mode]}"))
        rows.append(row)
    rows.append([button("⬅️ До користувачів","su:l:0")])
    text=(
        f"👤 {user_name(record)}\nTelegram ID: {uid}\n\n"
        "Усі дозволи — в одному списку нижче.\n\n"
        "Читання — перегляд даних; Запис — додавання без перегляду; "
        "Ч+З — перегляд і запис. ✓ — чинний режим. "
        "✕ біля назви прибирає доступ. 🔒 — тільки читання.\n"
        "Натискання одразу змінює право доступу."
    )
    return text,keyboard(rows)


