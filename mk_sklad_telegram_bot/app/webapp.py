"""Telegram Mini App: authenticated, role-scoped PostgreSQL API."""
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import PurePath
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import Settings
from app.pgstore import SHEETS, PostgresLedger, cells_as_list, update_record
from app.migration import migrate, import_status

router = APIRouter(prefix="/api/v1")
settings = Settings()
MAX_FILE_BYTES = 8 * 1024 * 1024

ROLE_SHEETS = {
    "owner": set(SHEETS),
    "buyer": {"Заявки", "Закупка", "Доручення"},
    "storekeeper": {"Заявки", "Склад", "Доручення", "Інвентаризація"},
}

def _authorized(init_data: str) -> dict:
    if not init_data or len(init_data) > 16000:
        raise HTTPException(401, "Відкрий Шухера в Telegram.")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(pairs) != len(data):
        raise HTTPException(401, "Некоректний Telegram підпис.")
    supplied_hash = data.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied_hash):
        raise HTTPException(401, "Відсутній підпис Telegram.")
    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        telegram_id = int(user["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(401, "Не вдалося визначити користувача Telegram.")
    now = int(datetime.now(timezone.utc).timestamp())
    if not (0 <= now-auth_date <= 86400):
        raise HTTPException(401, "Сесія Telegram застаріла. Перевідкрий Mini App.")
    data_check = "\n".join(f"{key}={value}" for key,value in sorted(data.items()))
    secret = hmac.new(b"WebAppData",settings.telegram_bot_token.encode(),hashlib.sha256).digest()
    expected = hmac.new(secret,data_check.encode(),hashlib.sha256).hexdigest()
    if not secrets.compare_digest(expected,supplied_hash.lower()):
        raise HTTPException(401, "Підпис Telegram недійсний.")
    role = settings.role(telegram_id)
    if not role:
        raise HTTPException(403,"Доступ до Шухера не надано.")
    return {"id":telegram_id,"role":role,"name":user.get("first_name","")}

async def actor(x_telegram_init_data: str = Header(default="")):
    return _authorized(x_telegram_init_data)

def accessible(role, sheet):
    if sheet not in ROLE_SHEETS.get(role,set()):
        raise HTTPException(403,"Немає доступу до цього розділу.")

def task_visible(role, cells):
    if role=="owner":
        return True
    assignee=str(cells[4] if len(cells)>4 else "").casefold()
    return ("юр" in assignee if role=="buyer" else "комір" in assignee)

def db_pool():
    # Imported lazily to avoid circular imports with app.main.
    from app.main import app
    return app.state.pool

@router.get("/me")
async def me(user=Depends(actor)):
    return {**user,"sheets":[s for s in SHEETS if s in ROLE_SHEETS[user["role"]]],
            "max_file_mb":8}

@router.get("/dashboard")
async def dashboard(user=Depends(actor)):
    pool=db_pool()
    allowed=ROLE_SHEETS[user["role"]]
    async with pool.acquire() as conn:
        rows=await conn.fetch(
            "SELECT sheet,COUNT(*) AS count FROM ledger_records WHERE sheet=ANY($1::text[]) GROUP BY sheet",
            list(allowed))
        last=await conn.fetchval("SELECT MAX(updated_at) FROM ledger_records")
    return {"sections":{r["sheet"]:r["count"] for r in rows},
            "updated_at":last.isoformat() if last else None,"role":user["role"]}

@router.get("/records/{sheet}")
async def records(sheet: str, q: str="", project: str="", status: str="",
                  limit: int=Query(default=30,ge=1,le=100),
                  offset: int=Query(default=0,ge=0), user=Depends(actor)):
    accessible(user["role"],sheet)
    if max(len(q),len(project),len(status))>120:
        raise HTTPException(400,"Занадто довгий фільтр.")
    clauses=["sheet=$1"];args=[sheet]
    if q.strip():
        args.append("%"+q.strip().replace("%",r"\%").replace("_",r"\_")+"%")
        clauses.append(f"search_text ILIKE ${len(args)} ESCAPE '\\'")
    if project.strip():
        args.append(project.strip())
        clauses.append(f"project=${len(args)}")
    if status.strip():
        args.append(status.strip())
        clauses.append(f"status=${len(args)}")
    # Enforce task row visibility on the backend, not just in the UI.
    if sheet=="Доручення" and user["role"]!="owner":
        args.append("%юр%" if user["role"]=="buyer" else "%комір%")
        clauses.append(f"cells->>4 ILIKE ${len(args)}")
    where=" AND ".join(clauses)
    async with db_pool().acquire() as conn:
        total=await conn.fetchval("SELECT COUNT(*) FROM ledger_records WHERE "+where,*args)
        args.extend([limit,offset])
        rows=await conn.fetch(
            f"""SELECT record_key,project,title,status,cells,version,updated_at
                FROM ledger_records WHERE {where}
                ORDER BY updated_at DESC,record_key DESC
                LIMIT ${len(args)-1} OFFSET ${len(args)}""",*args)
    return {"total":total,"items":[{"id":r["record_key"],"project":r["project"],
            "title":r["title"],"status":r["status"],"cells":cells_as_list(r),
            "version":r["version"],"updated_at":r["updated_at"].isoformat()} for r in rows]}

@router.get("/records/{sheet}/{record_id}")
async def record_detail(sheet: str,record_id:str,user=Depends(actor)):
    accessible(user["role"],sheet)
    async with db_pool().acquire() as conn:
        r=await conn.fetchrow(
            "SELECT * FROM ledger_records WHERE sheet=$1 AND record_key=$2",sheet,record_id)
        if r is None or (sheet=="Доручення" and not task_visible(user["role"],cells_as_list(r))):
            raise HTTPException(404,"Запис не знайдено.")
        h=r["headers"]
        if isinstance(h,str):h=json.loads(h)
    return {"sheet":sheet,"id":r["record_key"],"project":r["project"],
            "title":r["title"],"status":r["status"],"cells":cells_as_list(r),
            "headers":h,"version":r["version"],"origin":r["origin"]}

class RequestNew(BaseModel):
    project: str=Field(min_length=1,max_length=120)
    item: str=Field(min_length=1,max_length=300)
    quantity: str=Field(min_length=1,max_length=40)
    unit: str=Field(min_length=1,max_length=30)
    details: str=Field(default="",max_length=2000)

@router.post("/requests")
async def request_new(body:RequestNew,user=Depends(actor)):
    from app.sheets import text_number
    try:
        if text_number(body.quantity)<=0:
            raise ValueError()
    except Exception:
        raise HTTPException(422,"Кількість має бути додатним числом.")
    date=datetime.now().strftime("%Y%m%d")
    code="З-"+date+"-TG"+uuid.uuid4().hex[:6].upper()
    payload={"request_id":code,"project":body.project,"item":body.item,
             "quantity":body.quantity,"unit":body.unit,"details":body.details,
             "role":user["role"]}
    result=await PostgresLedger(db_pool(),user["id"]).create_request(payload)
    return {"id":code,"message":result}

class TaskUpdate(BaseModel):
    status:str
    comment:str=Field(default="",max_length=3000)
    version:int=Field(ge=1)

@router.post("/tasks/{record_id}/status")
async def task_update(record_id:str,body:TaskUpdate,user=Depends(actor)):
    if body.status not in {"Нове","У роботі","Очікує рішення","На перевірці","Виконано","Заблоковано"}:
        raise HTTPException(422,"Неприпустимий статус.")
    async with db_pool().acquire() as conn:
        async with conn.transaction():
            row=await conn.fetchrow(
                "SELECT cells,version FROM ledger_records WHERE sheet='Доручення' AND record_key=$1 FOR UPDATE",
                record_id)
            if row is None:
                raise HTTPException(404,"Доручення не знайдено.")
            before=cells_as_list(row)
            if not task_visible(user["role"],before):
                raise HTTPException(403,"Це не твоє доручення.")
            if row["version"] != body.version:
                raise HTTPException(409,"Доручення вже змінене. Онови сторінку.")
            after=before+[""]*max(0,20-len(before))
            after[10],after[11]=body.status,body.comment
            after[12]=datetime.now().strftime("%d.%m.%Y %H:%M")
            await update_record(conn,"Доручення",record_id,before,after,user["id"],"task_status")
    return {"ok":True,"version":body.version+1}

async def _parent(conn,sheet,record_id,user):
    accessible(user["role"],sheet)
    row=await conn.fetchrow(
        "SELECT cells FROM ledger_records WHERE sheet=$1 AND record_key=$2",
        sheet,record_id)
    if row is None or (sheet=="Доручення" and not task_visible(user["role"],cells_as_list(row))):
        raise HTTPException(404,"Пов'язаний запис не знайдено.")

@router.get("/records/{sheet}/{record_id}/attachments")
async def attachments(sheet:str,record_id:str,user=Depends(actor)):
    async with db_pool().acquire() as conn:
        await _parent(conn,sheet,record_id,user)
        rows=await conn.fetch(
            """SELECT id,filename,content_type,size_bytes,external_url,note,created_at
               FROM ledger_attachments WHERE sheet=$1 AND record_key=$2 ORDER BY created_at DESC""",
            sheet,record_id)
    return [{"id":str(r["id"]),"filename":r["filename"],"mime":r["content_type"],
             "size":r["size_bytes"],"url":r["external_url"],"note":r["note"],
             "created_at":r["created_at"].isoformat()} for r in rows]

@router.post("/attachments")
async def upload_attachment(sheet:str=Form(...),record_id:str=Form(...),
                            file:UploadFile=File(...),note:str=Form(""),user=Depends(actor)):
    if len(note)>1000:raise HTTPException(422,"Завелика примітка.")
    name=PurePath(file.filename or "document").name[:200] or "document"
    binary=await file.read(MAX_FILE_BYTES+1)
    if len(binary)>MAX_FILE_BYTES:
        raise HTTPException(413,"Максимальний розмір файлу — 8 МБ.")
    if not binary:raise HTTPException(422,"Порожній файл.")
    mime=(file.content_type or "application/octet-stream")[:120]
    identifier=uuid.uuid4()
    async with db_pool().acquire() as conn:
        async with conn.transaction():
            await _parent(conn,sheet,record_id,user)
            await conn.execute(
                """INSERT INTO ledger_attachments
                  (id,sheet,record_key,uploaded_by,filename,content_type,size_bytes,sha256,blob,note)
                  VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                identifier,sheet,record_id,user["id"],name,mime,len(binary),
                hashlib.sha256(binary).hexdigest(),binary,note)
    return {"id":str(identifier),"filename":name,"size":len(binary)}

class ExternalLink(BaseModel):
    sheet:str
    record_id:str
    url:str=Field(max_length=2048)
    title:str=Field(default="",max_length=200)
    note:str=Field(default="",max_length=1000)

@router.post("/links")
async def add_link(body:ExternalLink,user=Depends(actor)):
    parsed=urlsplit(body.url)
    if parsed.scheme not in ("http","https") or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(422,"Потрібне звичайне HTTPS/HTTP посилання.")
    identifier=uuid.uuid4()
    async with db_pool().acquire() as conn:
        async with conn.transaction():
            await _parent(conn,body.sheet,body.record_id,user)
            await conn.execute(
                """INSERT INTO ledger_attachments
                   (id,sheet,record_key,uploaded_by,filename,content_type,external_url,note)
                   VALUES($1,$2,$3,$4,$5,'text/uri-list',$6,$7)""",
                identifier,body.sheet,body.record_id,user["id"],body.title or body.url,
                body.url,body.note)
    return {"id":str(identifier),"url":body.url}

@router.get("/files/{file_id}")
async def download_file(file_id:uuid.UUID,user=Depends(actor)):
    async with db_pool().acquire() as conn:
        r=await conn.fetchrow(
            "SELECT * FROM ledger_attachments WHERE id=$1",file_id)
        if r is None:raise HTTPException(404,"Файл не знайдено.")
        await _parent(conn,r["sheet"],r["record_key"],user)
    if r["blob"] is None:
        raise HTTPException(404,"Це посилання, а не вкладений файл.")
    safe_name=re.sub(r"[^A-Za-z0-9._-]","_",r["filename"]) or "attachment"
    return Response(bytes(r["blob"]),media_type="application/octet-stream",
                    headers={"Content-Disposition":f'attachment; filename="{safe_name}"',
                             "X-Content-Type-Options":"nosniff","Cache-Control":"private, no-store"})

@router.get("/migration/status")
async def migration_status(user=Depends(actor)):
    if user["role"]!="owner":raise HTTPException(403)
    return await import_status(db_pool())

@router.get("/migration/preview")
async def migration_preview(user=Depends(actor)):
    if user["role"]!="owner":raise HTTPException(403)
    return await migrate(db_pool(),settings.google_service_account_json_b64,
                         settings.import_source_spreadsheet_id,True)

class ImportConfirmation(BaseModel):
    confirm:str

@router.post("/migration/import")
async def migration_import(body:ImportConfirmation,user=Depends(actor)):
    if user["role"]!="owner":raise HTTPException(403)
    if body.confirm!="IMPORT":
        raise HTTPException(422,"Для імпорту введи IMPORT.")
    return await migrate(db_pool(),settings.google_service_account_json_b64,
                         settings.import_source_spreadsheet_id,False)
