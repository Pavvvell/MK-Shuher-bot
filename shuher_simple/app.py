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
from company import build_company_context
from knowledge import search_history, render_owner_rules
from receipt_pdf import photos_to_pdf
from purchase_bundle import (validate_package, missing_invoice_project,
                             apply_invoice_project, grouped_preview,
                             validate_persistence)
from dialogue import queue_photo, queue_document, clear_attachments, remember_exchange, history_excerpt
from access_ui import (button as access_button, keyboard as access_keyboard,
                       list_view, user_view, CODE_MODE, MODE_LABEL,
                       can_read, can_write)
from projects import (canonical_project, approved_projects, select_project_message,
                      select_project_answer, requested_new_project, approve_new_project,
                      explicit_project_in_message)
from media import (MAX_PHOTO_BYTES, telegram_file_bytes, transcribe_telegram_voice)
from natural import interpret_natural, status_candidates, status_selection_text
from outreach import is_send_request, delivery_plan, choose_recipients, MAX_RECIPIENTS
from llm import generate

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("shuher-lite")
KYIV=ZoneInfo("Europe/Kyiv")

class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_file=".env",extra="ignore")
    telegram_bot_token:str
    telegram_webhook_secret:str
    openai_api_key:str
    openai_model:str="gpt-6-astra"
    spreadsheet_id:str
    history_spreadsheet_id:str=""
    owner_ids:str
    google_service_account_json_b64:str=""
    register_telegram_webhook:bool=False
    public_base_url:str=""

settings=Settings()
owners={int(x.strip()) for x in settings.owner_ids.split(",") if x.strip()}
if not owners:
    raise RuntimeError("OWNER_IDS must contain at least one Telegram numeric ID.")
store=SheetsStore(settings.spreadsheet_id,settings.google_service_account_json_b64)
history_store=(SheetsStore(settings.history_spreadsheet_id,
                          settings.google_service_account_json_b64)
               if settings.history_spreadsheet_id else None)
openai=AsyncOpenAI(api_key=settings.openai_api_key,timeout=90,max_retries=1)
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
            await bot_call("sendMessage",{
                "chat_id":owner,
                "text":f"Шухер: новий користувач {tag}. Telegram ID: {uid}.\n"
                       "Натисни кнопку, щоб вибрати роль і права доступу.",
                "reply_markup":access_keyboard([
                    [access_button("🔐 Налаштувати доступ",f"su:u:{uid}")]
                ]),
            })
        except Exception:
            log.exception("Cannot notify owner of registration")

async def ask_model(messages,max_tokens=500,json_mode=False):
    return await generate(
        openai,settings.openai_model,messages,
        max_tokens=max_tokens,json_mode=json_mode,
    )

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
        "Коли в заявці потрібно відшукати новий товар, постачальника чи аналог, "
        "заповни колонку «Потрібен пошук» словом «Так», якщо вона є. "
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
    "Ти Шухер — універсальний українськомовний помічник підприємства. "
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
    "Ніколи не оголошуй запис підтвердженим без фактичного серверного підтвердження. "
    "Шухер може надсилати особисті Telegram-повідомлення зареєстрованим працівникам "
    "за прямим дорученням власника: цю дію виконує сервер, а не GPT у чаті. "
    "Не відповідай лише чернеткою, якщо власник попросив саме надіслати. "
    "Не стверджуй, що ти надіслав повідомлення, якщо сервер не підтвердив доставку. "
    "Не обіцяй написати потім або 'коли будуть онлайн': відкладених розсилок немає."

)


async def live_rules():
    try:
        _,rows=await sheet(store.read_table,"Правила Шухера")
        return render_owner_rules(rows)
    except Exception:
        log.exception("Cannot load Shuher knowledge rules")
        return ""


async def historical_answer(uid,chat,query):
    if history_store is None:
        await send(chat,"Історичний реєстр ще не підключений у налаштуваннях бота.")
        return
    access=await sheet(permission,uid)
    if not access:
        await send(chat,"Немає доступу до історичних розділів.")
        return
    result=await sheet(search_history,history_store,access,uid in owners,query)
    if not result["results"]:
        await send(chat,result.get("error","У старих записах немає підтвердженого збігу."))
        return
    payload=json.dumps(result,ensure_ascii=False)[:19000]
    response=await ask_model([
        {"role":"system","content":
         "Ти Шухер. Відповідай українською тільки за знайденими "
         "історичними рядками. Обов'язково зазначай, що ці дані "
         "ІСТОРИЧНІ й не підтверджують теперішню оплату, доставку, "
         "залишки чи актуальність постачальника. Наводь назву вкладки, "
         "ідентифікатор/дату або номер рядка, коли доступні. "
         "Якщо результати часткові або неоднозначні — скажи це. "
         "Не вигадуй відсутні факти й не перетворюй старі записи "
         "на нові операції. Не показуй інформацію поза переданими даними."},
        {"role":"user","content":"Питання: "+str(query)[:800]+"\nДані: "+payload},
    ],max_tokens=1150)
    await send(chat,response[:3650])


async def chat_answer(uid,chat,text):
    """Open-ended GPT dialogue with bounded history and queued Telegram photos."""
    access=await sheet(permission,uid)
    if not access:
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
    rules=await live_rules()
    messages=[{"role":"system","content":
               CHAT_SYSTEM+"\n\n"+build_company_context(uid in owners,access)+
               "\n\n"+rules}]
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
    if not can_write(mode):
        raise ValueError("Для додавання потрібен дозвіл на запис у «"+resolved+"».")
    if resolved=="Рахунки":
        await stage_invoice(uid,chat,person,text)
        return
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
        phrase=text.casefold()
        needs_search=any(x in phrase for x in (
            "треба знайти", "потрібно знайти", "знайти постачальника",
            "підібрати нов", "підшукати", "пошук постачальника",
            "шукати де купити", "немає постачальника", "знайти аналог",
        ))
        if needs_search:
            clean["Потрібен пошук"]="Так"
            clean["Тип потреби"]="Пошук і підбір"
        project=await sheet(canonical_project,store,clean.get("Проєкт"))
        if not project:
            await sheet(store.save_pending,uid,resolved,{
                "kind":"project_pick","values":clean,
                "record_id":"SH-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper(),
            })
            await send(chat,await sheet(select_project_message,store,"заявки"))
            return
        clean["Проєкт"]=project
        missing=[f for f in ("Найменування","Кількість","Од. вим.") if not clean.get(f)]
        if missing:
            tip="Щоб оформити заявку, обов’язково уточни: "+", ".join(missing)+"."
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


async def notify_procurement(created_by,record_id,values):
    """Share confirmed requests with authorized purchasers and flag searches."""
    project=values.get("Проєкт","")
    name=values.get("Найменування","")
    qty=values.get("Кількість","")
    unit=values.get("Од. вим.","")
    needs=str(values.get("Потрібен пошук","")).strip().casefold() in (
        "так","yes","true","потрібно","пошук")
    msg=("Нова заявка у закупівлю\n"+
         "Проєкт: "+project+"\nПозиція: "+name+
         "\nКількість: "+qty+" "+unit+
         "\nID: "+record_id)
    purchasers=await sheet(store.authorized_users_for_table,"Закупка")
    for telegram_id in purchasers:
        if telegram_id==created_by or telegram_id in owners:
            continue
        try:
            await send(telegram_id,msg+"\nЗаявка вже записана в «Закупка».")
        except Exception:
            log.exception("Cannot alert purchasing user %s",telegram_id)
    if needs:
        for owner in owners:
            try:
                await send(owner,"Шухер: ПОТРІБЕН ПОШУК нової позиції або постачальника!\n"+
                           msg+"\nТи можеш допомогти з пошуком у терміновому випадку.")
            except Exception:
                log.exception("Cannot alert owner about research request")


async def stage_project_request(uid,chat,person,name,reason=""):
    if not await sheet(permission,uid):
        await send(chat,"Щоб запропонувати проєкт, потрібен доступ до Шухера.")
        return
    name=str(name or "").strip()
    if not name or len(name)>90:
        await send(chat,"Напиши назву нового проєкту, наприклад: новий: Назва")
        return
    found=await sheet(canonical_project,store,name)
    if found:
        await send(chat,"Проєкт «"+found+"» уже є у довіднику.")
        return
    rid="NPR-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper()
    await sheet(store.save_pending,uid,"Запити на проєкти",{
        "kind":"project_request","record_id":rid,"name":name,"reason":reason[:300],
    })
    await send(chat,"Надіслати власнику запит на додавання проєкту «"+name+
               "»?\nПідтверди «так» або «ні». До погодження проєкт не можна "
               "використовувати для рахунків чи заявок.")


async def ask_owner_about_project(uid,chat,name,rid):
    for owner in owners:
        try:
            await send(owner,"Шухер: запит на НОВИЙ ПРОЄКТ\nНазва: "+name+
                       "\nПодав Telegram ID: "+str(uid)+
                       "\nID запиту: "+rid+
                       "\nПогодити: /projectapprove "+rid+
                       "\nВідмовити: /projectreject "+rid)
        except Exception:
            log.exception("Unable to notify owner of project request")


def text_mentions_invoice(text):
    candidate=str(text or "").casefold()
    return any(phrase in candidate for phrase in
        ("рахунок на оплат","рахунок до оплат","рахунок для оплат",
         "виставили рахунок","це рахунок","надсилаю рахунок",
         "скидаю рахунок","оплата карткою","оплата з карт",
         "скрін оплати","чек оплати","рахунок-фактура"))


def wants_purchase_bundle(text):
    words=str(text or "").casefold()
    return any(term in words for term in (
        "рахун","оплат","картк","чек","покупк","видатков",
        "документ","розбери","розділи","розподіли",
        "перше фото","перша фот","друге фото","друга фот",
        "третє фото","третя фот","фото 1","фото 2",
        "все,","все.","все!","це все","це всі",
    ))


async def stage_purchase_bundle(uid,chat,person,text,state=None):
    """Classify each purchaser photo, then ask *purchaser* what is missing.

    Never send an unreviewed bundle of unrelated images to the owner.
    A card charge and an independent invoice must not be compared or joined.
    """
    access=await sheet(permission,uid)
    if not can_write(access.get("Рахунки")):
        raise ValueError("Для подання документів потрібен дозвіл на запис у «Рахунки».")
    state=state or await sheet(store.get_dialogue,uid)
    photos=state.get("photos",[])
    if not photos:
        await send(chat,"Надішли фото чека, товару або рахунку. "
                        "Після останнього напиши «розбери фото».")
        return
    content=[{"type":"text","text":(
        "Це незалежні фотографії, які працівник подав для розбору. "
        "Ретельно визнач тип КОЖНОГО фото ЗА НОМЕРОМ. "
        "Не об'єднуй не пов'язані документи. "
        "Якщо фото №1 підтверджує оплату карткою, №2 показує "
        "оплачений ним товар, а №3 є окремим рахунком — "
        "це ДВІ ФІНАНСОВІ ОПЕРАЦІЇ, а не один рахунок на три фото. "
        "Валюта, суми та постачальники різних документів НЕ "
        "повинні звірятися як помилка, якщо власник чи працівник "
        "не заявили, що це одна операція. "
        "Не вигадуй приховані реквізити, суми, кількості, проєкти "
        "та зв'язки між документами. Проєкти призначає виключно "
        "сервер після явної відповіді працівника. "
        "Поверни ТІЛЬКИ JSON-об'єкт із двома полями: "
        "images = [{index:1,type:'card_payment|goods_photo|invoice|other'},...], "
        "groups = [{type:'card_purchase|invoice',images:[1,2],"
        "supplier:'',amount:'',currency:'грн',description:'',"
        "invoice_no:'',quantity:'',note:''},...]. "
        "Число groups означає число ФІНАНСОВИХ операцій, не фото. "
        "Кожне фото віднеси лише до ОДНІЄЇ операції. "
        "card_purchase обов'язково має фото оплати, "
        "invoice — фото окремого рахунку. "
        "goods_photo може бути доповненням до card_purchase, "
        "але ніколи не invoice. "
        "Якщо не впевнений у типі, познач other. "
        "Текст працівника (джерело для групування): "
        +str(text or "")[:1800]+
        "\nПідписи до фото (джерело): "+str(state.get("notes") or "")[:1200]
    )}]
    for i,photo in enumerate(photos,1):
        raw=await telegram_file_bytes(
            bot_call,settings.telegram_bot_token,photo["file_id"],MAX_PHOTO_BYTES)
        content.append({"type":"text","text":"ОКРЕМЕ ФОТО №"+str(i)})
        content.append({"type":"image_url","image_url":{
            "url":"data:"+photo.get("mime","image/jpeg")+";base64,"+
                  base64.b64encode(raw).decode("ascii"),
            "detail":"auto",
        }})
    await bot_call("sendChatAction",{"chat_id":chat,"action":"typing"})
    raw_answer=await ask_model([
        {"role":"system","content":
         "Ти обережний обліковець документів. "
         "Фотографії НЕ є однією транзакцією за замовчуванням. "
         "Відповідай виключно JSON, не стверджуй факт оплати рахунку "
         "на підставі іншого фото банківської картки."},
        {"role":"user","content":content},
    ],max_tokens=1450,json_mode=True)
    try:
        package=validate_package(json.loads(raw_answer),len(photos))
    except (ValueError,TypeError,KeyError) as exc:
        await send(chat,"Не можу надійно розділити документи: "+str(exc)+
                        " Уточни, які номери фото належать одній покупці "
                        "й які є окремими рахунками. Нічого не записано.")
        return
    invoices=[g for g in package["groups"] if g["type"]=="invoice"]
    if len(invoices)==1:
        project=await sheet(explicit_project_in_message,store,text)
        if project:
            invoices[0]["project"]=project
    rid="PB-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper()
    await sheet(store.save_pending,uid,"Рахунки",{
        "kind":"purchase_bundle","bundle_id":rid,
        "package":package,"photos":photos,
    })
    preview=grouped_preview(package)
    if missing_invoice_project(package):
        await send(chat,preview+"\n\nДля рахунку (НЕ для оплати карткою) "+
                   "уточни проєкт.\n"+
                   await sheet(select_project_message,store,"рахунку"))
    else:
        await send(chat,preview+"\n\nПідтверди «так», щоб записати окремі "
                   "операції й надіслати власнику структурований підсумок. "
                   "Можеш також пояснити, якщо я неправильно розділив фото.")


async def commit_purchase_bundle(uid,chat,person,payload):
    """Confirm the whole package once, preserving source-photo boundaries.

    Stable per-group IDs let interrupted retries finish without duplicates.
    """
    package=payload["package"]
    validate_persistence(package)
    access=await sheet(permission,uid)
    if not can_write(access.get("Рахунки")):
        raise ValueError("Право подавати рахунки відкликано.")
    if (any(g["type"]=="card_purchase" for g in package["groups"])
            and not can_write(access.get("Покупки карткою"))):
        raise ValueError("Для карткових покупок немає права запису в "
                         "«Покупки карткою». Попроси власника відкрити його.")
    photos=payload["photos"]
    outcomes=[]
    author=user_label(person)
    for index,entry in enumerate(package["groups"],1):
        ids=[photos[i-1]["file_id"] for i in entry["images"]]
        record_id=payload["bundle_id"]+"-"+str(index)
        if entry["type"]=="invoice":
            values={
                "Проєкт":entry["project"],
                "Постачальник":entry["supplier"],
                "Номер рахунку":entry["invoice_no"],
                "Сума":entry["amount"],
                "Валюта":entry["currency"],
                "Коментар":entry["description"]+
                            ("; "+entry["note"] if entry["note"] else ""),
                "Тип документа":"Рахунок",
                "Документ / посилання":"Окремі фото: "+
                            ", ".join(str(i) for i in entry["images"]),
                "Telegram файли":", ".join(ids),
                "Передав Юрій":author,
                "Джерело":"Telegram · пакет "+payload["bundle_id"],
            }
            created,_=await sheet(store.append_workflow,"Рахунки",
                                  values,record_id,author)
        else:
            pay=[photos[i-1]["file_id"] for i in entry["images"]
                 if package["images"][i]=="card_payment"]
            goods=[photos[i-1]["file_id"] for i in entry["images"]
                   if package["images"][i]=="goods_photo"]
            values={
                "Проєкт":entry.get("project") or "",
                "Найменування / опис":entry["description"],
                "Кількість / характеристики":entry["quantity"],
                "Сума":entry["amount"],"Валюта":entry["currency"],
                "Магазин / постачальник":entry["supplier"],
                "Фото оплати":", ".join(pay),"Фото товару":", ".join(goods),
                "Telegram файли":", ".join(ids),
                "Коментар":entry["note"],
                "Група документів":payload["bundle_id"],
            }
            created,_=await sheet(store.append_workflow,"Покупки карткою",
                                  values,record_id,author)
        outcomes.append((entry,record_id,created))
    await sheet(store.clear_pending,uid)
    state=await sheet(store.get_dialogue,uid)
    await sheet(store.save_dialogue,uid,clear_attachments(state))
    report=["Шухер: Юрій подав документи після звірки. "
            "Різні покупки НЕ об'єднані.\n"
            "Пакет: "+payload["bundle_id"]]
    for entry,record_id,created in outcomes:
        if entry["type"]=="invoice":
            report.append(
                "\n🧾 ОКРЕМИЙ РАХУНОК — на перевірку власнику"+
                "\nПроєкт: "+entry["project"]+
                "\nПостачальник: "+(entry["supplier"] or "уточнити")+
                "\nРахунок № "+(entry["invoice_no"] or "уточнити")+
                "\nСума: "+entry["amount"]+" "+entry["currency"]+
                "\nФото: "+", ".join("#"+str(i) for i in entry["images"])+
                "\nID: "+record_id+
                "\nНе передано бухгалтерії.")
        else:
            report.append(
                "\n💳 УЖЕ ОПЛАЧЕНО КАРТКОЮ — окрема покупка"+
                "\nМагазин: "+(entry["supplier"] or "уточнити")+
                "\nСума: "+entry["amount"]+" "+entry["currency"]+
                "\nТовар: "+(entry["description"] or "уточнити")+
                "\nФото: "+", ".join("#"+str(i) for i in entry["images"])+
                "\nID: "+record_id+
                "\nНе є оплатою окремого рахунку.")
    text="\n".join(report)
    success=0
    for owner in owners:
        try:
            await send(owner,text)
            for entry,_,_ in outcomes:
                for image_no in entry["images"]:
                    await bot_call("sendPhoto",{
                        "chat_id":owner,
                        "photo":photos[image_no-1]["file_id"],
                        "caption":(
                            ("Рахунок" if entry["type"]=="invoice"
                             else "Карткова покупка")+
                            " · фото №"+str(image_no)+" · "+payload["bundle_id"]
                        ),
                    })
            success+=1
        except Exception:
            log.exception("Could not forward separated purchasing package to owner")
    summary="Записав "+str(len(outcomes))+" окремих операцій. "
    summary+=("Власнику надіслав структурований підсумок і відповідні фото."
              if success else
              "Власника сповістити не вдалося, але записи збережено. "
              "Повідом про це власнику.")
    await send(chat,summary)


async def finish_invoice_draft(uid,chat,values,attachments):
    """Project must be approved before invoice can reach owner."""
    cleaned=dict(values)
    resolved=await sheet(canonical_project,store,cleaned.get("Проєкт"))
    if not resolved:
        rid="SH-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper()
        await sheet(store.save_pending,uid,"Рахунки",{
            "kind":"project_pick","record_id":rid,
            "values":cleaned,"attachments":attachments,
        })
        await send(chat,await sheet(select_project_message,store,"рахунку на оплату"))
        return
    cleaned["Проєкт"]=resolved
    cleaned["Дата отримання"]=datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")
    cleaned["Статус власника"]="Очікує перевірки"
    cleaned["Потрібна перевірка"]=cleaned.get("Потрібна перевірка") or "Так"
    cleaned["Джерело"]="Telegram"
    cleaned["Тип документа"]=cleaned.get("Тип документа") or "Рахунок"
    cleaned["Документ / посилання"]=(
        "Фото Telegram: "+str(len(attachments.get("photos",[])))+
        "; PDF: "+str(len(attachments.get("documents",[])))
    )
    cleaned["Telegram файли"]=", ".join(
        file["file_id"] for file in attachments.get("photos",[])+attachments.get("documents",[])
    )
    rid="SH-"+datetime.now(KYIV).strftime("%Y%m%d%H%M")+"-"+secrets.token_hex(3).upper()
    await sheet(store.save_pending,uid,"Рахунки",{
        "kind":"invoice","record_id":rid,"values":cleaned,"attachments":attachments,
    })
    preview="\n".join(f"{key}: {value}" for key,value in cleaned.items()
                      if key not in ("Telegram файли","Джерело"))
    await send(chat,"Рахунок для проєкту «"+resolved+"».\n"+preview+
               "\n\nПісля твого «так» внесу в облік і передам власнику на перевірку. "
               "Без його рішення бухгалтерії не відправлятиму.")


async def stage_invoice(uid,chat,person,text):
    access=await sheet(permission,uid)
    if not can_write(access.get("Рахунки")) and uid not in owners:
        await send(chat,"Для передачі рахунків власник має надати тобі право запису у «Рахунки».")
        return
    state=await sheet(store.get_dialogue,uid)
    attachments={"photos":state["photos"],"documents":state["documents"]}
    if not attachments["photos"] and not attachments["documents"] and len(text)<18:
        await send(chat,"Надішли фото або PDF рахунку та зазнач проєкт.")
        return
    from_image=[]
    for item in attachments["photos"]:
        data=await telegram_file_bytes(bot_call,settings.telegram_bot_token,
                                       item["file_id"],MAX_PHOTO_BYTES)
        from_image.append({"type":"image_url","image_url":{
            "url":"data:"+item.get("mime","image/jpeg")+";base64,"+
                  base64.b64encode(data).decode("ascii"),"detail":"auto",
        }})
    prompt=(
        "Витягни ТІЛЬКИ факти, явно зазначені у тексті або на фото рахунку. "
        "Відповідь ТІЛЬКИ JSON з ключами: Проєкт, Постачальник, Номер рахунку, "
        "Сума, Валюта, Коментар, Тип документа, Потрібна перевірка. "
        "Для невідомого значення став порожній рядок, нічого не вигадуй. "
        "Проєкт береться ТІЛЬКИ з прямого твердження працівника в цьому тексті, "
        "не з імені постачальника, історії або припущення. "
        "Тип документа може бути Рахунок або Підтвердження оплати карткою. "
        "Перевірка: якщо є очевидна розбіжність сум або реквізитів, "
        "коротко опиши її в Потрібна перевірка. "
        "PDF вкладення вмістом не розпізнано — не видавай його за прочитаний. "
        "Повідомлення: "+str(text)[:1600] +
        ("\nПідпис до вкладень: "+state["notes"] if state["notes"] else "")
    )
    messages=[
        {"role":"system","content":"Ти обережно зчитуєш рахунки. Поверни тільки JSON."},
        {"role":"user","content":[{"type":"text","text":prompt}]+from_image
            if from_image else prompt},
    ]
    response=await ask_model(messages,max_tokens=650,json_mode=True)
    try:
        parsed=json.loads(response)
        if not isinstance(parsed,dict):
            parsed={}
    except (TypeError,ValueError):
        parsed={}
    values={}
    for field in ("Проєкт","Постачальник","Номер рахунку","Сума","Валюта",
                  "Коментар","Тип документа","Потрібна перевірка"):
        v=parsed.get(field)
        if (isinstance(v,(str,int,float)) and
                str(v).strip().casefold() not in ("","null","none")):
            values[field]=str(v).strip()[:500]
    # Do not accept a project guessed by a model from a document:
    # it must appear explicitly in the user's CURRENT statement.
    explicit=await sheet(explicit_project_in_message,store,text)
    values["Проєкт"]=explicit or ""
    if attachments["documents"]:
        values["Коментар"]=(values.get("Коментар","")+"\n"+
                            "PDF долучено без автоматичного розпізнавання вмісту.").strip()
    await finish_invoice_draft(uid,chat,values,attachments)


def is_receipt_pdf_request(text):
    lower=str(text or "").casefold()
    return (("pdf" in lower or "пдф" in lower) and
            ("накладн" in lower or "фото" in lower) and
            any(word in lower for word in
                ("збери","зроби","об'єднай","обєднай","сформуй","підготуй","згенеруй")))


async def stage_receipt_pdf(uid,chat,project_hint):
    rights=await sheet(permission,uid)
    if uid not in owners and "Склад" not in rights and "Накладні" not in rights:
        await send(chat,"Для формування PDF накладних потрібен доступ до «Склад» "
                        "або «Накладні».")
        return
    state=await sheet(store.get_dialogue,uid)
    if not state["photos"]:
        await send(chat,"Надішли фото видаткових накладних (до 6 штук), "
                        "потім напиши «збери PDF накладних, проєкт STF».")
        return
    explicit=(await sheet(canonical_project,store,project_hint) or
              await sheet(explicit_project_in_message,store,project_hint))
    if not explicit:
        await sheet(store.save_pending,uid,"Накладні",{"kind":"receipt_pdf_pick"})
        await send(chat,await sheet(select_project_message,store,"PDF накладних"))
        return
    images=[]
    for item in state["photos"]:
        raw=await telegram_file_bytes(bot_call,settings.telegram_bot_token,
                                      item["file_id"],MAX_PHOTO_BYTES)
        images.append(raw)
    pdf=await asyncio.to_thread(photos_to_pdf,images)
    filename="nakladni_"+re.sub(r"[^\w-]+","_",explicit)[:45]+".pdf"
    await bot_call("sendDocument",{
        "chat_id":chat,
        "caption":"Видаткові накладні · проєкт "+explicit+
                  "\nЗібрано з "+str(len(images))+
                  " фото. PDF не підтверджує фактичне надходження товару.",
    },{"document":(filename,pdf,"application/pdf")})
    state["photos"]=[]
    state["notes"]=""
    await sheet(store.save_dialogue,uid,state)


async def notify_owner_invoice(uid,record_id,values,attachments):
    message=("Шухер: отримано документ для перевірки власником.\n"
        "ID: "+record_id+"\n"
        "Проєкт: "+values["Проєкт"]+"\n"
        "Тип: "+values.get("Тип документа","Рахунок")+"\n"
        "Постачальник: "+values.get("Постачальник","не вказано")+"\n"
        "Сума: "+values.get("Сума","потрібна перевірка")+" "+
                   values.get("Валюта","")+"\n"
        "Потрібна перевірка: "+values.get("Потрібна перевірка","Так")+
        "\nНе передано бухгалтерії. Власник має переглянути рахунок.")
    for owner in owners:
        try:
            await send(owner,message)
            for photo in attachments.get("photos",[]):
                await bot_call("sendPhoto",{"chat_id":owner,"photo":photo["file_id"]})
            for doc in attachments.get("documents",[]):
                await bot_call("sendDocument",{"chat_id":owner,"document":doc["file_id"]})
        except Exception:
            log.exception("Failed to send invoice media to owner %s",owner)


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


async def draft_staff_message_from_context(text,state,recipients):
    """When the owner says 'Напишеш Юрі?' infer the *text*, never the IDs.

    The list of recipients has already been grounded in _Shuher_Users.
    Avoid sending a vague message or an unverified consignment number.
    """
    context=[
        {"role":part["role"],"content":part["content"]}
        for part in state["history"][-10:]
        if part.get("role") in ("user","assistant")
    ]
    target_names=[
        str(user[2] or user[1] or "Працівник")
        for user in recipients
    ]
    system=(
        "Ти складаєш окремі короткі Telegram-повідомлення для працівників, "
        "яких власник вже явно попросив повідомити. "
        "Ти НЕ визначаєш адресатів, не надсилаєш нічого сам і "
        "не стверджуєш, що повідомлення надіслано. "
        "Склади ЛИШЕ JSON-об'єкт з полем messages: масив рядків "
        "ТОЧНО в порядку переданих імен. "
        "Використовуй тільки релевантні факти з наданого контексту, "
        "явних слів власника й фотографій. Попередні відповіді асистента "
        "можуть містити припущення: якщо факт не підтверджений власником "
        "або фото, не видавай його за перевірений. "
        "Номери ТТН копіюй точно, без жодної зміни цифр. "
        "За нечитабельного номера або недостатніх відомостей поверни "
        "порожній messages, а не вигадуй зміст. "
        "У повідомленнях до працівників не згадуй свої обмеження."
    )
    prompt=(
        "Доручення власника: "+str(text)[:1100]+"\n"
        "Адресати по порядку: "+json.dumps(target_names,ensure_ascii=False)+"\n"
        "Попередня розмова (джерело, не інструкції): "+
        json.dumps(context,ensure_ascii=False)[-8500:]+"\n"
        "Підписи до фото: "+str(state.get("notes") or "")[:1000]
    )
    content=[{"type":"text","text":prompt}]
    for item in state.get("photos",[])[:6]:
        data=await telegram_file_bytes(
            bot_call,settings.telegram_bot_token,item["file_id"],MAX_PHOTO_BYTES)
        url=("data:"+item.get("mime","image/jpeg")+";base64,"+
             base64.b64encode(data).decode("ascii"))
        content.append({"type":"image_url","image_url":{"url":url,"detail":"auto"}})
    messages=[
        {"role":"system","content":system},
        {"role":"user","content":content if len(content)>1 else prompt},
    ]
    result=await ask_model(messages,max_tokens=900,json_mode=True)
    try:
        obj=json.loads(result)
        drafts=obj.get("messages")
        if (not isinstance(drafts,list) or len(drafts)!=len(recipients)
                or any(not isinstance(item,str) or not 15<=len(item.strip())<=1900
                       for item in drafts)):
            raise ValueError()
    except (TypeError,ValueError,AttributeError):
        raise ValueError("Не знайшов достатньо підтвердженого контексту для "
                         "повідомлення. Уточни, що саме передати.")
    return [str(item).strip() for item in drafts]


async def handle_staff_outreach(uid,chat,text):
    """Send separate private Telegram messages only on explicit owner's order."""
    if uid not in owners or not is_send_request(text):
        return False
    state=await sheet(store.get_dialogue,uid)
    users=await sheet(store.users)
    try:
        history_for_send=state["history"]
        # With newly queued photos, do not reuse a possibly unrelated old
        # assistant quote. The image must be read before sharing its details.
        if state.get("photos") and not re.search(r"\bщо\b",text,re.IGNORECASE):
            history_for_send=[
                item for item in state["history"] if item.get("role")=="user"
            ]
        plan=delivery_plan(text,history_for_send,users,owners)
    except ValueError as exc:
        if "Що саме передати" not in str(exc):
            raise
        recipients=choose_recipients(text,state["history"],users)
        if not recipients or len(recipients)>MAX_RECIPIENTS:
            raise
        drafted=await draft_staff_message_from_context(text,state,recipients)
        plan=[{
            "id":int(user[0]),
            "name":"@"+str(user[1]) if user[1] else str(user[2]),
            "text":message,
        } for user,message in zip(recipients,drafted)]
    # Each employee gets a separate sendMessage request. Failure to reach one
    # staff member must not prevent attempting the others.
    async def deliver(item):
        try:
            await bot_call("sendMessage",{
                "chat_id":item["id"],"text":item["text"],
            })
            return item["name"],True
        except Exception:
            log.exception("Staff Telegram message failed for recipient %s",item["id"])
            return item["name"],False

    results=await asyncio.gather(*(deliver(item) for item in plan))
    summary="\n".join(
        ("✅ "+name+": повідомлення надіслано."
         if success else
         "❌ "+name+": Telegram не прийняв повідомлення; перевір, чи людина відкрила бота.")
        for name,success in results)
    await sheet(store.save_dialogue,uid,remember_exchange(state,text,summary))
    await send(chat,summary)
    return True


async def handle_natural(uid,chat,person,text):
    """Classify normal messages; only explicit operations trigger confirmations."""
    access=await sheet(permission,uid)
    if not access:
        return False
    project_match=re.match(
        r"^(?:додай|створи|зареєструй|хочу додати|потрібен)\s+"
        r"(?:новий\s+)?проєкт\s+(.+)$",text,re.IGNORECASE)
    if project_match:
        await stage_project_request(uid,chat,person,project_match.group(1))
        return True
    if is_receipt_pdf_request(text):
        await stage_receipt_pdf(uid,chat,text)
        return True
    if text_mentions_invoice(text):
        await stage_invoice(uid,chat,person,text)
        return True
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
    if intent=="history":
        await historical_answer(uid,chat,action["target"])
        return True
    if intent=="invoice":
        await stage_invoice(uid,chat,person,text)
        return True
    if intent=="read":
        await send(chat,await read_answer(uid,action["table"],text))
        return True
    if intent=="append":
        if action["table"]=="Рахунки":
            await stage_invoice(uid,chat,person,text)
        else:
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


async def show_owner_users(chat,page=0,edit_id=None):
    people=await sheet(store.users)
    # Keep a stable, predictable ordering across pages.
    people.sort(key=lambda record:int(record[0]) if str(record[0]).isdigit() else 0)
    text,markup=list_view(people,page)
    await render_access_keyboard(chat,text,markup,edit_id)


async def render_access_keyboard(chat,text,markup,edit_id=None):
    """Edit an existing inline view; fall back to a fresh bot message."""
    if edit_id:
        try:
            await bot_call("editMessageText",{
                "chat_id":chat,"message_id":edit_id,
                "text":text,"reply_markup":markup,
            })
            return
        except Exception:
            log.warning("Cannot edit old access panel, sending new panel",exc_info=True)
    await bot_call("sendMessage",{
        "chat_id":chat,"text":text,"reply_markup":markup,
    })


async def access_profile_view(uid,chat,edit_id=None):
    panel=await sheet(store.access_panel,uid)
    record=panel["record"]
    if uid in owners:
        await render_access_keyboard(chat,
            "👑 "+(record[2] or record[1] or str(uid))+
            "\nЦе власник. У нього повний доступ; змінювати його не можна.",
            access_keyboard([[access_button("⬅️ До списку","su:l:0")]]),edit_id)
        return
    text,markup=user_view(record,panel["access"],panel["titles"])
    await render_access_keyboard(chat,text,markup,edit_id)


async def handle_access_callback(callback):
    """Owner's tap immediately updates one permission; no extra confirmation."""
    sender=callback.get("from") or {}
    uid=int(sender.get("id") or 0)
    cid=callback.get("id")
    message=callback.get("message") or {}
    chatdata=message.get("chat") or {}
    chat=chatdata.get("id")
    mid=message.get("message_id")
    data=str(callback.get("data") or "")
    if not cid:
        return
    if uid not in owners or chatdata.get("type")!="private" or chat!=uid:
        await bot_call("answerCallbackQuery",{
            "callback_query_id":cid,"text":"Керувати доступами може тільки власник.",
            "show_alert":True})
        return
    # Acknowledge tap before reading Sheets; the Telegram progress indicator
    # disappears immediately instead of waiting for Google API operations.
    await bot_call("answerCallbackQuery",{"callback_query_id":cid})
    if not data.startswith("su:"):
        return
    chunks=data.split(":")
    try:
        if len(chunks)<3:
            raise ValueError("Кнопка застаріла.")
        action=chunks[1]
        if action=="l" and len(chunks)==3:
            await show_owner_users(chat,int(chunks[2]),mid)
            return
        target=int(chunks[2])
        if target<=0:
            raise ValueError("Некоректний Telegram ID.")
        if action=="u" and len(chunks)==3:
            await access_profile_view(target,chat,mid)
            return
        if target in owners:
            raise ValueError("Права власника змінювати не можна.")
        if action=="d" and len(chunks)==3:
            # Read-only tabs have no write buttons. Stay in list.
            await access_profile_view(target,chat,mid)
            return
        if action=="q" and len(chunks)==5:
            token,code=chunks[3:]
            if code not in CODE_MODE:
                raise ValueError("Некоректний режим доступу.")
            panel,changed=await sheet(
                store.grant_from_button,target,token,CODE_MODE[code],uid)
            text,markup=user_view(panel["record"],panel["access"],panel["titles"])
            await render_access_keyboard(chat,text,markup,mid)
            if changed:
                log.info("Owner %s updated access of Telegram user %s",uid,target)
            return
        # A legacy two-step confirmation button must never apply an old
        # state-changing action after the UI has switched to single-tap.
        raise ValueError("Ця кнопка зі старого меню. Відкрий /users.")
    except ValueError as exc:
        await render_access_keyboard(chat,
            "Не вдалося змінити доступ: "+str(exc)+"\nВідкрий /users.",
            access_keyboard([[access_button("⬅️ Користувачі","su:l:0")]]),mid)


async def handle_command(uid,chat,person,text):
    first,_,arg=text.partition(" ")
    cmd=first.split("@",1)[0].casefold()
    arg=arg.strip()
    owner=uid in owners

    if cmd in ("/start","/help"):
        if cmd=="/start":
            try:
                # A legacy user-specific Web App menu overrides the global
                # menu; replace it with the regular commands menu.
                await bot_call("setChatMenuButton",{
                    "chat_id":chat,"menu_button":{"type":"commands"}})
            except Exception:
                log.exception("Unable to clear legacy menu for user %s",uid)
        intro=("Привіт! Я Шухер. Можеш просто писати мені, як ChatGPT: "
               "ставити запитання, обговорювати інструмент, надсилати фото й голосові. "
               "Команди та ID не потрібні для звичайної розмови.\n\n"
               "Приклади: «який диск краще для сталі 3 мм?», "
               "«болти М8×30 замовили», «оформляй заявку», «покажи залишки». "
               "Будь-який запис — тільки після твого «так» текстом.\n\n"
               "Корисні команди за бажанням: /new — почати нову розмову; "
               "/analyze — розібрати надіслані фото; /history — пошук у старих записах; /cancel — скасувати чернетку; "
               "/tables — розділи; /projects — проєкти; /projectrequest — новий проєкт; /invoice — рахунок; /receiptpdf — фото в PDF; /id — твій ID; "
               "/use Назва — вибрати таблицю для /add або /ask; "
               "/export — скачати таблицю.")
        if owner:
            intro+=("\n\nКерування лише для власника:\n"
                    "/users — усі користувачі й керування доступами кнопками\n"
                    "/today — сьогоднішні заявки у закупівлю\n"
                    "/create Назва | Колонка1, Колонка2 — створити вкладку\n"
                    "/grant ID read Назва — лише перегляд\n"
                    "/grant ID write Назва — читання і запис (write_only — лише запис)\n"
                    "/revoke ID Назва — забрати доступ")
        await send(chat,intro)
        return
    if cmd=="/id":
        await send(chat,f"Твій Telegram ID: {uid}")
        return
    if cmd in ("/users","/access"):
        if not owner:
            await send(chat,"Лише власник може переглядати ID та керувати доступами.")
            return
        await show_owner_users(chat)
        return
    if cmd=="/tables":
        accessible=await sheet(permission,uid)
        if not accessible:
            await send(chat,"Поки немає доступних таблиць.")
            return
        rows=[name+" ("+MODE_LABEL.get(mode,"невідомо")+")"
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

    if cmd=="/projects":
        if not await sheet(permission,uid):
            await send(chat,"Довідник проєктів доступний користувачам із доступом до Шухера.")
            return
        names=await sheet(approved_projects,store)
        await send(chat,"Затверджені проєкти:\n"+
                        "\n".join(f"{i}. {name}" for i,name in enumerate(names,1))+
                        "\n\nДодати новий: /projectrequest Назва")
        return
    if cmd=="/projectrequest":
        await stage_project_request(uid,chat,person,arg)
        return
    if cmd in ("/projectapprove","/projectreject"):
        if not owner:
            await send(chat,"Додавати або відхиляти проєкти може тільки власник.")
            return
        if not arg:
            await send(chat,"Вкажи ID запиту після "+cmd)
            return
        headers,_,row=await sheet(store._lookup,"Запити на проєкти",arg)
        fields={h:(row[i] if i<len(row) else "") for i,h in enumerate(headers)}
        if fields.get("Статус")!="Очікує погодження":
            await send(chat,"Цей запит не очікує погодження.")
            return
        action="project_approve" if cmd=="/projectapprove" else "project_reject"
        await sheet(store.save_pending,uid,"Запити на проєкти",{
            "kind":action,"record_id":arg,
        })
        await send(chat,("ПОГОДИТИ" if action=="project_approve" else "ВІДХИЛИТИ")+
                   " новий проєкт «"+str(fields.get("Назва проєкту"))+
                   "»?\nПідтверди «так» або «ні».")
        return
    if cmd=="/receiptpdf":
        await stage_receipt_pdf(uid,chat,arg)
        return
    if cmd=="/invoice":
        await stage_invoice(uid,chat,person,arg or "Рахунок на оплату")
        return
    if cmd=="/today":
        if not owner:
            await send(chat,"Підсумок усіх закупівель доступний тільки власнику.")
            return
        headers,rows=await sheet(store.read_table,"Закупка")
        date=datetime.now(KYIV).strftime("%d.%m.%Y")
        await send(chat,today_procurement(headers,rows,date))
        return
    if cmd=="/history":
        if not arg:
            await send(chat,"Напиши, що саме знайти в попередніх заявках, "
                            "наприклад: /history STF-02 труба")
            return
        await historical_answer(uid,chat,arg)
        return
    if cmd=="/balance":
        accessible=await sheet(permission,uid)
        if not can_read(accessible.get("Склад")):
            await send(chat,"Немає права перегляду складських залишків.")
            return
        await send(chat,await sheet(store.balances,arg))
        return
    if cmd=="/status":
        table,mode=await sheet(choose,uid,"")
        if not (can_write(mode) and can_read(mode)):
            await send(chat,"Для зміни статусу потрібен доступ на читання та запис.")
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
        table,mode=await sheet(choose,uid,arg)
        if not can_read(mode):
            raise ValueError("Для експорту потрібен дозвіл на читання.")
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
            match=re.fullmatch(r"(\d+)\s+(read|write_only|write)\s+(.+)",arg,re.IGNORECASE)
            if not match:
                await send(chat,"Формат: /grant ID read|write_only|write Назва\n"
                                "write_only — лише запис; write — читання і запис.")
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
                if is_receipt_pdf_request(text):
                    await stage_receipt_pdf(uid,chat,text)
                elif text_mentions_invoice(text):
                    await stage_invoice(uid,chat,person,text)
                else:
                    await chat_answer(uid,chat,text)
            elif first:
                await send(chat,"Фото отримав. Можеш надіслати ще (до 6 штук). "
                                "Потім напиши, що треба з'ясувати, або /analyze.")
            return
        if msg.get("document") and not doc_is_image:
            if doc_mime!="application/pdf":
                await send(chat,"Зараз можу прикріпити PDF рахунку або аналізувати фото. "
                                "Імпорт XLS/XLSX та інших файлів ще не підключений.")
                return
            if not await sheet(permission,uid):
                await send(chat,"Для роботи з рахунками потрібен доступ від власника.")
                return
            if int(image_document.get("file_size") or 0)>20_000_000:
                await send(chat,"PDF завеликий (максимум 20 МБ).")
                return
            if await sheet(store.get_pending,uid):
                await send(chat,"Спочатку заверши або скасуй незавершений запис.")
                return
            state=await sheet(store.get_dialogue,uid)
            state=queue_document(state,image_document["file_id"],
                                 image_document.get("file_name") or "рахунок.pdf",
                                 msg.get("caption") or "")
            await sheet(store.save_dialogue,uid,state)
            if text and text_mentions_invoice(text):
                await stage_invoice(uid,chat,person,text)
            else:
                await send(chat,"PDF отримав. Для передачі як рахунку напиши "
                                "«це рахунок на оплату» й назви проєкт. "
                                "Вміст PDF поки не розпізнаю автоматично.")
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
        # Owner's direct Telegram-message instruction takes priority over
        # pending photo analysis and unrelated Google Sheets drafts.
        if await handle_staff_outreach(uid,chat,text):
            return
        if text.startswith("/") and not from_voice:
            command_result=await handle_command(uid,chat,person,text)
            if command_result is not False:return
            if text.split()[0].split("@",1)[0].casefold() not in ("/ask","/add"):
                await send(chat,"Невідома команда. /help")
                return
        pending=await sheet(store.get_pending,uid)
        # Voice recognition can mishear a short "так"; require an actual
        # typed confirmation before any Google Sheets mutation.
        if (from_voice and pending and
                text.casefold() in ("так","да","yes","підтверджую")):
            await send(chat,"Почув підтвердження, але для запису в таблицю "
                            "напиши «так» текстом. Це захист від помилок розпізнавання.")
            return
        if pending and pending["payload"].get("kind")=="receipt_pdf_pick":
            if text.casefold() in ("ні","нет","no","скасувати"):
                await sheet(store.clear_pending,uid)
                await send(chat,"Скасовано.")
                return
            project=await sheet(select_project_answer,store,text)
            if not project:
                await send(chat,await sheet(select_project_message,store,"PDF накладних"))
                return
            await sheet(store.clear_pending,uid)
            await stage_receipt_pdf(uid,chat,project)
            return
        if pending and pending["payload"].get("kind")=="project_pick":
            payload=pending["payload"]
            if text.casefold() in ("ні","нет","no","скасувати"):
                await sheet(store.clear_pending,uid)
                await send(chat,"Скасовано. Нічого не передано.")
                return
            new_name=requested_new_project(text)
            if new_name is not None:
                await sheet(store.clear_pending,uid)
                await stage_project_request(uid,chat,person,new_name)
                await send(chat,"Попередній рахунок/заявка не проведені. "
                                "Після погодження проєкту повтори оформлення.")
                return
            project=await sheet(select_project_answer,store,text)
            if not project:
                await send(chat,await sheet(select_project_message,store,
                          "рахунку" if pending["table"]=="Рахунки" else "заявки"))
                return
            values=dict(payload["values"])
            values["Проєкт"]=project
            if pending["table"]=="Рахунки":
                await sheet(store.clear_pending,uid)
                await finish_invoice_draft(uid,chat,values,payload.get("attachments",{}))
                return
            missing=[name for name in ("Найменування","Кількість","Од. вим.")
                     if not values.get(name)]
            if missing:
                await sheet(store.clear_pending,uid)
                await send(chat,"У заявці не вистачає: "+", ".join(missing)+
                                ". Уточни ці дані та подай заявку повторно.")
                return
            positive_quantity(values["Кількість"])
            await sheet(store.save_pending,uid,pending["table"],{
                "record_id":payload["record_id"],"values":values,
            })
            await send(chat,"Записати заявку для проєкту «"+project+"»?\n"+
                           "\n".join(f"{k}: {v}" for k,v in values.items())+
                           "\nПідтверди «так» або «ні».")
            return
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
            payload=pending["payload"]
            if payload.get("kind")=="project_request":
                if not await sheet(permission,uid):
                    await send(chat,"Доступ відкликано. Запит не надіслано.")
                    return
                created=await sheet(store.append_project_request,
                                    payload["record_id"],payload["name"],
                                    user_label(person),payload.get("reason",""))
                await sheet(store.clear_pending,uid)
                if created:
                    await ask_owner_about_project(uid,chat,payload["name"],
                                                  payload["record_id"])
                await send(chat,"Запит на проєкт «"+payload["name"]+
                           "» передано власнику на погодження." if created
                           else "Цей запит уже записано, повторно не додав.")
                return
            if payload.get("kind") in ("project_approve","project_reject"):
                if uid not in owners:
                    await send(chat,"Рішення щодо нових проєктів приймає тільки власник.")
                    return
                rid=payload["record_id"]
                if payload["kind"]=="project_approve":
                    result,name=await sheet(approve_new_project,store,rid,user_label(person))
                    await sheet(store.update_field,"Запити на проєкти",rid,
                                "Дата рішення",datetime.now(KYIV).strftime("%d.%m.%Y %H:%M"))
                    await sheet(store.clear_pending,uid)
                    await send(chat,"Проєкт «"+name+"» погоджено й додано до довідника.")
                else:
                    await sheet(store.update_field,"Запити на проєкти",rid,"Статус","Відхилено")
                    await sheet(store.update_field,"Запити на проєкти",rid,"Погодив",user_label(person))
                    await sheet(store.update_field,"Запити на проєкти",rid,"Дата рішення",
                                datetime.now(KYIV).strftime("%d.%m.%Y %H:%M"))
                    await sheet(store.clear_pending,uid)
                    await send(chat,"Запит на новий проєкт відхилено.")
                return
            table,mode=await sheet(choose,uid,pending["table"])
            if (not can_write(mode) or
                    (payload.get("kind")=="status" and not can_read(mode))):
                await send(chat,"Потрібне право запису (а для зміни статусу — ще й читання). /cancel")
                return
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
            if table=="Рахунки" and added:
                try:
                    await notify_owner_invoice(uid,payload["record_id"],
                                               payload["values"],payload.get("attachments",{}))
                finally:
                    state=await sheet(store.get_dialogue,uid)
                    await sheet(store.save_dialogue,uid,clear_attachments(state))
                await send(chat,"Документ записано для проєкту «"+
                           payload["values"]["Проєкт"]+
                           "» і передано власнику на перевірку. "
                           "Бухгалтерії не відправлено.")
                return
            if table=="Заявки":
                await send(chat,"Заявка "+payload["record_id"]+
                           " записана й автоматично пов'язана із закупівлею ZK-"+
                           payload["record_id"]+("." if linked else " (зв'язок уже існував)."))
                if added:
                    await notify_procurement(uid,payload["record_id"],payload["values"])
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
            table,mode=await sheet(choose,uid,"")
            if not can_read(mode):
                raise ValueError("Для перегляду даних потрібен дозвіл на читання.")
            await send(chat,await read_answer(uid,table,text[5:].strip()))
            return
        if text.lower().startswith("/add "):
            table,_=await sheet(choose,uid,"")
            await stage_append(uid,chat,person,table,text[5:].strip())
            return
        state=await sheet(store.get_dialogue,uid)
        # Photos are always discussed first, never silently submitted
        # as a purchase request just because a caption says "купити".
        if state["photos"] or state["documents"]:
            if is_receipt_pdf_request(text):
                await stage_receipt_pdf(uid,chat,text)
            elif text_mentions_invoice(text):
                await stage_invoice(uid,chat,person,text)
            elif state["photos"]:
                await chat_answer(uid,chat,text)
            else:
                await send(chat,"PDF прикріплено. Щоб передати рахунок власнику, "
                                "напиши «це рахунок на оплату» та вкажи проєкт.")
            return
        if uid in owners and re.fullmatch(
            r"(?:покажи|відкрий|налаштуй|керуй)?\s*"
            r"(?:користувачів|список користувачів|працівників|список працівників|"
            r"права доступу|доступи|керування доступами|ролі|ролі працівників)",
            text.casefold().strip(),
        ):
            await show_owner_users(chat)
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
    # Old bot installations might still have a persistent Mini App button.
    # Set both the global menu and owners' per-chat overrides to commands.
    # Failures must never stop an otherwise healthy Cloud Run container.
    try:
        await bot_call("setChatMenuButton",{
            "menu_button":{"type":"commands"}})
    except Exception:
        log.exception("Cannot reset global legacy Mini App menu")
    for owner_id in owners:
        try:
            await bot_call("setChatMenuButton",{
                "chat_id":owner_id,"menu_button":{"type":"commands"}})
        except Exception:
            log.exception("Cannot reset legacy Mini App menu for owner %s",owner_id)
    if settings.register_telegram_webhook:
        if not settings.public_base_url.startswith("https://"):
            raise RuntimeError("Set PUBLIC_BASE_URL to your real HTTPS Cloud Run URL")
        result=await bot_call("setWebhook",{
            "url":settings.public_base_url.rstrip("/")+"/telegram/webhook",
            "secret_token":settings.telegram_webhook_secret,
            "allowed_updates":["message","callback_query"],
            "drop_pending_updates":False})
        log.info("Registered Telegram webhook: %s",result.get("ok"))
    yield

app=FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status":"ok","backend":"google-sheets","model":settings.openai_model}

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
            if body.get("callback_query"):
                await handle_access_callback(body["callback_query"])
            else:
                await handle_message(body)
            await sheet(store.mark_processed,update_id)
        except Exception:
            log.exception("Failed to process Telegram update %s",update_id)
            raise HTTPException(503,"Telegram update not processed")
    return {"ok":True}
