"""Telegram inline-keyboard UI for owner-only access management.

No permissions are modified by this module. Every grant/revoke is performed
by the trusted backend only after the owner's explicit confirmation click.
"""
PAGE_SIZE=7

MANAGED_TABS=(
    "Заявки", "Закупка", "Склад", "Доручення",
    "Рахунки", "Накладні", "Проєкти",
)
READ_ONLY_TABS=frozenset(("Проєкти","Запити на проєкти","Правила Шухера"))
PRESETS={
    "buy":("Закупівельник",{
        "Заявки":"read","Закупка":"write","Рахунки":"write",
        "Проєкти":"read",
    }),
    "stock":("Комірник",{
        "Заявки":"write","Склад":"write","Накладні":"write",
        "Проєкти":"read",
    }),
    "pm":("Менеджер проєкту",{
        "Заявки":"write","Закупка":"read","Доручення":"write",
        "Проєкти":"read",
    }),
    "lead":("Начальник виробництва",{
        "Заявки":"write","Закупка":"read","Склад":"read",
        "Доручення":"write","Проєкти":"read",
    }),
}
MODE_LABEL={"read":"👁 Перегляд","write":"✏️ Перегляд і запис","none":"🚫 Без доступу"}
MODE_SHORT={"read":"👁","write":"✏️","none":"—"}


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


def access_summary(access):
    return "\n".join("• "+name+": "+MODE_LABEL[mode]
                     for name,mode in access.items() if mode in ("read","write")) or "Немає виданих прав"


def user_view(record,access):
    uid=str(record[0])
    text=(f"👤 {user_name(record)}\nID: {uid}\n\n"
          "Поточні доступи:\n"+access_summary(access)+
          "\n\nВибери набір прав або налаштуй кожну таблицю окремо.")
    rows=[
      [button("🛒 Закупівельник",f"su:p:{uid}:buy"),
       button("📦 Комірник",f"su:p:{uid}:stock")],
      [button("📋 Менеджер проєкту",f"su:p:{uid}:pm")],
      [button("🏭 Начальник виробництва",f"su:p:{uid}:lead")],
      [button("⚙️ Окремі доступи",f"su:m:{uid}:0")],
      [button("🚫 Забрати всі доступи",f"su:z:{uid}")],
      [button("⬅️ До списку", "su:l:0")],
    ]
    return text,keyboard(rows)


def managed_changes(existing,titles,preset_key):
    if preset_key not in PRESETS:
        raise ValueError("Невідомий шаблон ролі")
    name,desired=PRESETS[preset_key]
    present=[t for t in MANAGED_TABS if t in titles]
    changes=[(tab,existing.get(tab,"none"),desired.get(tab,"none"))
             for tab in present if existing.get(tab,"none")!=desired.get(tab,"none")]
    return name,changes


def preview_view(uid,title,changes,action):
    if changes:
        change_text="\n".join(
            f"• {tab}: {MODE_SHORT.get(old,'—')} → {MODE_SHORT.get(new,'—')}"
            for tab,old,new in changes
        )
    else:
        change_text="Змін не потрібно: права вже відповідають вибору."
    text=(f"🔐 Перевір налаштування для {title}\n\n"+change_text+
          "\n\nПідтвердити оновлення прав?")
    rows=[
      [button("✅ Підтвердити",action)],
      [button("❌ Скасувати",f"su:u:{uid}")]
    ]
    return text,keyboard(rows)


def manual_view(uid,titles,access,page):
    pages=max(1,(len(titles)+PAGE_SIZE-1)//PAGE_SIZE)
    page=min(max(0,int(page)),pages-1)
    rows=[]
    for index in range(page*PAGE_SIZE,min(len(titles),(page+1)*PAGE_SIZE)):
        name=titles[index]
        mode=access.get(name,"none")
        rows.append([button(f"{MODE_SHORT[mode]} {name}",f"su:t:{uid}:{index}:{page}")])
    nav=[]
    if page>0:nav.append(button("◀️",f"su:m:{uid}:{page-1}"))
    if page+1<pages:nav.append(button("▶️",f"su:m:{uid}:{page+1}"))
    if nav:rows.append(nav)
    rows.append([button("⬅️ Профіль користувача",f"su:u:{uid}")])
    return (f"⚙️ Доступи до таблиць (сторінка {page+1}/{pages})\n"
            "✏️ — запис, 👁 — перегляд, — — немає доступу.\n"
            "Натисни на назву таблиці."),keyboard(rows)


def modes_view(uid,title,idx,page,mode):
    choices=[
      [button("👁 Лише читання",f"su:x:{uid}:{idx}:read:{page}")],
    ]
    if title not in READ_ONLY_TABS:
        choices.append([button("✏️ Читання і запис",f"su:x:{uid}:{idx}:write:{page}")])
    choices.append([button("🚫 Забрати доступ",f"su:x:{uid}:{idx}:none:{page}")])
    choices.append([button("⬅️ Таблиці",f"su:m:{uid}:{page}")])
    return f"Таблиця: {title}\nПоточний доступ: {MODE_LABEL.get(mode,'—')}\nОбери новий режим.",keyboard(choices)
