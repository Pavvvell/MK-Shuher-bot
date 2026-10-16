"""One-time, idempotent Google Sheets -> PostgreSQL migration.

All six existing tabs are imported without changing the source spreadsheet.
Conflicts are never silently overwritten. This is deliberately not a sync daemon.
"""
import asyncio
import json
from collections import Counter

from app.pgstore import SHEETS, insert_record, record_key, cells_as_list
from app.sheets import Sheets

WIDTH = {"Заявки":"Q","Закупка":"Y","Склад":"Q","Доручення":"T",
         "Уточнення відомостей":"G","Інвентаризація":"M"}

async def snapshot_sheet(client, name):
    def fetch():
        return client._service().values().get(
            spreadsheetId=client.spreadsheet_id,
            range=f"'{name}'!A1:{WIDTH[name]}2500",
            valueRenderOption="FORMATTED_VALUE").execute().get("values", [])
    rows = await asyncio.to_thread(fetch)
    if not rows:
        raise ValueError(f"Джерело: аркуш {name} порожній або не існує.")
    headers = rows[0]
    items = [(row_no, row, record_key(name, row, row_no))
             for row_no, row in enumerate(rows[1:],2) if row and any(str(x).strip() for x in row)]
    keys = [r[2] for r in items]
    repeated = [x for x,n in Counter(keys).items() if n>1]
    if repeated:
        raise ValueError(f"Неунікальні ідентифікатори в {name}: {', '.join(repeated[:5])}")
    return headers, items

async def prepare_snapshot(service_account_b64, source_id):
    if not source_id:
        raise ValueError("IMPORT_SOURCE_SPREADSHEET_ID не налаштовано.")
    client = Sheets(source_id, service_account_b64)
    payload = {}
    for name in SHEETS:
        payload[name] = await snapshot_sheet(client, name)
    return payload

async def import_status(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sheet,COUNT(*) AS n FROM ledger_records GROUP BY sheet ORDER BY sheet")
        last = await conn.fetchrow(
            "SELECT id,source_spreadsheet_id,success,finished_at,report FROM ledger_import_runs ORDER BY id DESC LIMIT 1")
    def obj(r):
        d = dict(r)
        if isinstance(d.get("report"),str): d["report"]=json.loads(d["report"])
        if d.get("finished_at"): d["finished_at"]=d["finished_at"].isoformat()
        return d
    return {"records":{r["sheet"]:r["n"] for r in rows},"last_run":obj(last) if last else None}

async def migrate(pool, service_account_b64, source_id, dry_run=True):
    snapshot = await prepare_snapshot(service_account_b64,source_id)
    expected = {name:len(rows) for name,(_,rows) in snapshot.items()}
    if dry_run:
        status = await import_status(pool)
        return {"mode":"preview","source":source_id,"expected":expected,
                "current":status["records"],"last_run":status["last_run"]}
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Prevent simultaneous imports. No overwrite of existing ledger data.
            await conn.execute("SELECT pg_advisory_xact_lock(8170452026)")
            sources = await conn.fetch(
                "SELECT DISTINCT source_spreadsheet_id FROM ledger_import_runs WHERE success=TRUE")
            if any(r["source_spreadsheet_id"] != source_id for r in sources):
                raise ValueError("PostgreSQL уже імпортував інше джерело; потрібна окрема перевірка, не змішуємо дані.")
            run_id = await conn.fetchval(
                "INSERT INTO ledger_import_runs(source_spreadsheet_id) VALUES($1) RETURNING id",source_id)
            inserted = {}
            skipped = {}
            inconsistent = {}
            for name,(headers,items) in snapshot.items():
                ins=skip=conflicts=0
                for line,cells,key in items:
                    existing = await conn.fetchrow(
                        "SELECT cells FROM ledger_records WHERE sheet=$1 AND record_key=$2",
                        name,key)
                    if existing is not None:
                        skip+=1
                        if cells_as_list(existing) != cells:
                            conflicts+=1
                        continue
                    if await insert_record(conn,name,key,cells,headers,line,origin="sheets_import"):
                        ins+=1
                inserted[name],skipped[name],inconsistent[name]=ins,skip,conflicts
            report={"expected":expected,"inserted":inserted,"existing":skipped,
                    "different_existing":inconsistent}
            await conn.execute(
                "UPDATE ledger_import_runs SET success=TRUE,finished_at=NOW(),report=$2::jsonb WHERE id=$1",
                run_id,json.dumps(report,ensure_ascii=False))
    return {"mode":"imported","source":source_id,"run_id":run_id,**report}
