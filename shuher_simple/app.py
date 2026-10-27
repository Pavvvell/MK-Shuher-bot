"""Шухер Lite: Telegram + OpenAI + Google Sheets. No database or Mini App."""
import asyncio
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

async def parse_request(text,table,available,force=None):
    system=(
        "Ти маршрутизатор складського Telegram-бота Шухер. "
        "Відповідай ЛИШЕ JSON-об'єктом з ключами "
        "intent ('read','append','other'), table (назва доступної таблиці або ''), "
        "values (об'єкт колонка:значення), question (запит користувача). "
        "НІКОЛИ не створюй таблиці, не змінюй права користувачів і не видаляй записи. "
        "Створення таблиць можливе тільки командою власника /create. "
        "Якщо прохання схоже на новий запис, intent='append'; "
        "пошук/перелік/питання -> 'read'. Якщо сумнів — 'read'. "
        "Не вигадуй значення, яких не сказав користувач. "
        "Для append вказуй лише назви колонок із переданого списку. "
        "Колонки ID, Дата й Автор заповнить сервер. "
        "Доступні таблиці: "+json.dumps(list(available),ensure_ascii=False)+". "
        "Поточна таблиця: "+table+". "
        "Колонки поточної таблиці: "+json.dumps((await sheet(store.read_table,table))[0],ensure_ascii=False)
    )
    parsed=await ask_model([
        {"role":"system","content":system},
        {"role":"user","content":text+"\nПРИМУСОВИЙ_РЕЖИМ: "+(force or "auto")}
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
               "/add опис — додати з підтвердженням; /cancel — скасувати; "
               "/export — завантажити таблицю.")
        if owner:
            intro+=("\n\nКерування лише для власника:\n"
                    "/users — усі користувачі та їхні ID\n"
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
    if cmd=="/cancel":
        await sheet(store.clear_pending,uid)
        await send(chat,"Незавершений запис скасовано.")
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
    text=(msg.get("text") or "").strip()
    if not text:return
    fresh=await sheet(store.register,person)
    if fresh and uid not in owners:
        await notify_owners(uid,person)
    try:
        if text.startswith("/"):
            command_result=await handle_command(uid,chat,person,text)
            if command_result is not False:return
            if text.split()[0].split("@",1)[0].casefold() not in ("/ask","/add"):
                await send(chat,"Невідома команда. /help")
                return
        pending=await sheet(store.get_pending,uid)
        if pending and text.casefold() in ("так","да","yes","підтверджую"):
            table,mode=await sheet(choose,uid,pending["table"])
            if mode!="write":
                await send(chat,"Право запису вже відкликано. /cancel")
                return
            payload=pending["payload"]
            added=await sheet(store.append_record,table,payload["values"],
                              payload["record_id"],user_label(person))
            await sheet(store.clear_pending,uid)
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
        force=None
        if text.lower().startswith("/ask "):
            table,_=await sheet(choose,uid,"")
            await send(chat,await read_answer(uid,table,text[5:].strip()))
            return
        if text.lower().startswith("/add "):
            force="append";text=text[5:].strip()
        table,mode=await sheet(choose,uid,"")
        # Ordinary language is interpreted only within the currently selected table.
        intent,values,question=await parse_request(text,table,list((await sheet(permission,uid))),force)
        if intent=="read":
            await send(chat,await read_answer(uid,table,question))
            return
        if intent=="append":
            if mode!="write":
                await send(chat,"Для цієї таблиці маєш лише право читання.")
                return
            headers,_=await sheet(store.read_table,table)
            allowed={str(h).casefold():h for h in headers if h not in ("ID","Дата","Автор")}
            clean={}
            for k,v in values.items():
                match=allowed.get(str(k).casefold())
                if match and isinstance(v,(str,int,float,bool)):
                    clean[match]=str(v)
            if not clean:
                await send(chat,"Не вдалося визначити, що записати. Вкажи назву, кількість та інші дані.")
                return
            record_id="SH-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper()
            await sheet(store.save_pending,uid,table,{"record_id":record_id,"values":clean})
            preview="\n".join(f"{k}: {v}" for k,v in clean.items())
            await send(chat,"Записати в «"+table+"»?\n"+preview+
                            "\n\nВідповідь: так / ні. Чернетка діє 30 хв.")
            return
        await send(chat,"Я можу знайти дані або додати рядок у вибрану таблицю. "
                        "Спробуй /ask запит або /add опис. Нові таблиці створює лише власник.")
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
