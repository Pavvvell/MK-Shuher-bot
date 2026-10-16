"""PostgreSQL ledger. Rows retain every original Sheets cell; API exposes typed facets.

Source import is insert-only, never overwrites operations written by Shuher.
"""
import json
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.sheets import text_number

KYIV = ZoneInfo("Europe/Kyiv")
SHEETS = ("Заявки", "Закупка", "Склад", "Доручення", "Уточнення відомостей", "Інвентаризація")

DDL = """
CREATE TABLE IF NOT EXISTS ledger_records (
    sheet TEXT NOT NULL,
    record_key TEXT NOT NULL,
    source_row INTEGER,
    cells JSONB NOT NULL,
    headers JSONB NOT NULL DEFAULT '[]'::jsonb,
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    search_text TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'sheets_import',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sheet, record_key)
);
CREATE INDEX IF NOT EXISTS ledger_sheet_project ON ledger_records(sheet, project);
CREATE INDEX IF NOT EXISTS ledger_sheet_status ON ledger_records(sheet, status);
CREATE INDEX IF NOT EXISTS ledger_updated ON ledger_records(updated_at DESC);
CREATE TABLE IF NOT EXISTS ledger_events (
    id BIGSERIAL PRIMARY KEY,
    actor_id BIGINT,
    action TEXT NOT NULL,
    sheet TEXT,
    record_key TEXT,
    before_cells JSONB,
    after_cells JSONB,
    info JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ledger_events_record ON ledger_events(sheet, record_key, id DESC);
CREATE TABLE IF NOT EXISTS ledger_attachments (
    id UUID PRIMARY KEY,
    sheet TEXT NOT NULL,
    record_key TEXT NOT NULL,
    uploaded_by BIGINT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    blob BYTEA,
    external_url TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT attached_record FOREIGN KEY (sheet, record_key)
        REFERENCES ledger_records(sheet, record_key),
    CONSTRAINT attachment_type CHECK
        ((blob IS NOT NULL AND external_url IS NULL) OR
         (blob IS NULL AND external_url IS NOT NULL)),
    CONSTRAINT attachment_max_size CHECK (size_bytes BETWEEN 0 AND 8388608)
);
CREATE INDEX IF NOT EXISTS attachment_parent ON ledger_attachments(sheet, record_key);
CREATE TABLE IF NOT EXISTS ledger_import_runs (
    id BIGSERIAL PRIMARY KEY,
    source_spreadsheet_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    report JSONB,
    success BOOLEAN NOT NULL DEFAULT FALSE
);
"""

def cells_as_list(row):
    raw = row["cells"]
    return json.loads(raw) if isinstance(raw, str) else list(raw)

def facets(sheet, cells):
    def at(i):
        return str(cells[i]) if len(cells) > i and cells[i] is not None else ""
    if sheet == "Заявки":
        return at(3), at(4), at(12)
    if sheet == "Закупка":
        return at(2), at(3), at(13) or at(11)
    if sheet == "Склад":
        return at(5), at(6), at(2)
    if sheet == "Доручення":
        return at(5), at(6), at(10)
    return at(0), at(2), at(5)

def record_key(sheet, cells, row_number):
    if sheet == "Уточнення відомостей":
        return "ROW-" + str(row_number)
    if sheet == "Інвентаризація" and len(cells) > 11 and cells[11]:
        return str(cells[11])
    return str(cells[0]) if cells and cells[0] else "ROW-" + str(row_number)

def json_text(value):
    return json.dumps(value, ensure_ascii=False, default=str)

async def prepare_ledger(pool):
    async with pool.acquire() as conn:
        await conn.execute(DDL)

async def record_event(conn, actor, action, sheet, key, before, after, info=None):
    await conn.execute(
        """INSERT INTO ledger_events(actor_id,action,sheet,record_key,before_cells,after_cells,info)
           VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb)""",
        actor, action, sheet, key, json_text(before) if before is not None else None,
        json_text(after) if after is not None else None, json_text(info or {}))

async def insert_record(conn, sheet, key, cells, headers=None, source_row=None, actor=None, origin="telegram"):
    project, title, status = facets(sheet, cells)
    val = await conn.fetchval(
        """INSERT INTO ledger_records
           (sheet,record_key,source_row,cells,headers,project,title,status,search_text,origin)
           VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$8,$9,$10)
           ON CONFLICT DO NOTHING RETURNING record_key""",
        sheet, key, source_row, json_text(cells), json_text(headers or []),
        project, title, status, " ".join(str(x) for x in cells if x is not None), origin)
    if val is not None:
        await record_event(conn, actor, "create" if origin != "sheets_import" else "import",
                           sheet, key, None, cells)
    return val is not None

async def update_record(conn, sheet, key, before, after, actor, action):
    project, title, status = facets(sheet, after)
    await conn.execute(
        """UPDATE ledger_records
           SET cells=$3::jsonb,project=$4,title=$5,status=$6,search_text=$7,
               version=version+1, updated_at=NOW()
           WHERE sheet=$1 AND record_key=$2""",
        sheet, key, json_text(after), project, title, status,
        " ".join(str(x) for x in after if x is not None))
    await record_event(conn, actor, action, sheet, key, before, after)

class PostgresLedger:
    def __init__(self, pool, actor_id=None):
        self.pool = pool
        self.actor_id = actor_id

    async def read(self, sheet):
        if sheet not in SHEETS:
            raise ValueError("Невідомий розділ.")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT source_row, cells FROM ledger_records
                   WHERE sheet=$1 ORDER BY COALESCE(source_row, 2147483647), created_at, record_key""", sheet)
        return [{"row": r["source_row"], "cells": cells_as_list(r)} for r in rows]

    async def find_purchase(self, reference):
        if not reference or len(reference.strip()) < 4:
            raise ValueError("Вкажіть ID закупівлі або номер рахунку.")
        ref = reference.casefold().strip()
        matched = [r for r in await self.read("Закупка")
                   if (r["cells"] and str(r["cells"][0]).casefold().strip() == ref)
                   or (len(r["cells"]) > 10 and ref in str(r["cells"][10]).casefold())]
        if len(matched) != 1:
            raise ValueError("Закупівлю не знайдено або збігів декілька; вкажіть точний ID.")
        return matched[0]

    async def create_request(self, payload):
        code = payload["request_id"]
        today = datetime.now(KYIV).strftime("%d.%m.%Y")
        cells = [code+"-01", code, today, payload["project"], payload["item"],
                 payload.get("details",""), payload["quantity"], payload["unit"], "",
                 "Звичайний", "", payload["quantity"],
                 "Підтверджена" if payload["role"] == "owner" else "На погодженні власника",
                 payload["role"], "", "Створено Шухером", today]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                added = await insert_record(conn, "Заявки", cells[0], cells,
                                            actor=self.actor_id)
        if not added:
            return "Заявка вже існує; повтор не додано."
        return f"Заявку {code} записано в PostgreSQL: {payload['item']} — {payload['quantity']} {payload['unit']}."

    async def pay(self, payload):
        key = payload["purchase_id"]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rec = await conn.fetchrow(
                    "SELECT cells FROM ledger_records WHERE sheet='Закупка' AND record_key=$1 FOR UPDATE", key)
                if rec is None:
                    raise ValueError("Не знайдено закупівлю.")
                before = cells_as_list(rec)
                if len(before)>11 and str(before[11]).casefold().startswith("оплачено"):
                    return "Оплата вже підтверджена; повтор не записано."
                after = before + [""]*max(0,25-len(before))
                today = datetime.now(KYIV).strftime("%d.%m.%Y")
                after[11], after[12], after[24] = "Оплачено — підтверджено власником", today, today
                await update_record(conn,"Закупка",key,before,after,self.actor_id,"payment_confirmed")
        return f"Оплату {key} підтверджено власником і внесено в PostgreSQL."

    async def receive(self, payload):
        key = payload["purchase_id"]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rec = await conn.fetchrow(
                    "SELECT cells FROM ledger_records WHERE sheet='Закупка' AND record_key=$1 FOR UPDATE", key)
                if rec is None:
                    raise ValueError("Закупівлю не знайдено.")
                before = cells_as_list(rec)
                row = before + [""]*max(0,25-len(before))
                unit = str(row[5])
                if unit.casefold().strip() != str(payload["unit"]).casefold().strip():
                    raise ValueError("Одиниці приймання не збігаються з закупівлею.")
                ordered = text_number(row[4])
                received = text_number(row[19])
                qty = text_number(payload["quantity"])
                if qty <= 0 or qty > ordered-received:
                    raise ValueError("Кількість перевищує залишок або некоректна.")
                receipt_id = payload["receipt_id"]
                exists = await conn.fetchval(
                    "SELECT 1 FROM ledger_records WHERE sheet='Склад' AND record_key=$1", receipt_id)
                if exists:
                    return "Це приймання вже зареєстроване."
                today = datetime.now(KYIV).strftime("%d.%m.%Y")
                stock = [receipt_id,today,"Надходження",row[0],row[1],row[2],row[3],
                         payload.get("details",""),str(qty),unit,row[6],payload["location"],
                         row[2],payload.get("document",""),"Telegram","Підтверджене фізичне приймання",today]
                await insert_record(conn,"Склад",receipt_id,stock,actor=self.actor_id)
                new_received=received+qty
                remaining=ordered-new_received
                row[19],row[20],row[24]=str(new_received),str(remaining),today
                row[13]="Поставка повністю прийнята" if remaining==0 else "Частково прийнято складом"
                await update_record(conn,"Закупка",key,before,row,self.actor_id,"receipt_confirmed")
        return f"Приймання {receipt_id}: {qty} {unit}. Отримано {new_received}/{ordered}, залишок {remaining}."
