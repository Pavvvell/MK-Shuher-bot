"""Telegram transport and business services for the model-first Shuher agent."""
import asyncio
import json
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from openai import AsyncOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict
from store import SheetsStore, request_reads
from agent import run_agent
from api_errors import quota_notice
from agent_tools import AgentTools
from invoice_intake import InvoiceIntake
from workflow_bridge import WorkflowBridge, install_routes
from payable_invoices import invoice_caption
from purchase_bundle import validate_persistence
from dialogue import queue_photo, queue_document, clear_attachments
from access_ui import (button as access_button, keyboard as access_keyboard,
                       list_view, user_view, CODE_MODE, can_read, can_write)
from media import MAX_PHOTO_BYTES, transcribe_telegram_voice
from task_notifications import plain_telegram_text, task_fields, authorized_recipient, task_marker, task_notice
from concurrency import KeyedQueues, SheetsWorker, user_queue_key
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log=logging.getLogger("shuher-lite")
KYIV=ZoneInfo("Europe/Kyiv")

class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_file=".env",extra="ignore")
    telegram_bot_token:str
    telegram_webhook_secret:str
    openai_api_key:str
    openai_model:str="gpt-6.1-sol"
    spreadsheet_id:str
    history_spreadsheet_id:str=""
    owner_ids:str
    google_service_account_json_b64:str=""
    register_telegram_webhook:bool=False
    public_base_url:str=""
    codex_bridge_enabled:bool=False
    codex_master_spreadsheet_id:str="1P3v-A5Obzfwbw1CpunZEztKJ_tYORZOr5KSrQXvWias"
    codex_owner_id:int=0
    codex_bridge_secret:str=""
    codex_test_delivery:bool=False
    codex_live_delivery:bool=False


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
queues=KeyedQueues()
sheet_worker=SheetsWorker()
pdf_worker=SheetsWorker("shuher-pdf")
workflow_bridge=None

async def sheet(method,*args):
    return await sheet_worker.run(method,*args)


async def write_as(uid,table,method,*args,require_read=False):
    """Recheck permission and commit in the same serialized Sheets operation."""
    def commit():
        access=permission(uid)
        if table is None:
            allowed=bool(access)
        else:
            mode=access.get(table)
            allowed=can_write(mode) and (not require_read or can_read(mode))
        if not allowed:
            raise ValueError("Доступ відкликано або недостатньо прав. Запис не виконано.")
        return method(*args)
    return await sheet(commit)


async def bot_call(method,payload=None,files=None):
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response=await client.post(
                telegram_api+"/"+method,
                json=payload if files is None else None,
                data={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v
                      for k,v in (payload or {}).items()} if files is not None else None,
                files=files)
            response.raise_for_status()
            answer=response.json()
            if not answer.get("ok"):
                raise RuntimeError("Telegram API відхилив запит "+method)
            return answer
    except httpx.HTTPError:
        raise RuntimeError("Telegram transport request failed") from None


async def send(chat,text):
    if text is None:return  # An inline tool already rendered the complete answer.
    text=plain_telegram_text(str(text or "Немає даних."))
    for start in range(0,len(text),3900):
        await bot_call("sendMessage",{"chat_id":chat,"text":text[start:start+3900],"link_preview_options":{"is_disabled":True}})


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


def user_label(user):
    return ("@"+user["username"]) if user.get("username") else user.get("first_name","Користувач")


def permission(uid):
    return store.permissions(uid,owners)


async def sync_task_notifications():
    """Serialize notification scans without blocking unrelated user requests."""
    async with queues.hold(("task-notifications",)):
        return await _sync_task_notifications()


async def _sync_task_notifications():
    headers, rows = await sheet(store.read_table, "Доручення")
    users = await sheet(store.users)
    grants = await sheet(store.data, "_Shuher_Access")
    processed = await sheet(store.task_notification_markers)
    count = 0
    for row in rows:
        fields = task_fields(headers, row)
        record_id = fields.get("ID", "")
        if not record_id or not fields.get("Завдання"):
            continue
        target = authorized_recipient(fields.get("Виконавець"), users, grants)
        if target is None:
            continue
        recipient_id, _ = target
        marker = task_marker(record_id, fields, recipient_id)
        if marker in processed:
            continue
        previous = any(m.startswith("tasknotify:" + record_id + ":") for m in processed)
        try:
            await send(recipient_id, task_notice(fields, update=previous))
        except Exception:
            log.exception("Task notification failed for %s", record_id)
            continue
        await sheet(store.mark_processed, marker)
        processed.add(marker)
        count += 1
    return count


def append_checked_task(values,record_id,author):
    """Validate the current assignee inside the serialized write operation."""
    target=authorized_recipient(values.get("Виконавець"),store.users(),
                                store.data("_Shuher_Access"))
    if target is None:
        raise ValueError("Виконавець не має права читання «Доручень» або не визначений однозначно. "
                         "Запис не виконано. Перевір доступ чи скасуй чернетку через /cancel.")
    return store.append_workflow("Доручення",values,record_id,author)


def task_delivery_status(record_id):
    headers,_,row=store._lookup("Доручення",record_id)
    fields=task_fields(headers,row)
    target=authorized_recipient(fields.get("Виконавець"),store.users(),
                                store.data("_Shuher_Access"))
    if target is None:
        return "⚠️ Повідомлення виконавцю не доставлено: перевір його реєстрацію та право читання «Доручень»."
    if task_marker(record_id,fields,target[0]) in store.task_notification_markers():
        return "✅ Telegram прийняв повідомлення для "+fields["Виконавець"]+". Це не підтверджує прочитання."
    return "⚠️ Доручення записано, але повідомлення виконавцю не доставлено. Повторити надсилання: /tasksync."


def undelivered_task_count():
    headers,rows=store.read_table("Доручення")
    users,grants=store.users(),store.data("_Shuher_Access")
    processed=store.task_notification_markers()
    count=0
    for row in rows:
        fields=task_fields(headers,row)
        rid=fields.get("ID")
        if not rid or not fields.get("Завдання"):
            continue
        target=authorized_recipient(fields.get("Виконавець"),users,grants)
        if target is None or task_marker(rid,fields,target[0]) not in processed:
            count+=1
    return count


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
            created,_=await write_as(uid,"Рахунки",store.append_workflow,"Рахунки",
                                  values,record_id,author)
        else:
            pay=[photos[i-1]["file_id"] for i in entry["images"]
                 if package["images"].get(str(i),package["images"].get(i))=="card_payment"]
            goods=[photos[i-1]["file_id"] for i in entry["images"]
                   if package["images"].get(str(i),package["images"].get(i))=="goods_photo"]
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
            created,_=await write_as(uid,"Покупки карткою",store.append_workflow,"Покупки карткою",
                                  values,record_id,author)
        outcomes.append((entry,record_id,created))
    await sheet(store.clear_pending,uid)
    state=await sheet(store.get_dialogue,uid)
    await sheet(store.save_dialogue,uid,clear_attachments(state))
    report=["Шухер: "+author+" подав документи після звірки. "
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
                            invoice_caption({"Постачальник":entry["supplier"],
                                             "Проєкт":entry["project"]})
                            if entry["type"]=="invoice" else
                            "Карткова покупка · фото №"+str(image_no)+" · "+payload["bundle_id"]
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
    return {"ok":True,"records":[{"ID":rid,"created":created} for _,rid,created in outcomes],
            "owner_notifications_accepted":success,"summary":summary}


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
    delivered=[]
    failed=[]
    for owner in owners:
        try:
            await send(owner,message)
            for photo in attachments.get("photos",[]):
                await bot_call("sendPhoto",{"chat_id":owner,"photo":photo["file_id"],
                                            "caption":invoice_caption(values)})
            for doc in attachments.get("documents",[]):
                await bot_call("sendDocument",{"chat_id":owner,"document":doc["file_id"],
                                               "caption":invoice_caption(values)})
            delivered.append(owner)
        except Exception:
            failed.append(owner)
            log.exception("Failed to send invoice media to owner %s",owner)
    return {"telegram_accepted":delivered,"failed":failed,"read_receipt":False}


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


@asynccontextmanager
async def lifespan(app:FastAPI):
    await sheet(store.bootstrap)
    global workflow_bridge
    workflow_bridge=None
    if settings.codex_bridge_enabled:
        workflow_bridge=WorkflowBridge(sys.modules[__name__], SheetsStore(
            settings.codex_master_spreadsheet_id, settings.google_service_account_json_b64))
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
            "drop_pending_updates":False,
            "max_connections":4})
        log.info("Registered Telegram webhook: %s",result.get("ok"))
    try:
        yield
    finally:
        await asyncio.to_thread(sheet_worker.close)
        await asyncio.to_thread(pdf_worker.close)


app=FastAPI(lifespan=lifespan)
install_routes(sys.modules[__name__])

@app.get("/health")
async def health():
    return {"status":"ok","backend":"google-sheets","model":settings.openai_model,
            "queue":"per-user","parallel_requests":4,"architecture":"model-first-tools"}


@app.post("/tasks/sync")
async def task_sync(x_shuher_sync_secret: str | None = Header(default=None)):
    """Trusted scheduled sync for edits made directly in Google Sheets."""
    if not x_shuher_sync_secret or not secrets.compare_digest(
            x_shuher_sync_secret, settings.telegram_webhook_secret):
        raise HTTPException(403, "Forbidden")
    with request_reads():
        count = await sync_task_notifications()
    return {"ok": True, "notifications": count}


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
    async with queues.hold(("update",update_id)), queues.hold(user_queue_key(body)):
        with request_reads():
            try:
                if await sheet(store.processed,update_id):
                    return {"ok":True,"duplicate":True}
                if body.get("callback_query"):
                    await handle_callback(body)
                else:
                    await handle_message(body)
                await sheet(store.mark_processed,update_id)
            except Exception:
                log.exception("Failed to process Telegram update %s",update_id)
                raise HTTPException(503,"Telegram update not processed")
    return {"ok":True}



async def handle_message(update):
    """Only transport/authentication here. Text meaning belongs to the model."""
    msg=update.get('message') or {}
    person=msg.get('from') or {}
    chatdata=msg.get('chat') or {}
    if not person.get('id') or chatdata.get('type')!='private':return
    uid=int(person['id']);chat=chatdata['id']
    fresh=await sheet(store.register,person)
    if fresh and uid not in owners:await notify_owners(uid,person)
    if not await sheet(permission,uid):
        await send(chat,f'Потрібен доступ від власника. Твій Telegram ID: {uid}. Власник може налаштувати його у списку користувачів.')
        return
    if workflow_bridge is not None:
        # Durable staging before model/business work; Master is never called here.
        # Existing Sheets failure returns 503, so Telegram retries before any action.
        await workflow_bridge.capture(update)
    text=(msg.get('text') or msg.get('caption') or '').strip()
    # Explicit session-control command, never semantic routing of prose.
    if text.split('@',1)[0] in ('/new','/clear'):
        await sheet(store.clear_pending,uid)
        await sheet(store.clear_dialogue,uid)
        await send(chat,'Нова розмова. Чернетку скасовано; записи в таблицях збережені.')
        return
    state=await sheet(store.get_dialogue,uid)
    incoming_ids=[]
    try:
        doc=msg.get('document') or {}
        mime=doc.get('mime_type','')
        is_image=mime in ('image/jpeg','image/png','image/webp')
        if msg.get('photo') or is_image:
            if not await sheet(permission,uid):raise ValueError('Для роботи з фото потрібен доступ від власника.')
            photo=msg['photo'][-1] if msg.get('photo') else doc
            if int(photo.get('file_size') or 0)>MAX_PHOTO_BYTES:raise ValueError('Фото завелике (до 6 МБ).')
            state,_=queue_photo(state,photo['file_id'],msg.get('media_group_id'),text,mime if is_image else 'image/jpeg')
            entry=next(p for p in state['photos'] if p['file_id']==photo['file_id'])
            entry.update(unique_id=photo.get('file_unique_id',''),as_document=is_image)
            incoming_ids=[photo['file_id']]
            await sheet(store.save_dialogue,uid,state)
            text=text or 'Надсилаю фото. Подивись на нього; якщо дія незрозуміла, уточни.'
        elif doc:
            if not await sheet(permission,uid):raise ValueError('Для роботи з файлами потрібен доступ від власника.')
            if mime!='application/pdf':raise ValueError('Підтримуються фото й PDF. Імпорт інших файлів ще не підключено.')
            if int(doc.get('file_size') or 0)>20_000_000:raise ValueError('PDF завеликий (до 20 МБ).')
            state=queue_document(state,doc['file_id'],doc.get('file_name','document.pdf'),text)
            next(p for p in state['documents'] if p['file_id']==doc['file_id'])['unique_id']=doc.get('file_unique_id','')
            incoming_ids=[doc['file_id']]
            await sheet(store.save_dialogue,uid,state)
            text=text or 'Надсилаю PDF. Прочитай документ.'
        elif msg.get('voice'):
            if not await sheet(permission,uid):raise ValueError('Для голосових потрібен доступ від власника.')
            text=await transcribe_telegram_voice(msg,bot_call,settings.telegram_bot_token,openai)
        if not text:return
        runtime=AgentTools(sys.modules[__name__],uid,chat,person,state,update.get('update_id',msg.get('message_id',secrets.token_hex(8))))
        runtime.incoming_ids=incoming_ids
        await send(chat,await run_agent(openai,settings.openai_model,runtime,text))
    except ValueError as exc:
        await send(chat,str(exc))
    except Exception as exc:
        log.exception('Agent message failed')
        notice=quota_notice(exc,owner=uid in owners)
        await send(chat,notice+'\nЗбережені файли та записи залишаються. Після відновлення спочатку перевір стан операції.' if notice else
                   'Не вдалося завершити відповідь. Виконані кроки могли вже зберегтися; перевірю їх перед повторною дією.')


async def handle_callback(update):
    callback=update['callback_query']
    data=str(callback.get('data') or '')
    if data.startswith('inv:'):
        person=callback.get('from') or {};uid=int(person.get('id') or 0)
        chatdata=(callback.get('message') or {}).get('chat') or {}
        if chatdata.get('type')!='private' or chatdata.get('id')!=uid:
            await bot_call('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Ця кнопка доступна автору файла.','show_alert':True})
            return
        await bot_call('answerCallbackQuery',{'callback_query_id':callback['id']})
        try:
            state=await sheet(store.get_dialogue,uid)
            runtime=AgentTools(sys.modules[__name__],uid,uid,person,state,update['update_id'])
            await InvoiceIntake(runtime).callback(callback)
        except ValueError as exc:
            await send(uid,str(exc))
        except Exception:
            log.exception('Invoice project submission failed')
            await send(uid,'Не вдалося завершити доставку. Файл збережено; натисни той самий проєкт, щоб повторити.')
        return
    if not data.startswith('act:'):
        await handle_access_callback(callback)
        return
    person=callback.get('from') or {};uid=int(person.get('id') or 0)
    chatdata=(callback.get('message') or {}).get('chat') or {}
    if chatdata.get('type')!='private' or chatdata.get('id')!=uid:
        await bot_call('answerCallbackQuery',{'callback_query_id':callback['id'],'text':'Ця кнопка доступна автору чернетки.','show_alert':True})
        return
    await bot_call('answerCallbackQuery',{'callback_query_id':callback['id']})
    try:
        _,decision,token=data.split(':')
        if decision not in ('yes','no'):raise ValueError('Невідома кнопка.')
        state=await sheet(store.get_dialogue,uid)
        runtime=AgentTools(sys.modules[__name__],uid,uid,person,state,update['update_id'])
        result=await runtime.confirm(token,decision=='yes')
        # The server, not the model, applies the authenticated approval.
        state.setdefault('turns',[]).append({'user':'Натиснуто '+('Підтвердити' if decision=='yes' else 'Скасувати'),
            'tools':[{'tool':'confirmed_action','result':result}],'answer':''})
        await runtime.save()
        runtime.schemas=[]  # Approval result is explanatory; no further actions.
        await send(uid,await run_agent(openai,settings.openai_model,runtime,
                       'Поясни результат щойно підтвердженої кнопкою операції. Не повторюй її.'))
    except ValueError as exc:
        await send(uid,str(exc))
    except Exception as exc:
        log.exception('Confirmation failed')
        notice=quota_notice(exc,owner=uid in owners)
        await send(uid,notice+'\nПідтверджена кнопкою дія могла вже виконатися. Перед повтором потрібно перевірити запис.' if notice else
                   'Операцію не вдалося завершити повністю. Чернетка збережена; перевірю запис перед повтором.')
