"""Read-only invoice delivery to the owner; never submits anything for payment."""
import re
from io import BytesIO

from PIL import Image


def one_line(value, fallback=""):
    text=" ".join(str(value or "").replace("**", "").split())
    return (text or fallback)[:160]


def is_payable_request(text):
    text=" ".join(str(text or "").casefold().split())
    if text.split(" ",1)[0].split("@",1)[0] == "/invoices":
        return True
    # Read requests may put "мені" before the verb, use future tense, or
    # refer to the files without repeating "рахунки". Do not require a magic
    # phrase. Keep named third-party recipients and new submissions out.
    match=re.match(
        r"^(?:(?:шухер|ну|то|і|а|тоді|тепер|вже|ти|чи|можеш|зможеш|"
        r"будь ласка|будь-ласка|мені)[,!?\s]+)*"
        r"(?:покажи|показати|покажеш|дай|надай|надішли|надіслати|надішлеш|"
        r"скинь|скинеш|скинути|відправ|відправити|відправиш|перешли|перешлеш|"
        r"виведи|перелік|список|які)\b\s*(.*)$",text)
    if not match:
        return False
    tail=match[1].strip(" ,:?!")
    # After the verb, accept only a self-addressed or unaddressed object.
    # For example, "надішли Юрію рахунки" belongs to the staff workflow.
    if not re.match(
        r"(?:(?:мені|будь ласка|будь-ласка|всі|усі|ці|ті|мої|наші|всі ці|усі ці|"
        r"збережені|будь-які)[,\s]+)*"
        r"(?:рахун\w*|неоплачен\w*|оригінал\w*|pdf|пдф|файл\w*|фото|"
        r"документ\w*|скан\w*)\b",tail):
        return False
    # A request for paid/cancelled/history records must not return unpaid ones.
    if re.search(r"\b(?:оплачен\w*|сплачен\w*|скасован\w*|відхилен\w*|історі\w*)\b",tail):
        return False
    invoice=bool(re.search(r"\bрахун(?:ок|к\w*)\b",tail))
    media=bool(re.search(r"\b(?:pdf|пдф|файл\w*|фото|документ\w*|скан\w*)\b",tail))
    payment=bool(re.search(r"\b(?:до|на) оплат\w*\b",tail))
    return invoice or (media and payment)


def payable_rows(headers, rows):
    result=[]
    seen=set()
    for cells in rows:
        row={str(key).strip():str(cells[i] or "").strip() if i<len(cells) else ""
             for i,key in enumerate(headers)}
        rid=row.get("ID", "")
        if not rid or rid in seen:
            continue
        kind=row.get("Тип документа", "").casefold()
        if kind and kind not in ("рахунок", "рахунок на оплату", "рахунок-фактура", "invoice"):
            continue
        statuses=[one_line(row.get(field)).casefold().rstrip(".")
                  for field in ("Статус оплати", "Статус власника", "Статус")]
        if any(s in ("оплачено", "оплачений", "сплачено", "сплачений", "повністю оплачено", "paid")
               or s.startswith(("скасован", "відхилен", "анульован", "cancelled", "canceled", "rejected"))
               for s in statuses):
            continue
        seen.add(rid)
        result.append(row)
    return result


def invoice_caption(row, users=()):
    # "Передав Юрій" is a legacy column title, not proof that Yuriy submitted it.
    author=one_line(row.get("Автор") or row.get("Передав Юрій"))
    if author.startswith("@") or author.isdigit():
        matches=[person for person in users if len(person)>2 and
                 (str(person[0])==author or
                  (person[1] and ("@"+str(person[1]).lstrip("@")).casefold()==author.casefold()))]
        if len(matches)==1 and matches[0][2]:
            author=one_line(matches[0][2])
    return "\n".join((
        one_line(row.get("Постачальник"), "Постачальника не вказано"),
        one_line(row.get("Проєкт"), "Проєкт не вказано"),
        author or "Хто подав — не вказано",
    ))


def invoice_files(row):
    ids=[part for part in re.split(r"[,;\s]+",row.get("Telegram файли", "").strip()) if part]
    if not ids:
        raise ValueError("немає збереженого файлу рахунку")
    if len(ids)>12 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,320}", fid) for fid in ids):
        raise ValueError("некоректний перелік файлів рахунку")
    description=row.get("Документ / посилання", "")
    counts=re.fullmatch(r"Фото Telegram: (\d+); PDF: (\d+)", description)
    kinds=["unknown"]*len(ids)
    if counts and sum(map(int, counts.groups()))==len(ids):
        kinds=["photo"]*int(counts[1])+["pdf"]*int(counts[2])
    elif description.startswith("Окремі фото:"):
        kinds=["photo"]*len(ids)
    # Deduplicate only within a row; never combine unrelated financial records.
    return list(dict.fromkeys(zip(ids,kinds)))


async def send_invoice(row, chat, users, bot_call, fetch_file):
    """Keep PDFs as documents and photographs as photos, each with the caption."""
    caption=invoice_caption(row,users)
    documents=[]
    photos=[]
    for fid,kind in invoice_files(row):
        if kind=="pdf":
            documents.append(fid)
            continue
        raw=await fetch_file(fid,20_000_000 if kind=="unknown" else 6_000_000)
        if kind=="unknown" and raw.startswith(b"%PDF-"):
            documents.append(fid)
        else:
            # Upload photo bytes because an image originally sent as a document
            # cannot be resent as a Telegram photo using the document's file_id.
            with Image.open(BytesIO(raw)) as image:
                mime=Image.MIME.get(image.format)
                if mime not in ("image/jpeg", "image/png", "image/webp"):
                    raise ValueError("невідомий формат фото рахунку")
                image.verify()
            photos.append((raw,mime))
    for fid in documents:
        await bot_call("sendDocument",{"chat_id":chat,"document":fid,"caption":caption})
    for raw,mime in photos:
        extension={"image/jpeg":"jpg", "image/png":"png", "image/webp":"webp"}[mime]
        await bot_call("sendPhoto",{"chat_id":chat,"caption":caption},
                       {"photo":("Рахунок."+extension,raw,mime)})
    return len(documents)+len(photos)
