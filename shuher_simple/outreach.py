"""Owner-authorized direct Telegram messages to registered staff.

Recipient identity is resolved solely from the private _Shuher_Users rows
and the owner's own conversational turns. AI replies do not authorize a
recipient, and a request is never queued for later/background delivery.
"""
import re
import unicodedata

MAX_RECIPIENTS = 5

# Common Ukrainian inflections for staff already identified by their own
# Telegram first names. These are NOT Telegram IDs and cannot grant access.
KNOWN_ALIASES = {
    "yuriy": ("юра","юрі","юрій","юрію","юрія","юре","юру",
              "yuriy","yuri","yurii","yura"),
    "sergey": ("сергій","сергію","сергія","сергей","сергею",
               "сергея","сергієм","sergey","serhii","sergii","sergiy"),
}
VOCATIVES = {"yuriy": "Юрію", "sergey": "Сергію"}

SEND_RE = re.compile(
    r"^\s*(?:(?:а|ну|тоді|тепер|шухер|будь ласка)[,\s]+)*"
    r"(?:(?:чи|а)?\s*можеш\s+(?:(?:тепер|сам|уже|вже|просто)\s+)*)?"
    r"(?:напиши|написати|надішли|надіслати|відправ|відправити|"
    r"повідом|повідомити|передай|передати|скажи|сказати|скинь|скинути)"
    r"\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"\b(?:не|не треба|не потрібно|забороняю)\s+"
    r"(?:напиши|написати|надішли|надіслати|відправ|повідом|скажи|передай)",
    re.IGNORECASE,
)
PRONOUN_RE = re.compile(r"\b(?:їм|обом|кожному|хлопцям|їх|ним)\b",re.IGNORECASE)
WORD_BODY_RE = re.compile(r"\bщо\b",re.IGNORECASE)


def _norm(text):
    return unicodedata.normalize("NFKC",str(text or "")).casefold().strip()


def is_send_request(text):
    value=str(text or "").strip()
    if len(value)>2500 or NEGATION_RE.search(value):
        return False
    if re.search(r"\b(?:напиши|надішли|відправ)\s+мені\b",value,re.IGNORECASE):
        return False  # Asking for a draft for the owner, not a staff DM.
    return bool(SEND_RE.match(value))


def _forms(record):
    username=_norm(record[1]) if len(record)>1 else ""
    first=_norm(record[2]) if len(record)>2 else ""
    forms={username,first}
    # Only extend aliases for registered people with matching given name.
    aliases=KNOWN_ALIASES.get(first,())
    forms.update(aliases)
    return {form for form in forms if form and len(form)>=3}


def _occurs(value,form):
    return bool(re.search(r"(?<!\w)"+re.escape(form)+r"(?!\w)",value,re.UNICODE))


def _recipient_head(text):
    # Names in the message *body* are not additional recipients.
    value=str(text or "")
    before=WORD_BODY_RE.split(value,maxsplit=1)[0]
    if ":" in before:
        before=before.split(":",1)[0]
    return _norm(before)


def named_recipients(text,users):
    """Resolve unique registered users explicitly addressed by the owner."""
    value=_recipient_head(text)
    results=[]
    seen=set()
    for user in users:
        if not user or not str(user[0]).isdigit():
            continue
        uid=int(user[0])
        digits_match=bool(re.search(r"(?<!\d)"+str(uid)+r"(?!\d)",value))
        if digits_match or any(_occurs(value,form) for form in _forms(user)):
            if uid not in seen:
                results.append(user)
                seen.add(uid)
    return results


def choose_recipients(text,history,users):
    """Resolve from direct names or the last explicit owner mention.

    Never trust assistant-generated names, and never infer a target solely
    from his/her job, permission rows, or an unrelated message body.
    """
    direct=named_recipients(text,users)
    if direct:
        return direct
    if not PRONOUN_RE.search(text):
        return []
    # Historical references must be recent and originate from the owner.
    owner_turns=[item.get("content","") for item in history
                 if isinstance(item,dict) and item.get("role")=="user"]
    for prior in reversed(owner_turns[-6:]):
        found=named_recipients(prior,users)
        if found:
            return found
    return []


def _identity(record):
    first=_norm(record[2]) if len(record)>2 else ""
    return VOCATIVES.get(first,str(record[2] or record[1] or "колего").strip())


def _personalized_notice(record):
    return (
        f"Привіт, {_identity(record)}! Тобі вже відкрито доступ до Шухера. "
        "Можеш починати працювати. Надсилай мені робочі питання, заявки, "
        "фотографії та документи в межах своїх доступів — я допоможу "
        "їх опрацювати. Якщо даних бракуватиме, уточню."
    )


def _personalized_instructions(record):
    first=_norm(record[2]) if len(record)>2 else ""
    greeting=f"Привіт, {_identity(record)}! Можеш починати працювати з Шухером. "
    if first=="yuriy":
        return (
            greeting+"Для закупівель надсилай мені інформацію про замовлення, "
            "рахунки та фото оплат. У кожному рахунку обов'язково вказуй проєкт. "
            "Рахунки передаватиму власнику на перевірку; бухгалтерії їх не "
            "надсилаю без його рішення. Якщо товар новий і треба шукати "
            "постачальника — зазнач це."
        )
    if first=="sergey":
        return (
            greeting+"Надсилай потреби у закупівлях із проєктом, "
            "найменуванням і кількістю. Для надходжень фотографуй видаткові "
            "накладні, вказуй проєкт і фактично отриману кількість. "
            "Якщо чогось бракує, я уточню."
        )
    return _personalized_notice(record)


def _message_body(text):
    value=str(text or "").strip()
    match=WORD_BODY_RE.search(value)
    if match:
        return value[match.end():].lstrip(" ,:—-").strip().rstrip(".")
    if ":" in value:
        return value.split(":",1)[1].strip().rstrip(".")
    return ""


def compose_message(request,record):
    """Simple contextual messages are immediate; custom instructions are relayed."""
    lower=_norm(request)
    body=_norm(_message_body(request))
    if (
        re.search(r"\b(?:починати|почати|розпочати)\s+працювати\b",lower)
        or ("можуть" in lower and "працювати" in lower)
        or ("надсилати" in lower and "все" in lower and "треба" in lower)
    ):
        return _personalized_notice(record)
    if "інструкці" in lower and not body:
        return _personalized_instructions(record)
    exact=_message_body(request)
    if exact:
        if len(exact)>1900:
            raise ValueError("Повідомлення завелике. Скороти текст до 1900 символів.")
        return f"Привіт, {_identity(record)}! Повідомлення від керівника: {exact}"
    return None


def delivery_plan(text,history,users,owner_ids=()):
    """Build one DM per unique Telegram ID or return an actionable error."""
    if not is_send_request(text):
        return None
    recipients=choose_recipients(text,history,users)
    if not recipients:
        raise ValueError("Кому саме написати? Назви працівників, наприклад: «Юрію та Сергію».")
    if len(recipients)>MAX_RECIPIENTS:
        raise ValueError("За одне доручення можна написати максимум п'ятьом працівникам.")
    if any(int(item[0]) in owner_ids for item in recipients):
        raise ValueError("Надсилання самому власнику через доручення не потрібне.")
    plan=[]
    for user in recipients:
        message=compose_message(text,user)
        if not message:
            raise ValueError("Що саме передати працівникам? Напиши текст після «що».")
        label="@"+str(user[1]) if len(user)>1 and user[1] else str(user[2])
        plan.append({"id":int(user[0]),"name":label,"text":message})
    return plan
