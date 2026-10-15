import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from fastapi import FastAPI, Header, HTTPException, Request

from app.ai import AssistantAI
from app.config import Settings
from app.db import prepare, take_update, release_update, stage, pending, clear_pending, audit
from app.sheets import Sheets, text_number

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mk-sklad")
settings = Settings()
bot = Bot(token=settings.telegram_bot_token)
dp = Dispatcher()
router = Router()
dp.include_router(router)
sheets = Sheets(settings.spreadsheet_id, settings.google_service_account_json_b64)
ai = AssistantAI(settings.openai_api_key, settings.openai_model)
KYIV = ZoneInfo("Europe/Kyiv")


def role_of(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return None
    return settings.role(message.from_user.id)


async def allowed(message: Message):
    role = role_of(message)
    if role is None:
        await message.answer("Доступ не надано. Надішли /id власнику системи для додавання Telegram ID.")
    return role


@router.message(Command("id"))
async def user_id(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    await message.answer(f"Твій Telegram ID: {message.from_user.id}")


@router.message(CommandStart())
@router.message(Command("help"))
async def help_bot(message: Message):
    if not await allowed(message):
        return
    await message.answer(
        "МК Склад. Пиши вільним текстом. Приклади:\n"
        "• Що є в закупівлях на АР-26?\n"
        "• Потрібно купити 10 ременів B1015 для Металообробки, шт.\n"
        "• (тільки власник) Оплату закупівлі METK-1206-01 проведено.\n"
        "• (склад) Фактично прийняв METK-1206-01: 3 м, стелаж 4.\n"
        "Перед записом бот просить підтвердження «так» або «ні».\n"
        "Зараз підтримуються текстові повідомлення; фото/PDF/голосові — наступний модуль."
    )


@router.message(Command("cancel"))
async def cancel(message: Message):
    if not await allowed(message):
        return
    await clear_pending(app.state.pool, message.from_user.id)
    await message.answer("Чернетку дії скасовано.")


async def status_context(role: str, question: str) -> str:
    names = (["Заявки", "Закупка", "Склад", "Доручення"] if role == "owner"
             else ["Заявки", "Закупка"] if role == "buyer" else ["Заявки", "Склад"])
    terms = [t.strip(".,!?():;\"'").casefold() for t in question.split() if len(t) >= 4]
    segments = []
    for name in names:
        rows = await sheets.read(name)
        matches = [r for r in rows if any(t in " ".join(map(str,r["cells"])).casefold() for t in terms)]
        selected = matches[-35:] if matches else rows[-20:]
        segments.append(json.dumps({"sheet": name, "total_rows": len(rows),
            "sample": selected}, ensure_ascii=False, default=str))
    return "\n".join(segments)[:26000]


async def prepare_action(role: str, data: dict) -> tuple[str, dict]:
    intent = data["intent"]
    if intent == "create_request":
        try:
            qty = text_number(data["quantity"])
        except InvalidOperation:
            raise ValueError("Для заявки вкажіть кількість числом.")
        if qty <= 0 or not all(data.get(k) for k in ("project", "item", "unit")):
            raise ValueError("Для заявки потрібні проєкт, товар, кількість і одиниця виміру.")
        code = "З-" + datetime.now(KYIV).strftime("%Y%m%d") + "-TG" + uuid.uuid4().hex[:6].upper()
        obj = {"request_id": code, "project": data["project"], "item": data["item"],
               "quantity": str(qty), "unit": data["unit"], "details": data["details"], "role": role}
        return f"Додати заявку {code}: {obj['item']}, {qty} {obj['unit']}, проєкт {obj['project']}?", obj
    if intent == "mark_paid":
        if role != "owner":
            raise ValueError("Позначати оплату підтвердженою може тільки власник.")
        purchase = await sheets.find_purchase(data["purchase_ref"])
        obj = {"purchase_id": purchase["cells"][0]}
        return f"ПІДТВЕРДЖУЄШ ФАКТИЧНУ ОПЛАТУ закупівлі {obj['purchase_id']}? (Не план, не передано на оплату)", obj
    if intent == "receive":
        if role not in ("owner", "storekeeper"):
            raise ValueError("Приймання можуть вносити лише власник або комірник.")
        if not all(data.get(k) for k in ("purchase_ref", "quantity", "unit", "location")):
            raise ValueError("Для приймання вкажіть ID закупівлі, фактично отриману кількість, одиницю й місце зберігання.")
        try:
            qty = text_number(data["quantity"])
        except InvalidOperation:
            raise ValueError("Кількість має бути числом.")
        if qty <= 0:
            raise ValueError("Кількість має бути більше нуля.")
        purchase = await sheets.find_purchase(data["purchase_ref"])
        obj = {"purchase_id": purchase["cells"][0],"quantity": str(qty), "unit": data["unit"],
               "location": data["location"], "details": data["details"],
               "document": data["document"],
               "receipt_id": "SKL-" + datetime.now(KYIV).strftime("%Y%m%d") + "-TG" + uuid.uuid4().hex[:6].upper()}
        return (f"ПІДТВЕРДЖУЄШ ФІЗИЧНЕ ПРИЙМАННЯ {obj['purchase_id']}: "
                f"{qty} {obj['unit']}, місце: {obj['location']}?"), obj
    raise ValueError("Невідома дія.")


@router.message(F.text)
async def receive_text(message: Message):
    role = await allowed(message)
    if not role:
        return
    uid = message.from_user.id
    plain = message.text.strip()
    pending_action = await pending(app.state.pool, uid)
    if pending_action:
        if plain.casefold() in {"ні", "нет", "no", "скасувати", "cancel"}:
            await clear_pending(app.state.pool, uid)
            await message.answer("Скасовано. Дані не змінено.")
            return
        if plain.casefold() not in {"так", "да", "yes", "підтверджую"}:
            await message.answer("Є дія, що чекає на підтвердження. Напиши «так» або «ні» (/cancel).")
            return
        kind, data = pending_action["action_type"], pending_action["payload"]
        try:
            if kind == "create_request":
                result = await sheets.create_request(data)
                entity_id = data["request_id"]
            elif kind in {"mark_paid", "receive"}:
                if kind == "mark_paid" and role != "owner":
                    raise ValueError("Оплату може підтвердити лише власник.")
                if kind == "receive" and role not in {"owner", "storekeeper"}:
                    raise ValueError("Немає прав на приймання.")
                # Serialize writes to one purchase across all bot replicas.
                async with app.state.pool.acquire() as connection:
                    await connection.execute("SELECT pg_advisory_lock(hashtext($1))", data["purchase_id"])
                    try:
                        result = await (sheets.pay(data) if kind == "mark_paid" else sheets.receive(data))
                    finally:
                        await connection.execute("SELECT pg_advisory_unlock(hashtext($1))", data["purchase_id"])
                entity_id = data["purchase_id"] if kind == "mark_paid" else data["receipt_id"]
            else:
                raise ValueError("Невідома дія.")
            await audit(app.state.pool, uid, kind, entity_id, data, "confirmed")
            await clear_pending(app.state.pool, uid)
            await message.answer(result)
        except ValueError as error:
            await clear_pending(app.state.pool, uid)
            await message.answer(f"Не записано: {error}")
        return
    try:
        data = await ai.parse(plain)
        intent = data.get("intent", "other")
        if intent in {"create_request", "mark_paid", "receive"}:
            preview, payload = await prepare_action(role, data)
            await stage(app.state.pool, uid, intent, payload)
            await message.answer(preview + "\nВідповідь: так / ні (чернетка діє 30 хв).")
        elif intent == "status":
            context = await status_context(role, plain)
            await message.answer((await ai.answer(plain, role, context))[:3900])
        else:
            await message.answer("Зрозумів повідомлення, але ще не маю для нього безпечної дії. "
                                 "Поясни, чи це заявка, факт оплати, фізичне приймання або запит статусу. /help")
    except ValueError as error:
        await message.answer(str(error))
    except Exception:
        log.exception("Failed to handle message from Telegram user %s", uid)
        await message.answer("Не вдалося перевірити дані. Запис не підтверджую; спробуй повторити трохи пізніше.")


@router.message()
async def unsupported(message: Message):
    if not await allowed(message):
        return
    await message.answer("Ця стартова версія поки приймає тільки текст. Розбір фото, PDF і голосових — окремий модуль. Не записував матеріал за вкладенням.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    await prepare(app.state.pool)
    await bot.set_webhook(settings.public_base_url.rstrip("/") + "/telegram/webhook",
                          secret_token=settings.telegram_webhook_secret,
                          allowed_updates=["message"], drop_pending_updates=False)
    yield
    await bot.session.close()
    await app.state.pool.close()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
            x_telegram_bot_api_secret_token, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.json()
    update_id = body.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=400, detail="Missing update_id")
    if not await take_update(app.state.pool, update_id):
        return {"ok": True, "duplicate": True}
    try:
        await dp.feed_raw_update(bot, body)
    except Exception:
        await release_update(app.state.pool, update_id)
        log.exception("Webhook processing failed for update %s", update_id)
        raise HTTPException(status_code=503, detail="Temporary webhook failure")
    return {"ok": True}
