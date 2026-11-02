"""Resolve registered assignees by name; never interpret user requests."""
import re
import unicodedata
KNOWN_ALIASES = {
    "yuriy": ("юра","юро","юрі","юрій","юрію","юрія","юре","юру",
              "yuriy","yuri","yurii","yura"),
    "sergey": ("сергій","сергію","сергія","сергей","сергею",
               "сергея","сергієм","sergey","serhii","sergii","sergiy"),
}


WORD_BODY_RE = re.compile(r"\bщо\b",re.IGNORECASE)


def _norm(text):
    return unicodedata.normalize("NFKC",str(text or "")).casefold().strip()


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
