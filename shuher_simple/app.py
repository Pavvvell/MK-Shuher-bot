"""Шухер Lite: Telegram + OpenAI + Google Sheets. No database or Mini App."""
import asyncio
import base64
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from openai import AsyncOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict

from store import SheetsStore
from business import today_procurement, positive_quantity
from dialogue import queue_photo, remember_exchange, history_excerpt
from media import (MAX_PHOTO_BYTES, telegram_file_bytes, transcribe_telegram_voice)
from natural import interpret_natural, status_candidates, status_selection_text

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("shuher-lite")
KYIV=ZoneInfo("Europe/Kyiv")

class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_file=".env",extra="ignore")
    telegram_bot_token:str
    telegram_webhook_secret:str
    openai_api_key:str
    openai_model:str="gpt-4.1-mini"
    spreadsheet_id:str
    owner_ids:str
    google_service_account_json_b64:str=""
    register_telegram_webhook:bool=False
    public_base_url:str=""

settings=Settings()
owners={int(x.strip()) for x in settings.owner_ids.split(",") if x.strip()}
if not owners:
    raise RuntimeError("OWNER_IDS must contain at least one Telegram numeric ID.")
store=SheetsStore(settings.spreadsheet_id,settings.google_service_account_json_b64)
openai=AsyncOpenAI(api_key=settings.openai_api_key,timeout=35)
telegram_api=f"https://api.telegram.org/bot{settings.telegram_bot_token}"
inflight=asyncio.Lock()

async def sheet(method,*args):
    return await asyncio.to_thread(method,*args)

async def bot_call(method,payload=None,files=None):
    async with httpx.AsyncClient(timeout=45) as client:
        response=await client.post(
            telegram_api+"/"+method,
            json=payload if files is None else None,
            data=payload if files is not None else None,
            files=files)
        response.raise_for_status()
        answer=response.json()
        if not answer.get("ok"):
            raise RuntimeError("Telegram API відхилив запит "+method)
        return answer

async def send(chat,text):
    text=str(text or "Немає даних.")
    for start in range(0,len(text),3900):
        await bot_call("sendMessage",{"chat_id":chat,"text":text[start:start+3900]})

async def notify_owners(uid,person):
    tag="@"+person.get("username") if person.get("username") else person.get("first_name","новий користувач")
    for owner in owners:
        if owner==uid:continue
        try:
            await send(owner,f"Шухер: новий користувач {tag}. Telegram ID: {uid}. "
                             f"Надати доступ: /grant {uid} read НазваТаблиці "
                             f"або /grant {uid} write НазваТаблиці")
        except Exception:
            log.exception("Cannot notify owner of registration")

async def ask_model(messages,max_tokens=500,json_mode=False):
    params={"model":settings.openai_model,"messages":messages,
            "max_tokens":max_tokens,"temperature":0}
    if json_mode:
        params["response_format"]={"type":"json_object"}
    response=await openai.chat.completions.create(**params)
    return response.choices[0].message.content or ""

def user_label(user):
    return ("@"+user["username"]) if user.get("username") else user.get("first_name","Користувач")

def permission(uid):
    return store.permissions(uid,owners)

def choose(uid,desired):
    available=permission(uid)
    if not available:
        raise ValueError("Доступ до таблиць ще не надано. Власник побачить тебе в /users.")
    if desired:
        for x in available:
            if x.casefold()==desired.strip().casefold():
                return x,available[x]
        raise ValueError("Немає доступу до цієї таблиці. Перевір /tables.")
    registered=store.user(uid)
    active=registered[3] if registered else ""
    if active and active in available:
        return active,available[active]
    if len(available)==1:
        name=next(iter(available))
        return name,available[name]
    raise ValueError("Вибери таблицю командою /use НазваТаблиці.")

async def parse_request(text,table,available,force=None,history=""):
    system=(
        "Ти маршрутизатор складського Telegram-бота Шухер. "
        "Відповідай ЛИШЕ JSON-об'єктом з ключами "
        "intent ('read','append','other'), table (назва доступної таблиці або ''), "
        "values (об'єкт колонка:значення), question (запит користувача). "
        "НІКОЛИ не створюй таблиці, не змінюй права користувачів і не видаляй записи. "
        "Створення таблиць можливе тільки командою власника /create. "
        "Готуй новий запис ТІЛЬКИ якщо його попросили. "
        "Намір консультативний або обговорення варіантів — other. "
        "Пошук/перелік даних — read. Якщо сумнів — other. "
        "Не вигадуй значення, яких немає в повідомленні або попередньому діалозі. "
        "Коли користувач каже 'оформляй', бери уточнені характеристики "
        "із його попереднього діалогу, але не вигадуй чисел. "
        "Для append вказуй лише назви колонок із переданого списку. "
        "Колонки ID, Дата й Автор заповнить сервер. "
        "Доступні таблиці: "+json.dumps(list(available),ensure_ascii=False)+". "
        "Поточна таблиця: "+table+". "
        "Колонки поточної таблиці: "+json.dumps((await sheet(store.read_table,table))[0],ensure_ascii=False)
    )
    parsed=await ask_model([
        {"role":"system","content":system},
        {"role":"user","content":"Попередня розмова:\n"+str(history)[-2300:]+
            "\nНове повідомлення: "+text+"\nПРИМУСОВИЙ_РЕЖИМ: "+(force or "auto")}
    ],max_tokens=650,json_mode=True)
    try:
        obj=json.loads(parsed)
    except (ValueError,TypeError):
        raise ValueError("Не зміг розібрати запит. Спробуй /ask або /add.")
    if not isinstance(obj,dict):
        raise ValueError("Некоректна відповідь ШІ.")
    intent=force or obj.get("intent","other")
    chosen=obj.get("table","")
    if chosen and str(chosen).casefold()!=table.casefold():
        raise ValueError("Спочатку вибери потрібну таблицю через /use НазваТаблиці.")
    values=obj.get("values") if isinstance(obj.get("values"),dict) else {}
    return intent,values,str(obj.get("question") or text)

async def read_answer(uid,table,question):
    headers,rows=await sheet(store.read_table,table)
    meaningful=[t for t in re.findall(r"[\w-]{4,}",question.casefold())
                if t not in {"скільки","покажи","мені","таблиці","знайди","який","яких","потрібно","будь","ласка"}]
    found=[r for r in rows if any(t in " ".join(map(str,r)).casefold() for t in meaningful)] if meaningful else []
    selection=(found[-55:] if found else rows[-45:])
    payload={"table":table,"total_rows":len(rows),"matched_rows":len(found),
             "sample_size":len(selection),"columns":headers,"rows":selection}
    answer=await ask_model([
        {"role":"system","content":
         "Ти Шухер, асистент складу. Відповідай українською коротко й чітко, "
         "НЕ малюй текстову таблицю. Джерело — тільки передані дані Google Sheets. "
         "Якщо даних немає, так і скажи. Якщо показано вибірку, не роби висновків "
         "про всі рядки. Для довгих списків запропонуй команду /export. "
         "Не називай Telegram ID інших користувачів."},
        {"role":"user","content":"Запит: "+question+"\nДані: "+json.dumps(payload,ensure_ascii=False)[:18500]}
    ],max_tokens=800)
    return answer[:3300]


CHAT_SYSTEM=(
    "Ти Шухер — корисний українськомовний помічник на виробництві. "
    "Спілкуйся вільно, як звичайний ChatGPT: відповідай на загальні питання, "
    "аналізуй ідеї, допомагай вибирати інструменти й формулювати характеристики. "
    "Веди природний діалог, пам'ятай попередні уточнення в межах переданого контексту. "
    "Коли аналізуєш фото, розрізняй те, що справді видно, і припущення. "
    "Для ріжучих/абразивних дисків, електроінструментів та небезпечних операцій "
    "не гарантуй сумісність за фото: перевірка паспортних характеристик виробника, "
    "посадкового отвору, максимальних обертів і захисного обладнання обов'язкова. "
    "Не стверджуй, ніби маєш живий доступ до інтернету, актуальних цін чи каталогів. "
    "Відповідай чесно про невизначеність і за потреби уточнюй питання. "
    "Ти НЕ маєш можливості записувати або змінювати Google Sheets самостійно. "
    "Якщо після консультації людина хоче оформити заявку, порадь їй написати "
    "'оформляй заявку', і сервер покаже чернетку для підтвердження. "
    "Ніколи не оголошуй запис підтвердженим без фактичного серверного підтвердження."
)


async def chat_answer(uid,chat,text):
    """Open-ended GPT dialogue with bounded history and queued Telegram photos."""
    if not await sheet(permission,uid):
        await send(chat,"Для роботи з Шухером спочатку потрібен доступ від власника. "
                        "Напиши /start, щоб він побачив твій ID.")
        return
    state=await sheet(store.get_dialogue,uid)
    pictures=state["photos"]
    question=str(text or "").strip() or "Проаналізуй надіслані фотографії."
    input_text=(("Підписи до фото: "+state["notes"]+"\n") if state["notes"] else "")+question
    content=[{"type":"text","text":input_text[:4200]}]
    for item in pictures:
        data=await telegram_file_bytes(
            bot_call,settings.telegram_bot_token,item["file_id"],MAX_PHOTO_BYTES)
        url="data:"+item.get("mime","image/jpeg")+";base64,"+base64.b64encode(data).decode("ascii")
        content.append({"type":"image_url","image_url":{"url":url,"detail":"auto"}})
    messages=[{"role":"system","content":CHAT_SYSTEM}]
    for item in state["history"]:
        messages.append({"role":item["role"],"content":item["content"]})
    messages.append({"role":"user","content":content if pictures else input_text[:4200]})
    await bot_call("sendChatAction",{"chat_id":chat,"action":"typing"})
    answer=str(await ask_model(messages,max_tokens=1000) or "").strip()
    if not answer:
        answer="Не вдалося отримати відповідь. Спробуй переформулювати питання."
    next_state=remember_exchange(state,question,answer,len(pictures))
    await sheet(store.save_dialogue,uid,next_state)
    await send(chat,answer[:3700])


async def stage_append(uid,chat,person,table,text):
    """Build a draft from current text and prior discussion; never write yet."""
    resolved,mode=await sheet(choose,uid,table)
    if mode!="write":
        raise ValueError("Для додавання потрібен дозвіл на запис у «"+resolved+"».")
    state=await sheet(store.get_dialogue,uid)
    _,values,_=await parse_request(text,resolved,list((await sheet(permission,uid))),
                                   force="append",history=history_excerpt(state))
    headers,_=await sheet(store.read_table,resolved)
    allowed={str(h).casefold():h for h in headers if h not in ("ID","Дата","Автор")}
    clean={}
    for key,value in values.items():
        match=allowed.get(str(key).strip().casefold())
        if match and isinstance(value,(str,int,float,bool)):
            clean[match]=str(value).strip()
    if not clean:
        tip="Не вистачає даних для заявки. Скажи, що саме, скільки та для чого потрібно."
        await sheet(store.save_dialogue,uid,remember_exchange(state,text,tip))
        await send(chat,tip)
        return
    if resolved=="Заявки":
        missing=[f for f in ("Найменування","Кількість","Од. вим.") if not clean.get(f)]
        if missing:
            tip="Щоб оформити заявку, уточни: "+", ".join(missing)+"."
            await sheet(store.save_dialogue,uid,remember_exchange(state,text,tip))
            await send(chat,tip)
            return
        positive_quantity(clean["Кількість"])
    if resolved=="Склад":
        missing=[f for f in ("Тип операції","Найменування","Кількість","Од. вим.")
                 if not clean.get(f)]
        if missing:
            tip="Для складського руху уточни: "+", ".join(missing)+"."
            await sheet(store.save_dialogue,uid,remember_exchange(state,text,tip))
            await send(chat,tip)
            return
        positive_quantity(clean["Кількість"])
    record_id="SH-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper()
    await sheet(store.save_pending,uid,resolved,{"record_id":record_id,"values":clean})
    preview="\n".join(f"{key}: {value}" for key,value in clean.items())
    await send(chat,"Записати в «"+resolved+"»?\n"+preview+
                    "\n\nПідтверди: так / ні. Чернетка діє 30 хв.")


async def stage_status(uid,chat,table,record_id,field,value,label=""):
    """Save an explicit, user-confirmed, single-cell status change."""
    resolved,mode=await sheet(choose,uid,table)
    if mode!="write":
        raise ValueError("Для зміни статусу потрібен доступ на запис.")
    field,value,old=await sheet(store.prepare_status,resolved,record_id,field,value)
    if value==old:
        await send(chat,"Цей статус уже встановлено. Нічого не змінено.")
        return
    await sheet(store.save_pending,uid,resolved,{
        "kind":"status","record_id":record_id,"field":field,
        "value":value,"expected_old":old,
    })
    title=(label+"\n") if label else ""
    await send(chat,f"Змінити «{resolved}»?\n"+title+
               f"ID: {record_id}\n{field}: {old or 'порожньо'} → {value}\n"+
               "Підтверди: так / ні.")


async def handle_natural(uid,chat,person,text):
    """Classify normal messages; only explicit operations trigger confirmations."""
    access=await sheet(permission,uid)
    if not access:
        return False
    state=await sheet(store.get_dialogue,uid)
    action=await interpret_natural(text,access,uid in owners,ask_model,
                                   history_excerpt(state))
    intent=action["intent"]
    if intent=="other":
        return False
    if intent=="today":
        headers,rows=await sheet(store.read_table,"Закупка")
        date=datetime.now(KYIV).strftime("%d.%m.%Y")
        await send(chat,today_procurement(headers,rows,date))
        return True
    if intent=="balance":
        await send(chat,await sheet(store.balances,action["target"]))
        return True
    if intent=="read":
        await send(chat,await read_answer(uid,action["table"],text))
        return True
    if intent=="append":
        await stage_append(uid,chat,person,action["table"],text)
        return True
    table=action["table"]
    description=action["target"]
    if not description:
        await send(chat,"Яку саме позицію потрібно змінити? Вкажи назву або розмір.")
        return True
    headers,rows=await sheet(store.read_table,table)
    candidates=status_candidates(headers,rows,description)
    if not candidates:
        await send(chat,"Не знайшов позиції «"+description+"» у «"+table+
                   "». Уточни найменування, розмір або проєкт.")
        return True
    if len(candidates)>8:
        await send(chat,"Знайшов багато схожих позицій. Уточни розмір або проєкт.")
        return True
    if len(candidates)>1:
        await sheet(store.save_pending,uid,table,{
            "kind":"status_pick","field":action["field"],"value":action["value"],
            "candidates":candidates,
        })
        await send(chat,status_selection_text(candidates))
        return True
    chosen=candidates[0]
    await stage_status(uid,chat,table,chosen["id"],action["field"],action["value"],chosen["label"])
    return True


async def handle_command(uid,chat,person,text):
    first,_,arg=text.partition(" ")
    cmd=first.split("@",1)[0].casefold()
    arg=arg.strip()
    owner=uid in owners

    if cmd in ("/start","/help"):
        intro=("Шухер працює з Google Sheets. Замість довгих списків: "
               "коротка відповідь або /export (CSV-файл).\n"
               "/id — твій ID; /tables — доступні таблиці; "
               "/use Назва — вибрати; /ask запит — запитати GPT; "
               "/add опис — додати заявку; /cancel — скасувати чернетку; "
               "/new — нова розмова; /analyze — проаналізувати додані фото; "
               "/export — завантажити таблицю.\n"
               "Просто пиши, як у звичайний ChatGPT, надсилай фото або голосове. "
               "Наприклад: «який диск краще для сталі 3 мм?», "
               "«болти М8×30 замовили», «оформляй заявку». "
               "Будь-який запис — тільки після твого «так».")
        if owner:
            intro+=("\n\nКерування лише для власника:\n"
                    "/users — усі користувачі та їхні ID\n"
                    "/today — сьогоднішні заявки у закупівлю\n"
                    "/create Назва | Колонка1, Колонка2 — створити вкладку\n"
                    "/grant ID read Назва — лише перегляд\n"
                    "/grant ID write Назва — перегляд і додавання\n"
                    "/revoke ID Назва — забрати доступ")
        await send(chat,intro)
        return
    if cmd=="/id":
        await send(chat,f"Твій Telegram ID: {uid}")
        return
    if cmd=="/users":
        if not owner:
            await send(chat,"Лише власник може переглядати ID усіх користувачів.")
            return
        users=await sheet(store.users)
        lines=[f"{x[0]} · @{x[1]}" if x[1] else f"{x[0]} · {x[2]}"
               for x in users]
        await send(chat,"Користувачі Шухера:\n"+"\n".join(lines))
        return
    if cmd=="/tables":
        accessible=await sheet(permission,uid)
        if not accessible:
            await send(chat,"Поки немає доступних таблиць.")
            return
        rows=[name+" ("+("запис" if mode=="write" else "читання")+")"
              for name,mode in accessible.items()]
        message="Доступні таблиці:\n"+"\n".join(rows)
        if owner:
            message+="\n\nФайл: https://docs.google.com/spreadsheets/d/"+settings.spreadsheet_id+"/edit"
        await send(chat,message)
        return
    if cmd=="/use":
        if not arg:
            await send(chat,"Формат: /use НазваТаблиці")
            return
        table,_=await sheet(choose,uid,arg)
        await sheet(store.set_active,uid,table)
        await send(chat,f"Обрана таблиця: {table}. Тепер можеш запитувати або /add.")
        return
    if cmd in ("/new","/clear"):
        await sheet(store.clear_pending,uid)
        await sheet(store.clear_dialogue,uid)
        await send(chat,"Нова розмова. Попередній контекст і фото очищено, таблиці не змінені.")
        return
    if cmd=="/analyze":
        state=await sheet(store.get_dialogue,uid)
        if not state["photos"]:
            await send(chat,"Немає фото для аналізу. Надішли їх спочатку.")
            return
        await chat_answer(uid,chat,arg or "Проаналізуй надіслані фото й підписи.")
        return
    if cmd=="/cancel":
        await sheet(store.clear_pending,uid)
        await send(chat,"Незавершений запис скасовано.")
        return

    if cmd=="/today":
        if not owner:
            await send(chat,"Підсумок усіх закупівель доступний тільки власнику.")
            return
        headers,rows=await sheet(store.read_table,"Закупка")
        date=datetime.now(KYIV).strftime("%d.%m.%Y")
        await send(chat,today_procurement(headers,rows,date))
        return
    if cmd=="/balance":
        accessible=await sheet(permission,uid)
        if "Склад" not in accessible:
            await send(chat,"Немає доступу до складських залишків.")
            return
        await send(chat,await sheet(store.balances,arg))
        return
    if cmd=="/status":
        table,mode=await sheet(choose,uid,"")
        if mode!="write":
            await send(chat,"Для зміни статусу потрібен доступ на запис до вибраної таблиці.")
            return
        parts=[x.strip() for x in arg.split("|")]
        if len(parts)==2:
            record_id,proposed=parts
            field="Статус"
        elif len(parts)==3:
            record_id,field,proposed=parts
        else:
            await send(chat,"Формат: /status ID | Новий статус\n"
                            "Для закупівлі: /status ID | Статус замовлення | В дорозі\n"
                            "Або: /status ID | Статус оплати | Оплачено")
            return
        if not record_id or not field or not proposed:
            raise ValueError("Заповни ID, поле та новий статус.")
        field,proposed,old=await sheet(store.prepare_status,table,record_id,field,proposed)
        if proposed==old:
            await send(chat,"Цей статус уже встановлено. Нічого не змінено.")
            return
        await sheet(store.save_pending,uid,table,{
            "kind":"status","record_id":record_id,"field":field,
            "value":proposed,"expected_old":old,
        })
        await send(chat,f"Змінити «{table}», ID {record_id}?\n"
                        f"{field}: {old or 'порожньо'} → {proposed}\n"
                        "Відповідай «так» або «ні». Чернетка діє 30 хв.")
        return
    if cmd=="/export":
        table,_=await sheet(choose,uid,arg)
        csv_bytes=await sheet(store.csv_data,table)
        filename=re.sub(r"[^\w.-]+","_",table)[:55]+".csv"
        if len(csv_bytes)>15_000_000:
            await send(chat,"Завеликий експорт. Звернися до власника.")
            return
        await bot_call("sendDocument",{"chat_id":chat},
            {"document":(filename,csv_bytes,"text/csv")})
        return
    if cmd in ("/create","/grant","/revoke"):
        if not owner:
            await send(chat,"Цю команду може виконати тільки власник.")
            return
        if cmd=="/create":
            if not arg:
                await send(chat,"Формат: /create Назва | Колонка1, Колонка2\n"
                                "Без | створиться таблиця зі стандартними полями.")
                return
            title,sep,colspec=arg.partition("|")
            columns=[x.strip() for x in colspec.split(",") if x.strip()] if sep else None
            headers=await sheet(store.create_table,title,columns)
            await send(chat,f"Створено таблицю «{title.strip()}». Поля: "+", ".join(headers)+
                            "\nТепер /grant ID write "+title.strip())
            return
        if cmd=="/grant":
            match=re.fullmatch(r"(\d+)\s+(read|write)\s+(.+)",arg,re.IGNORECASE)
            if not match:
                await send(chat,"Формат: /grant 123456789 read Назва\n"
                                "або /grant 123456789 write Назва")
                return
            target=int(match.group(1));mode=match.group(2).lower();table=match.group(3)
            if target in owners:
                await send(chat,"Власники вже мають повний доступ.")
                return
            await sheet(store.grant,target,table,mode,uid)
            await send(chat,f"Доступ {mode} до «{table}» для ID {target} налаштовано.")
            try:
                await send(target,f"Шухер: тобі відкрито доступ до «{table}» ({mode}). /tables")
            except Exception:
                log.info("Cannot notify user %s; access already granted",target)
            return
        match=re.fullmatch(r"(\d+)\s+(.+)",arg)
        if not match:
            await send(chat,"Формат: /revoke 123456789 Назва")
            return
        target=int(match.group(1));table=match.group(2)
        if target in owners:
            await send(chat,"Доступ власника не відкликається цією командою.")
            return
        await sheet(store.grant,target,table,"none",uid)
        await send(chat,f"Доступ ID {target} до «{table}» відкликано.")
        return
    return False

async def handle_message(update):
    msg=update.get("message",{})
    person=msg.get("from")
    chatobj=msg.get("chat") or {}
    if not person or chatobj.get("type")!="private":
        return
    chat=chatobj["id"]
    uid=int(person["id"])
    text=(msg.get("text") or msg.get("caption") or "").strip()
    fresh=await sheet(store.register,person)
    if fresh and uid not in owners:
        await notify_owners(uid,person)
    try:
        # Each Telegram album photo arrives as a separate update with a
        # common media_group_id. Queue the IDs; analyse after a text question
        # (or /analyze), never respond independently to every album image.
        image_document=msg.get("document") or {}
        doc_mime=str(image_document.get("mime_type") or "")
        doc_is_image=doc_mime in ("image/jpeg","image/png","image/webp")
        if msg.get("photo") or doc_is_image:
            if not await sheet(permission,uid):
                await send(chat,"Щоб обговорювати фото, власник має надати тобі доступ. /start")
                return
            if await sheet(store.get_pending,uid):
                await send(chat,"Спочатку підтвердь або скасуй незавершений запис: так / ні.")
                return
            image=(msg["photo"][-1] if msg.get("photo") else image_document)
            if int(image.get("file_size") or 0)>MAX_PHOTO_BYTES:
                await send(chat,"Фото завелике. Надішли звичайне стиснене фото в Telegram.")
                return
            state=await sheet(store.get_dialogue,uid)
            state,first=queue_photo(state,image["file_id"],msg.get("media_group_id"),
                                    msg.get("caption") or "",
                                    doc_mime if doc_is_image else "image/jpeg")
            await sheet(store.save_dialogue,uid,state)
            if text and not msg.get("media_group_id"):
                await chat_answer(uid,chat,text)
            elif first:
                await send(chat,"Фото отримав. Можеш надіслати ще (до 6 штук). "
                                "Потім напиши, що треба з'ясувати, або /analyze.")
            return
        if msg.get("document") and not doc_is_image:
            await send(chat,"Поки розбираю фото JPEG/PNG/WebP. PDF та інші документи ще не підтримую.")
            return
        from_voice=bool(msg.get("voice"))
        if from_voice:
            if not await sheet(permission,uid):
                await send(chat,"Для голосових спочатку потрібен доступ від власника. /start")
                return
            text=await transcribe_telegram_voice(
                msg,bot_call,settings.telegram_bot_token,openai)
            await send(chat,"🎙️ Почув: "+text)
        if not text:
            return
        if text.startswith("/") and not from_voice:
            command_result=await handle_command(uid,chat,person,text)
            if command_result is not False:return
            if text.split()[0].split("@",1)[0].casefold() not in ("/ask","/add"):
                await send(chat,"Невідома команда. /help")
                return
        pending=await sheet(store.get_pending,uid)
        if pending and pending["payload"].get("kind")=="status_pick":
            payload=pending["payload"]
            if text.casefold() in ("ні","нет","no","скасувати"):
                await sheet(store.clear_pending,uid)
                await send(chat,"Скасовано, нічого не змінено.")
                return
            if text.isdigit():
                index=int(text)-1
                options=payload["candidates"]
                if not 0<=index<len(options):
                    await send(chat,"Відповідай номером від 1 до "+str(len(options))+".")
                    return
                await sheet(store.clear_pending,uid)
                chosen=options[index]
                await stage_status(uid,chat,pending["table"],chosen["id"],
                                   payload["field"],payload["value"],chosen["label"])
                return
            await send(chat,"Вибери номер позиції зі списку або «ні» для скасування.")
            return
        if pending and text.casefold() in ("так","да","yes","підтверджую"):
            table,mode=await sheet(choose,uid,pending["table"])
            if mode!="write":
                await send(chat,"Право запису вже відкликано. /cancel")
                return
            payload=pending["payload"]
            if payload.get("kind")=="status":
                changed=await sheet(store.update_status,table,payload["record_id"],
                                    payload["field"],payload["value"],payload["expected_old"])
                await sheet(store.clear_pending,uid)
                await send(chat,("Статус оновлено. ID "+payload["record_id"]+
                                 ": "+payload["field"]+" → "+payload["value"])
                           if changed else "Цей статус уже встановлено.")
                return
            added,linked=await sheet(store.append_workflow,table,payload["values"],
                                    payload["record_id"],user_label(person))
            await sheet(store.clear_pending,uid)
            if table=="Заявки":
                await send(chat,"Заявка "+payload["record_id"]+
                           " записана й автоматично пов'язана із закупівлею ZK-"+
                           payload["record_id"]+("." if linked else " (зв'язок уже існував)."))
            else:
                await send(chat,("Запис додано в «"+table+"». ID "+payload["record_id"])
                           if added else "Цей запис уже є в таблиці. Дублікат не створено.")
            return
        if pending and text.casefold() in ("ні","нет","no","скасувати"):
            await sheet(store.clear_pending,uid)
            await send(chat,"Скасовано. Нічого не записано.")
            return
        if pending:
            await send(chat,"Є незавершений запис. Відповідай «так» або «ні», "
                            "або /cancel для нового запиту.")
            return
        if text.lower().startswith("/ask "):
            table,_=await sheet(choose,uid,"")
            await send(chat,await read_answer(uid,table,text[5:].strip()))
            return
        if text.lower().startswith("/add "):
            table,_=await sheet(choose,uid,"")
            await stage_append(uid,chat,person,table,text[5:].strip())
            return
        state=await sheet(store.get_dialogue,uid)
        # Photos are always discussed first, never silently submitted
        # as a purchase request just because a caption says "купити".
        if state["photos"]:
            await chat_answer(uid,chat,text)
            return
        if await handle_natural(uid,chat,person,text):
            return
        # Default: no slash commands, no table selection, ordinary GPT chat.
        await chat_answer(uid,chat,text)
    except ValueError as exc:
        await send(chat,str(exc))
    except Exception:
        log.exception("Request failed for Telegram user %s",uid)
        await send(chat,"Помилка під час операції. Нічого не підтверджую; перевір таблицю й повтори.")

@asynccontextmanager
async def lifespan(app:FastAPI):
    await sheet(store.bootstrap)
    if settings.register_telegram_webhook:
        if not settings.public_base_url.startswith("https://"):
            raise RuntimeError("Set PUBLIC_BASE_URL to your real HTTPS Cloud Run URL")
        result=await bot_call("setWebhook",{
            "url":settings.public_base_url.rstrip("/")+"/telegram/webhook",
            "secret_token":settings.telegram_webhook_secret,
            "allowed_updates":["message"],
            "drop_pending_updates":False})
        log.info("Registered Telegram webhook: %s",result.get("ok"))
    yield

app=FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status":"ok","backend":"google-sheets"}

@app.post("/telegram/webhook")
async def webhook(request:Request,
    x_telegram_bot_api_secret_token:str|None=Header(default=None)):
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token,settings.telegram_webhook_secret):
        raise HTTPException(403,"Forbidden")
    body=await request.json()
    update_id=body.get("update_id")
    if not isinstance(update_id,int):
        raise HTTPException(400,"Invalid update")
    async with inflight:
        try:
            if await sheet(store.processed,update_id):
                return {"ok":True,"duplicate":True}
            await handle_message(body)
            await sheet(store.mark_processed,update_id)
        except Exception:
            log.exception("Failed to process Telegram update %s",update_id)
            raise HTTPException(503,"Telegram update not processed")
    return {"ok":True}
