"""Notification rules for the existing Doruchennia ledger.

Telegram recipients come only from registered users with current read permission.
A notification hash tracks changes in task fields, not background/metadata edits.
"""
import hashlib
import json

from outreach import named_recipients

WATCH_FIELDS = ("Виконавець", "Завдання", "Проєкт", "Пріоритет", "Дедлайн",
                "Статус", "Результат", "Коментар", "Постановник",
                "Очікуваний результат / критерій")


def plain_telegram_text(text):
    """Telegram messages use plain text, not rendered Markdown."""
    return str(text or "").replace("**", "")


def task_fields(headers, row):
    return {str(name): str(row[i]).strip() if i < len(row) else ""
            for i, name in enumerate(headers)}


def authorized_recipient(assignee, users, grants):
    """Return (Telegram ID, user row) for ONE registered task assignee.

    Last permission row wins; revoked users and write-only users cannot read
    tasks. No arbitrary numeric ID or model-generated recipient is trusted.
    """
    name = str(assignee or "").strip()
    if not name:
        return None
    matches = named_recipients("Напиши " + name, users)
    if len(matches) != 1:
        return None
    user = matches[0]
    uid = str(user[0])
    mode = "none"
    for row in grants[1:]:
        if (len(row) > 2 and str(row[0]) == uid
                and str(row[1]).casefold() == "доручення"):
            mode = str(row[2])
    if mode not in ("read", "write"):
        return None
    return int(uid), user


def task_marker(record_id, fields, recipient_id):
    values = [str(record_id), int(recipient_id)]
    values.extend(str(fields.get(name, "")) for name in WATCH_FIELDS)
    digest = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return "tasknotify:" + str(record_id) + ":" + str(recipient_id) + ":" + digest


def task_notice(fields, update=False):
    lines = ["Оновлено доручення" if update else "Нове доручення"]
    for key in ("ID", "Проєкт", "Завдання", "Пріоритет", "Дедлайн",
                "Статус", "Очікуваний результат / критерій", "Коментар",
                "Результат", "Постановник"):
        value = str(fields.get(key) or "").strip()
        if value:
            lines.append(key + ": " + value[:1300])
    lines.append("Деталі доступні через Шухера у розділі «Доручення».")
    return plain_telegram_text("\n".join(lines))
