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

KYIV=ZoneInfo("Europe/Kyiv")
USERS="_Shuher_Users"
GRANTS="_Shuher_Access"
PENDING="_Shuher_Pending"
UPDATES="_Shuher_Updates"
META={
    USERS:["Telegram ID","Username","Ім'я","Активна таблиця","Перший вхід"],
    GRANTS:["Telegram ID","Таблиця","Доступ","Змінив"],
    PENDING:["Telegram ID","Таблиця","Дані JSON","Дійсне до"],
    UPDATES:["Telegram update ID","Час"],
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
        if not headers or headers[0]!="ID":
            raise ValueError("Для запису потрібна таблиця з першою колонкою ID.")
        if any(row and row[0]==record_id for row in rows):
            return False
        final=[]
        for key in headers:
            if key=="ID":value=record_id
            elif key=="Дата":value=now()
            elif key=="Автор":value=author
            else:value=values.get(key,"")
            final.append(str(value or ""))
        self.append(table,final)
        return True

    def csv_data(self,table):
        headers,rows=self.read_table(table)
        buffer=io.StringIO()
        writer=csv.writer(buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        return ("\ufeff"+buffer.getvalue()).encode("utf-8")
