"""MVP: Google Sheets is the authoritative inventory ledger.
PostgreSQL records pending actions, Telegram delivery ids and the bot audit trail.
"""
import asyncio
import base64
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build

KYIV = ZoneInfo("Europe/Kyiv")

def text_number(value) -> Decimal:
    s = str(value or "0").strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    return Decimal(s)

def value_cell(value):
    if value is None or value == "":
        return {}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}

class Sheets:
    def __init__(self, spreadsheet_id: str, json_b64: str):
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if json_b64:
            data = json.loads(base64.b64decode(json_b64, validate=True))
            self.credentials = service_account.Credentials.from_service_account_info(
                data, scopes=scopes)
        else:
            # Cloud Run workload identity: no long-lived Google JSON key required.
            self.credentials, _ = google.auth.default(scopes=scopes)
        self.spreadsheet_id = spreadsheet_id

    def _service(self):
        return build("sheets", "v4", credentials=self.credentials, cache_discovery=False).spreadsheets()

    def _read(self, sheet: str):
        width = {"Заявки": "Q", "Закупка": "Y", "Склад": "Q", "Доручення": "T"}[sheet]
        result = self._service().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet}'!A1:{width}2500",
            valueRenderOption="FORMATTED_VALUE").execute()
        rows = result.get("values", [])
        return [{"row": i + 1, "cells": r} for i, r in enumerate(rows) if i and r and r[0]]

    async def read(self, sheet: str):
        return await asyncio.to_thread(self._read, sheet)

    def _append(self, sheet: str, data: list):
        return self._service().values().append(
            spreadsheetId=self.spreadsheet_id, range=f"'{sheet}'!A:Z",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"majorDimension": "ROWS", "values": [data]}).execute()

    async def append(self, sheet: str, data: list):
        return await asyncio.to_thread(self._append, sheet, data)

    def _batch(self, requests: list[dict]):
        return self._service().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}).execute()

    async def batch(self, requests: list[dict]):
        return await asyncio.to_thread(self._batch, requests)

    async def find_purchase(self, reference: str):
        if not reference or len(reference.strip()) < 4:
            raise ValueError("Вкажіть ID закупівлі або номер рахунку (щонайменше 4 символи).")
        ref = reference.casefold().strip()
        matched = [r for r in await self.read("Закупка") if
                   ref == str(r["cells"][0]).casefold().strip() or
                   (len(r["cells"]) > 10 and ref in str(r["cells"][10]).casefold())]
        if not matched:
            raise ValueError("Закупівлю за цим ID/рахунком не знайдено.")
        if len(matched) != 1:
            ids = ", ".join(str(r["cells"][0]) for r in matched[:10])
            raise ValueError(f"Кілька закупівель ({ids}). Назвіть точний ID закупівлі.")
        return matched[0]

    async def create_request(self, payload: dict) -> str:
        code = payload["request_id"]
        if any(r["cells"][0] == code + "-01" for r in await self.read("Заявки")):
            return f"Заявка {code} вже є в реєстрі (повтор не додано)."
        today = datetime.now(KYIV).strftime("%d.%m.%Y")
        row = [code + "-01", code, today, payload["project"], payload["item"],
               payload.get("details", ""), payload["quantity"], payload["unit"],
               "", "Звичайний", "", payload["quantity"],
               "Підтверджена" if payload["role"] == "owner" else "На погодженні власника",
               payload["role"], "", "Створено Telegram-ботом за підтвердженням відправника", today]
        await self.append("Заявки", row)
        return f"Заявку {code} записано: {payload['item']} — {payload['quantity']} {payload['unit']}, проєкт {payload['project']}."

    async def pay(self, payload: dict) -> str:
        purchase = await self.find_purchase(payload["purchase_id"])
        c = purchase["cells"]
        if len(c) > 11 and str(c[11]).strip().lower().startswith("оплачено"):
            return f"Закупівля {c[0]} вже позначена оплаченою; повтор не додано."
        today = datetime.now(KYIV).strftime("%d.%m.%Y")
        changes = [(11, "Оплачено — підтверджено власником"), (12, today), (24, today)]
        req = [{"updateCells": {
            "start": {"sheetId": 101, "rowIndex": purchase["row"]-1, "columnIndex": col},
            "rows": [{"values": [value_cell(val)]}], "fields": "userEnteredValue"}}
            for col, val in changes]
        await self.batch(req)
        return f"Оплату {c[0]} підтверджено власником і внесено ({today})."

    async def receive(self, payload: dict) -> str:
        purchase = await self.find_purchase(payload["purchase_id"])
        c = purchase["cells"]
        unit = c[5] if len(c) > 5 else ""
        if unit.strip().casefold() != payload["unit"].strip().casefold():
            raise ValueError(f"Одиниці не збігаються: закупівля в «{unit}», повідомлення в «{payload['unit']}».")
        ordered = text_number(c[4])
        received = text_number(c[19] if len(c)>19 else "0")
        qty = text_number(payload["quantity"])
        if qty <= 0 or qty > ordered - received:
            raise ValueError(f"Кількість перевищує залишок до поставки ({ordered-received} {unit}) або дорівнює нулю.")
        code = payload["receipt_id"]
        if any(r["cells"][0] == code for r in await self.read("Склад")):
            return f"Приймання {code} вже записано (повтор не додано)."
        today = datetime.now(KYIV).strftime("%d.%m.%Y")
        stock = [code, today, "Надходження", c[0], c[1] if len(c)>1 else "",
                 c[2], c[3], payload.get("details", ""), str(qty), unit, c[6],
                 payload["location"], c[2], payload.get("document", ""),
                 "Telegram / підтвердження комірника", "Фактичне приймання підтверджено відправником", today]
        new_received, remain = received + qty, ordered - received - qty
        status = "Поставка повністю прийнята" if remain == 0 else "Частково прийнято складом"
        updates = [(13,status), (19,str(new_received)), (20,str(remain)), (24,today)]
        req = [{"appendCells": {"sheetId": 102,
                 "rows": [{"values": [value_cell(v) for v in stock]}],
                 "fields": "userEnteredValue"}}]
        req.extend({"updateCells": {
            "start": {"sheetId": 101,"rowIndex": purchase["row"]-1,"columnIndex": col},
            "rows": [{"values": [value_cell(val)]}],"fields":"userEnteredValue"}}
            for col,val in updates)
        await self.batch(req)
        return f"Приймання {code} записано: {qty} {unit}. Закупівля {c[0]}: отримано {new_received}/{ordered}, залишок {remain} {unit}."
