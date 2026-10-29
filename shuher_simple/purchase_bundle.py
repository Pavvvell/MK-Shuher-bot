"""Safe independent classification of purchase photos and accounting documents.

Each image belongs to at most one financial operation. A photo of goods
cannot be used as invoice evidence, and bank-card payment evidence must
never be equated with a separate supplier invoice.
"""
from decimal import Decimal, InvalidOperation

IMAGE_KINDS=frozenset(("card_payment","goods_photo","invoice","other"))
GROUP_KINDS=frozenset(("card_purchase","invoice"))
ALLOWED_GROUP_IMAGES={
    "card_purchase":frozenset(("card_payment","goods_photo")),
    "invoice":frozenset(("invoice",)),
}
GROUP_IMAGE_REQUIRED={"card_purchase":"card_payment","invoice":"invoice"}


def money_amount(value):
    raw=str(value or "").strip().replace("\u00a0","").replace(" ","").replace(",",".")
    if not raw:
        return ""
    try:
        d=Decimal(raw)
    except InvalidOperation:
        raise ValueError("Суму документа не вдалось однозначно прочитати.")
    if not d.is_finite() or d<=0 or d>Decimal("1000000000"):
        raise ValueError("Некоректна сума документа.")
    return str(d)


def _clean(value,size=220):
    if isinstance(value,(str,int,float)):
        return str(value).strip()[:size]
    return ""


def validate_package(parsed,photo_count):
    """Validate model-proposed grouping without trusting its assumptions."""
    if not isinstance(parsed,dict):
        raise ValueError("Не вдалося визначити типи документів.")
    images=parsed.get("images")
    groups=parsed.get("groups")
    if not isinstance(images,list) or not isinstance(groups,list):
        raise ValueError("Не вдалося розділити фотографії за документами.")
    if not 1<=photo_count<=6 or len(images)!=photo_count:
        raise ValueError("Фото потрібно проаналізувати кожне окремо.")
    kinds={}
    for item in images:
        if not isinstance(item,dict):
            raise ValueError("Некоректна класифікація фотографій.")
        index=item.get("index")
        kind=item.get("type")
        if type(index) is not int or index<1 or index>photo_count or index in kinds:
            raise ValueError("Номери фото не збігаються.")
        if kind not in IMAGE_KINDS:
            raise ValueError("Невідомий тип документа — потрібно уточнення.")
        kinds[index]=kind
    if set(kinds)!=set(range(1,photo_count+1)):
        raise ValueError("Пропущено одне з фото — потрібна повторна перевірка.")
    used=set()
    records=[]
    for position,entry in enumerate(groups,1):
        if not isinstance(entry,dict) or entry.get("type") not in GROUP_KINDS:
            raise ValueError("Невизначена операція у пакеті документів.")
        typ=entry["type"]
        ids=entry.get("images")
        if not isinstance(ids,list) or not ids or any(type(i) is not int for i in ids):
            raise ValueError("У кожного запису мають бути конкретні фото.")
        if len(set(ids))!=len(ids) or any(i not in kinds or i in used for i in ids):
            raise ValueError("Одне фото не можна зараховувати до різних операцій.")
        roles={kinds[i] for i in ids}
        if not roles.issubset(ALLOWED_GROUP_IMAGES[typ]):
            raise ValueError("Фото оплати, товару і рахунку не можна змішувати.")
        if GROUP_IMAGE_REQUIRED[typ] not in roles:
            raise ValueError("Не знайдено підтвердження цього типу операції.")
        amount=money_amount(entry.get("amount",""))
        if not amount:
            raise ValueError("Не вказана сума фінансової операції. Уточни суму.")
        used.update(ids)
        records.append({
            "type":typ,
            "images":ids,
            "project":"", # Projects are set ONLY by the worker explicitly.
            "supplier":_clean(entry.get("supplier")),
            "amount":amount,
            "currency":_clean(entry.get("currency") or "грн",12),
            "description":_clean(entry.get("description"),500),
            "invoice_no":_clean(entry.get("invoice_no"),80),
            "quantity":_clean(entry.get("quantity"),120),
            "note":_clean(entry.get("note"),500),
            "ordinal":position,
        })
    if not records:
        raise ValueError("Не знайшов підтверджених фінансових документів.")
    unknown=[idx for idx in kinds if idx not in used]
    if any(kinds[i]!="other" for i in unknown):
        raise ValueError("Є нерозподілені фото оплати, товару чи рахунку. Уточни їх.")
    if len(records)>5:
        raise ValueError("Забагато незалежних операцій для одного пакета.")
    return {"images":kinds,"groups":records,"unassigned":unknown}


def missing_invoice_project(package):
    return next((g for g in package["groups"]
                 if g["type"]=="invoice" and not g.get("project")),None)


def apply_invoice_project(package,project):
    """Attach project ONLY to an invoice group, never to card-paid goods."""
    group=missing_invoice_project(package)
    if not group:
        return False
    group["project"]=project
    return True


def grouped_preview(package):
    lines=["Розділив фотографії на окремі операції:"]
    for i,entry in enumerate(package["groups"],1):
        title="🧾 РАХУНОК ДО ПЕРЕВІРКИ" if entry["type"]=="invoice" else "💳 ОПЛАЧЕНА ПОКУПКА КАРТКОЮ"
        lines.append(
            f"\n{i}. {title}\n"
            f"Фото: {', '.join('#'+str(k) for k in entry['images'])}\n"
            f"Магазин/постачальник: {entry['supplier'] or 'не визначено'}\n"
            f"Сума: {entry['amount']} {entry['currency']}\n"
            f"Опис: {entry['description'] or 'потрібне уточнення'}"
        )
        if entry["type"]=="invoice":
            lines.append("Проєкт: "+(entry["project"] or "НЕ ВКАЗАНО"))
            if entry["invoice_no"]:
                lines.append("Номер рахунку: "+entry["invoice_no"])
        else:
            lines.append("Проєкт покупки: "+(entry["project"] or "не вказано (не підставляю автоматично)"))
    if package["unassigned"]:
        lines.append("Фото без підтвердженої належності: "+
                     ", ".join("#"+str(i) for i in package["unassigned"]))
    return "\n".join(lines)


def validate_persistence(package):
    if missing_invoice_project(package):
        raise ValueError("У рахунку не вказаний підтверджений проєкт.")
    if package["unassigned"]:
        raise ValueError("Є невизначені фото: їх не можна мовчки відкидати.")
    return True
