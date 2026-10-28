"""Pure business rules for Shuher Lite: procurement, inventory and allowed statuses."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

STATUSES = {
    "Заявки": {
        "Статус": ("Нова", "У закупці", "Частково виконана", "Виконана", "Скасована"),
    },
    "Закупка": {
        "Статус оплати": ("Не оплачено", "На оплаті", "Частково оплачено", "Оплачено", "Скасовано"),
        "Статус замовлення": ("До замовлення", "Замовлено", "В дорозі", "Частково отримано", "Отримано", "Скасовано"),
    },
    "Доручення": {
        "Статус": ("Нове", "У роботі", "Очікує", "Виконано", "Скасовано"),
    },
}
STOCK_TYPES = ("Надходження", "Видача")


def positive_quantity(value):
    if isinstance(value, bool):
        raise ValueError("Кількість має бути додатним числом.")
    cleaned = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise ValueError("Кількість має бути додатним числом.")
    if not number.is_finite() or number <= 0:
        raise ValueError("Кількість має бути більшою за нуль.")
    return number


def number_text(number):
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")


def stock_key(name, characteristics, unit):
    return tuple(str(x or "").strip().casefold() for x in (name, characteristics, unit))


def stock_balances(headers, rows):
    """Sum receipts less issues by exact normalized name/specification/unit."""
    col = {str(h).strip(): i for i, h in enumerate(headers)}
    required = ("Тип операції", "Найменування", "Кількість", "Од. вим.")
    if not all(k in col for k in required):
        raise ValueError("У вкладці «Склад» не вистачає колонок для розрахунку залишків.")
    groups = defaultdict(Decimal)
    labels = {}
    skipped = 0
    for row in rows:
        def get(name):
            index = col.get(name)
            return str(row[index]).strip() if index is not None and index < len(row) else ""
        kind = get("Тип операції")
        name = get("Найменування")
        unit = get("Од. вим.")
        spec = get("Характеристики")
        try:
            if kind not in STOCK_TYPES or not name or not unit:
                raise ValueError("Недостатньо даних руху")
            amount = positive_quantity(get("Кількість"))
        except ValueError:
            skipped += 1
            continue
        key = stock_key(name, spec, unit)
        groups[key] += amount if kind == "Надходження" else -amount
        labels.setdefault(key, (name, spec, unit))
    return groups, labels, skipped


def allowed_status(table, field, proposed):
    fields = STATUSES.get(table, {})
    canonical_field = next((f for f in fields if f.casefold() == str(field).strip().casefold()), None)
    if not canonical_field:
        raise ValueError("Це поле не є керованим статусом. Доступні: " + ", ".join(fields or {"Статус": ()}))
    canonical_value = next((v for v in fields[canonical_field]
                            if v.casefold() == str(proposed).strip().casefold()), None)
    if not canonical_value:
        raise ValueError("Доступні значення: " + ", ".join(fields[canonical_field]))
    return canonical_field, canonical_value


def procurement_from_request(values, request_id):
    return {
        "ID заявки": request_id,
        "Проєкт": values.get("Проєкт", ""),
        "Найменування": values.get("Найменування", ""),
        "Кількість": values.get("Кількість", ""),
        "Од. вим.": values.get("Од. вим.", ""),
        "Статус оплати": "Не оплачено",
        "Статус замовлення": "До замовлення",
        "Коментар": "Створено автоматично із заявки " + request_id,
    }


def today_procurement(headers, rows, date):
    col = {str(h).strip(): i for i, h in enumerate(headers)}
    def cell(row, key):
        i = col.get(key)
        return str(row[i]).strip() if i is not None and i < len(row) else ""
    today = [row for row in rows if cell(row, "Дата").startswith(date)]
    lines = [f"Закупка за {date}: {len(today)} позицій."]
    for row in today[-35:]:
        name = cell(row, "Найменування") or "(без назви)"
        qty = cell(row, "Кількість")
        unit = cell(row, "Од. вим.")
        request_id = cell(row, "ID заявки") or "без заявки"
        status = cell(row, "Статус замовлення")
        lines.append(f"• {name} — {qty} {unit}; {status}; заявка {request_id}")
    if len(today) > 35:
        lines.append("Показано останні 35 позицій. Повний список: /export Закупка")
    return "\n".join(lines)
