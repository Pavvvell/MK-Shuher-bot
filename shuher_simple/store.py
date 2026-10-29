"""Google Sheets is the only database; all permissions are enforced by the bot.

The *_Shuher* tabs are internal metadata; ordinary business tabs are created
only by an explicit /create command from an owner Telegram ID.
"""
import base64
import csv
import io
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build

from business import (allowed_status, positive_quantity, stock_balances, stock_key,
                      procurement_from_request, number_text)
from dialogue import new_dialogue, normalize_dialogue

KYIV=ZoneInfo("Europe/Kyiv")
USERS="_Shuher_Users"
GRANTS="_Shuher_Access"
PENDING="_Shuher_Pending"
UPDATES="_Shuher_Updates"
DIALOG="_Shuher_Dialog"
META={
    USERS:["Telegram ID","Username","Ім'я","Активна таблиця","Перший вхід"],
    GRANTS:["Telegram ID","Таблиця","Доступ","Змінив"],
    PENDING:["Telegram ID","Таблиця","Дані JSON","Дійсне до"],
    UPDATES:["Telegram update ID","Час"],
    DIALOG:["Telegram ID","Розмова JSON","Оновлено"],
}
DEFAULT=["Найменування","Кількість","Одиниця","Проєкт","Статус","Коментар"]

def now():
    return datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")

def quoted(name):
    return "'"+name.replace("'","''")+"'"

def row_values(values,width):
    return (list(values)+[""]*width)[:width]

class SheetsStore:
    def __init__(self,spreadsheet_id,service_account_json_b64=""):
        if service_account_json_b64:
            info=json.loads(base64.b64decode(service_account_json_b64,validate=True))
            creds=service_account.Credentials.from_service_account_info(
                info,scopes=["https://www.googleapis.com/auth/spreadsheets"])
        else:
            creds,_=google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.id=spreadsheet_id
        self.client=build("sheets","v4",credentials=creds,cache_discovery=False).spreadsheets()

    def titles(self):
        result=self.client.get(spreadsheetId=self.id,fields="sheets.properties.title").execute()
        return [s["properties"]["title"] for s in result.get("sheets",[])]

    def bootstrap(self):
        existing=set(self.titles())
        missing=[x for x in META if x not in existing]
        if missing:
            requests=[{"addSheet":{"properties":{"title":name,"hidden":True}}} for name in missing]
            self.client.batchUpdate(spreadsheetId=self.id,body={"requests":requests}).execute()
        for name in missing:
            self.client.values().update(
                spreadsheetId=self.id,range=quoted(name)+"!A1",
                valueInputOption="RAW",body={"values":[META[name]]}).execute()

    def data(self,tab,limit=4000):
        return self.client.values().get(
            spreadsheetId=self.id,range=quoted(tab)+f"!A1:AZ{limit}",
            valueRenderOption="FORMATTED_VALUE").execute().get("values",[])

    def append(self,tab,values):
        return self.client.values().append(
            spreadsheetId=self.id,range=quoted(tab)+"!A:AZ",
            valueInputOption="RAW",insertDataOption="INSERT_ROWS",
            body={"values":[values]}).execute()

    def update(self,tab,row_number,values):
        self.client.values().update(spreadsheetId=self.id,
            range=quoted(tab)+f"!A{row_number}",valueInputOption="RAW",
            body={"values":[values]}).execute()

    def _upsert(self,tab,lookup,values):
        rows=self.data(tab)
        for i,row in enumerate(rows[1:],2):
            if lookup(row):
                self.update(tab,i,values)
                return False
        self.append(tab,values)
        return True

    def register(self,user):
        uid=str(user["id"])
        rows=self.data(USERS)
        for row in rows[1:]:
            if row and str(row[0])==uid:
                return False
        self.append(USERS,[uid,user.get("username",""),user.get("first_name",""),
                           "",now()])
        return True

    def users(self):
        return [row_values(x,5) for x in self.data(USERS)[1:] if x and x[0]]

    def user(self,uid):
        return next((r for r in self.users() if str(r[0])==str(uid)),None)

    def set_active(self,uid,table):
        record=self.user(uid)
        if not record:raise ValueError("Напиши /start.")
        record[3]=table
        self._upsert(USERS,lambda r:r and str(r[0])==str(uid),record)

    def tables(self):
        return [x for x in self.titles() if not x.startswith("_Shuher_")]

    def resolve(self,table):
        return next((x for x in self.tables() if x.casefold()==table.strip().casefold()),None)

    def create_table(self,name,columns=None):
        name=name.strip()
        if not name or len(name)>90 or name.startswith("_") or re.search(r"[\\/\[\]*?:]",name):
            raise ValueError("Некоректна назва. До 90 символів, без / \\ [ ] * ? :")
        if self.resolve(name):
            raise ValueError("Така таблиця вже існує.")
        requested=columns or DEFAULT
        headers=["ID","Дата"]+requested+["Автор"]
        cleaned=[]
        for c in headers:
            c=str(c).strip()
            if not c or len(c)>70 or c.casefold() in {x.casefold() for x in cleaned}:
                continue
            cleaned.append(c)
        if len(cleaned)<4 or len(cleaned)>35:
            raise ValueError("Потрібно від 1 до 32 різних колонок.")
        self.client.batchUpdate(spreadsheetId=self.id,body={
            "requests":[{"addSheet":{"properties":{"title":name,"gridProperties":{
                "frozenRowCount":1}}}}]}).execute()
        self.update(name,1,cleaned)
        return cleaned

    def grant(self,uid,table,mode,owner):
        if not self.user(uid):
            raise ValueError("Користувач ще не написав боту /start.")
        resolved=self.resolve(table)
        if not resolved:raise ValueError("Таблиці не знайдено. Перевір /tables.")
        self._upsert(GRANTS,
            lambda r:len(r)>1 and str(r[0])==str(uid) and str(r[1]).casefold()==resolved.casefold(),
            [str(uid),resolved,mode,str(owner)])

    def permissions(self,uid,owners):
        if int(uid) in owners:
            return {t:"write" for t in self.tables()}
        out={}
        for row in self.data(GRANTS)[1:]:
            if len(row)>2 and str(row[0])==str(uid) and row[2] in ("read","write"):
                table=self.resolve(row[1])
                if table:out[table]=row[2]
        return out

    def get_pending(self,uid):
        for row in self.data(PENDING)[1:]:
            if len(row)>3 and str(row[0])==str(uid) and row[2]:
                try:
                    expiry=datetime.fromisoformat(row[3])
                    if expiry<datetime.now(timezone.utc):return None
                    return {"table":row[1],"payload":json.loads(row[2])}
                except (ValueError,TypeError,KeyError,json.JSONDecodeError):
                    return None
        return None

    def save_pending(self,uid,table,payload):
        expiry=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()
        self._upsert(PENDING,lambda r:r and str(r[0])==str(uid),
                     [str(uid),table,json.dumps(payload,ensure_ascii=False),expiry])

    def clear_pending(self,uid):
        self._upsert(PENDING,lambda r:r and str(r[0])==str(uid),
                     [str(uid),"","",""])

    def get_dialogue(self,uid):
        for row in self.data(DIALOG)[1:]:
            if len(row)>=2 and str(row[0])==str(uid):
                try:
                    return normalize_dialogue(json.loads(row[1]))
                except (TypeError,ValueError,json.JSONDecodeError):
                    return new_dialogue()
        return new_dialogue()

    def save_dialogue(self,uid,state):
        normalized=normalize_dialogue(state)
        payload=json.dumps(normalized,ensure_ascii=False,separators=(",",":"))
        self._upsert(DIALOG,lambda row:row and str(row[0])==str(uid),
                     [str(uid),payload,now()])

    def clear_dialogue(self,uid):
        self.save_dialogue(uid,new_dialogue())

    def processed(self,update_id):
        rows=self.client.values().get(spreadsheetId=self.id,
            range=quoted(UPDATES)+"!A2:A30000").execute().get("values",[])
        return any(row and str(row[0])==str(update_id) for row in rows)

    def mark_processed(self,update_id):
        self.append(UPDATES,[str(update_id),now()])

    def read_table(self,table):
        resolved=self.resolve(table)
        if not resolved:raise ValueError("Таблиці не знайдено.")
        rows=self.data(resolved)
        if not rows:return [],[]
        return rows[0],rows[1:]

    def append_record(self,table,values,record_id,author):
        headers,rows=self.read_table(table)
        if not headers or not str(headers[0]).casefold().startswith("id"):
            raise ValueError("Для запису потрібна перша колонка ID / ID позиції / ID операції.")
        if any(row and row[0]==record_id for row in rows):
            return False
        final=[]
        for i,key in enumerate(headers):
            label=str(key).casefold().strip()
            if i==0:value=record_id
            elif label in ("дата","дата й час","створено (дата і час)"):value=now()
            elif label in ("автор","ініціатор","вніс"):value=values.get(key) or author
            elif label=="id заявки":value=values.get(key,"")
            else:value=values.get(key,"")
            if value is None:value=""
            # Keep stock quantities and prices numeric inside Google Sheets.
            if isinstance(value,str) and any(word in label for word in
                    ("кількіст","ціна","сума","залишок","наявно","вартіст")):
                normalized=value.strip().replace(" ","").replace(",",".")
                if re.fullmatch(r"-?\d+(?:\.\d+)?",normalized):
                    value=float(normalized) if "." in normalized else int(normalized)
            final.append(value if isinstance(value,(int,float)) else str(value))
        self.append(table,final)
        return True


    def _lookup(self,table,record_id):
        headers,rows=self.read_table(table)
        matches=[(index,row) for index,row in enumerate(rows,2)
                 if row and str(row[0]).strip()==str(record_id).strip()]
        if len(matches)>1:
            raise ValueError("Знайдено кілька рядків із таким ID. Потрібна перевірка власника.")
        if not matches:
            raise ValueError("ID не знайдено у «"+table+"»: "+str(record_id))
        return headers,matches[0][0],matches[0][1]

    def update_status(self,table,record_id,field,new_value,expected_old):
        """Update a single status cell, never rewrite unrelated columns."""
        field,new_value=allowed_status(table,field,new_value)
        headers,row_num,row=self._lookup(table,record_id)
        if field not in headers:
            raise ValueError("У таблиці немає колонки «"+field+"».")
        index=headers.index(field)
        current=str(row[index]).strip() if index<len(row) else ""
        if current==new_value:
            return False
        if current!=expected_old:
            raise ValueError("Статус уже змінився з моменту підтвердження. Перевір запис і повтори /status.")
        column=""
        number=index+1
        while number:
            number,remainder=divmod(number-1,26)
            column=chr(65+remainder)+column
        self.client.values().update(
            spreadsheetId=self.id,range=quoted(table)+f"!{column}{row_num}",
            valueInputOption="RAW",body={"values":[[new_value]]}).execute()
        return True

    def prepare_status(self,table,record_id,field,new_value):
        field,new_value=allowed_status(table,field,new_value)
        headers,_,row=self._lookup(table,record_id)
        if field not in headers:
            raise ValueError("У таблиці немає колонки «"+field+"».")
        index=headers.index(field)
        old=str(row[index]).strip() if index<len(row) else ""
        return field,new_value,old

    def append_workflow(self,table,values,record_id,author):
        """Write a row and, for requests, its deterministic linked purchasing row.

        If the first write succeeds but the second fails, retrying the same
        pending operation repairs the purchase without duplicating the request.
        """
        resolved=self.resolve(table)
        if not resolved:
            raise ValueError("Таблицю не знайдено.")
        values=dict(values)
        if resolved=="Заявки":
            if not all(values.get(name) and str(values.get(name)).strip().casefold()
                       not in ("null","none","невідомо","-")
                       for name in ("Проєкт","Найменування","Од. вим.")):
                raise ValueError("У заявці обов'язкові проєкт, найменування та одиниця виміру.")
            positive_quantity(values.get("Кількість",""))
            if not self.resolve("Закупка"):
                raise ValueError("Спочатку власник має створити вкладку «Закупка».")
            values["Статус"]="У закупці"
            created=self.append_record(resolved,values,record_id,author)
            # One linked procurement per request. The deterministic ID
            # protects against Telegram retries and interrupted operations.
            purchase_id="ZK-"+record_id
            pvalues=procurement_from_request(values,record_id)
            pcreated=self.append_record("Закупка",pvalues,purchase_id,author)
            return created,pcreated

        if resolved=="Склад":
            headers,rows=self.read_table("Склад")
            if any(row and str(row[0])==record_id for row in rows):
                return False,False
            kind=str(values.get("Тип операції","")).strip()
            if kind not in ("Надходження","Видача"):
                raise ValueError("Для складу вкажи тип «Надходження» або «Видача».")
            name=str(values.get("Найменування","")).strip()
            unit=str(values.get("Од. вим.","")).strip()
            if not name or not unit:
                raise ValueError("Для складу потрібні найменування та одиниця виміру.")
            amount=positive_quantity(values.get("Кількість",""))
            reference=str(values.get("ID заявки","")).strip()
            purchase_id=str(values.get("ID закупівлі","")).strip()
            if reference:
                self._lookup("Заявки",reference)
                purchases_headers,purchases=self.read_table("Закупка")
                ref_col=purchases_headers.index("ID заявки")
                matching=[r for r in purchases if len(r)>ref_col and str(r[ref_col])==reference]
                if not matching:
                    raise ValueError("Для цієї заявки ще немає закупівлі.")
                valid_ids={str(r[0]) for r in matching}
                if purchase_id and purchase_id not in valid_ids:
                    raise ValueError("ID закупівлі не відповідає заявці.")
                if not purchase_id:
                    purchase_id=str(matching[0][0])
            elif purchase_id:
                self._lookup("Закупка",purchase_id)
            if "ID закупівлі" in headers and purchase_id:
                values["ID закупівлі"]=purchase_id
            if kind=="Видача":
                balances,_,_=stock_balances(headers,rows)
                key=stock_key(name,values.get("Характеристики",""),unit)
                available=balances.get(key,0)
                if amount>available:
                    raise ValueError("Недостатньо на складі: "+name+" — "+number_text(available)+
                                     " "+unit+". Видачу не записано.")
            created=self.append_record(resolved,values,record_id,author)
            return created,False

        if resolved=="Закупка" and values.get("ID заявки"):
            reference=str(values["ID заявки"]).strip()
            self._lookup("Заявки",reference)
            headers,rows=self.read_table("Закупка")
            idx=headers.index("ID заявки")
            if any(len(r)>idx and str(r[idx]).strip()==reference for r in rows):
                raise ValueError("Ця заявка вже є в «Закупка». Зміни її статус через /status.")
        return self.append_record(resolved,values,record_id,author),False

    def balances(self,query=""):
        headers,rows=self.read_table("Склад")
        amounts,labels,skipped=stock_balances(headers,rows)
        wanted=query.strip().casefold()
        selected=[(label,amounts[k]) for k,label in labels.items()
                  if not wanted or wanted in " ".join(label).casefold()]
        selected.sort(key=lambda item:item[0][0].casefold())
        lines=["Залишки складу (надходження мінус видача):"]
        for (name,spec,unit),amount in selected[:45]:
            suffix=(" · "+spec) if spec else ""
            warning=" ⚠️" if amount<0 else ""
            lines.append(f"• {name}{suffix}: {number_text(amount)} {unit}{warning}")
        if not selected:
            lines.append("Позицій не знайдено.")
        if len(selected)>45:
            lines.append("Показано 45 позицій. Уточни пошук: /balance назва")
        if skipped:
            lines.append(f"⚠️ Пропущено некоректних рухів: {skipped}. Перевір вкладку «Склад».")
        return "\n".join(lines)

    def csv_data(self,table):
        headers,rows=self.read_table(table)
        buffer=io.StringIO()
        writer=csv.writer(buffer)
        # Escape spreadsheet-formula prefixes in exported CSV files.
        def safe(cell):
            value=str(cell or "")
            if value.lstrip().startswith(("=","+","-","@")):
                return "'"+value
            return value
        writer.writerow([safe(c) for c in headers])
        for row in rows:
            writer.writerow([safe(c) for c in row])
        return ("\ufeff"+buffer.getvalue()).encode("utf-8")
