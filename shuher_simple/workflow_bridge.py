"""Opt-in Codex bridge. One process/Sheets worker; never changes Telegram webhook."""
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone, timedelta, date

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

STAGING = '_Shuher_Codex_Staging'
INBOX = '_Shuher_Codex_Inbox'
OUTBOX = '_Shuher_Codex_Outbox'
HEADERS = {
    STAGING: ['key', 'event_json', 'exported_at'],
    INBOX: ['key', 'received_at', 'event_json'],
    OUTBOX: ['key', 'kind', 'source_id', 'mode', 'text', 'expires_at', 'status',
             'telegram_message_id', 'updated_at'],
}


def now():
    return datetime.now(timezone.utc)


def ensure(store, tab):
    if tab not in store.titles():
        store._invalidate()
        store.client.batchUpdate(spreadsheetId=store.id, body={'requests':[
            {'addSheet': {'properties': {'title': tab, 'hidden': True}}}]}).execute()
        store.update(tab, 1, HEADERS[tab])
    rows = store.data(tab, limit=None)
    if not rows or rows[0] != HEADERS[tab]:
        raise ValueError('Bridge table schema mismatch')
    return rows


def find(rows, key):
    return next(((n, row) for n, row in enumerate(rows[1:], 2) if row and row[0] == key), (None, None))


def event(update, owner_id):
    msg = update.get('message') or {}
    if (msg.get('from', {}).get('id') != owner_id or
            msg.get('chat', {}).get('id') != owner_id or msg.get('chat', {}).get('type') != 'private'):
        return None
    attachments = []
    for kind in ('document', 'voice', 'photo'):
        item = msg.get(kind)
        if kind == 'photo' and item: item = item[-1]
        if item:
            attachments.append({'type': kind, **{k: item[k] for k in
                ('file_id', 'file_unique_id', 'file_name', 'mime_type', 'file_size') if k in item}})
    return {'key': f'telegram:{owner_id}:{update["update_id"]}',
            'update_id': update['update_id'], 'message_id': msg.get('message_id'),
            'telegram_date': msg.get('date'), 'chat_id': str(owner_id), 'user_id': str(owner_id),
            'text': msg.get('text') or msg.get('caption') or '', 'attachments': attachments,
            'media_group_id': msg.get('media_group_id'), 'reply_to_message_id':
                (msg.get('reply_to_message') or {}).get('message_id'),
            'received_at': now().isoformat(), 'source': 'telegram', 'status': 'new'}


class Notice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = Field(pattern=r'^(morning|mail)$')
    source_id: str = Field(min_length=1, max_length=160)
    mode: str = Field(default='shadow', pattern=r'^(shadow|test|live)$')
    text: str = Field(min_length=1, max_length=3000)
    expires_at: datetime


class WorkflowBridge:
    def __init__(self, app, master):
        self.a, self.master = app, master
        self.owner = app.settings.codex_owner_id
        self.secret = app.settings.codex_bridge_secret
        if self.owner not in app.owners or len(self.secret) < 32:
            raise ValueError('Configure CODEX_OWNER_ID and a separate CODEX_BRIDGE_SECRET (32+ chars)')
        if secrets.compare_digest(self.secret, app.settings.telegram_webhook_secret):
            raise ValueError('Bridge secret must differ from Telegram webhook secret')

    def authorize(self, secret):
        if not secret or not secrets.compare_digest(secret, self.secret):
            raise HTTPException(403, 'Forbidden')

    async def capture(self, update):
        item = event(update, self.owner)
        if item is None: return
        def stage():
            rows = ensure(self.a.store, STAGING)
            if find(rows, item['key'])[1] is None:
                self.a.store.append(STAGING, [item['key'], json.dumps(item, ensure_ascii=False), ''])
        await self.a.sheet(stage)

    async def sync(self):
        def export():
            staged = ensure(self.a.store, STAGING)
            target = ensure(self.master, INBOX)
            keys = {r[0] for r in target[1:] if r}
            count = 0
            for n, row in enumerate(staged[1:], 2):
                if len(row) < 2 or (len(row) > 2 and row[2]): continue
                item = json.loads(row[1])
                if row[0] not in keys:
                    self.master.append(INBOX, [row[0], item['received_at'], row[1]])
                    keys.add(row[0])
                # If this fails after append, the next sync detects the existing key.
                self.a.store.update(STAGING, n, [row[0], row[1], now().isoformat()])
                count += 1
                if count >= 100: break
            return {'exported': count}
        return await self.a.sheet(export)

    async def inbox(self, cursor, limit):
        def read():
            # Reading never creates tabs. Initial setup happens on explicit sync.
            rows = self.master.data(INBOX, limit=None)
            if not rows or rows[0] != HEADERS[INBOX]: raise ValueError('Run bridge sync first')
            if cursor > len(rows)-1: raise ValueError('Inbox cursor exceeds rows; do not delete/reorder inbox rows')
            events = [json.loads(r[2]) for r in rows[1+cursor:1+cursor+limit]]
            return {'events': events, 'next_cursor': cursor + len(events)}
        return await self.a.sheet(read)

    async def enqueue(self, notice):
        if notice.kind == 'morning':
            try:
                if date.fromisoformat(notice.source_id).isoformat() != notice.source_id: raise ValueError()
            except ValueError: raise ValueError('Morning source_id must be the Kyiv YYYY-MM-DD date')
        elif not re.fullmatch(r'[A-Za-z0-9_-]+', notice.source_id):
            raise ValueError('Mail source_id must be a Gmail message ID')
        expiry = notice.expires_at
        if expiry.tzinfo is None or not now() < expiry <= now() + timedelta(hours=24):
            raise ValueError('Expiry must have timezone and be within the next 24 hours')
        if notice.mode == 'live' and not self.a.settings.codex_live_delivery:
            raise ValueError('Live delivery is disabled')
        if notice.mode == 'test' and not self.a.settings.codex_test_delivery:
            raise ValueError('Test delivery is disabled')
        prefix = '📋 РРД' if notice.kind == 'morning' else '📬 Пошта'
        text = ('🧪 ТЕСТ · ' if notice.mode == 'test' else '') + prefix + '\n' + notice.text.strip()
        if not notice.text.strip(): raise ValueError('Empty notice')
        if len(text.encode('utf-16-le')) // 2 > 3900: raise ValueError('Notice must fit one Telegram message')
        key = hashlib.sha256(f'{self.owner}:{notice.mode}:{notice.kind}:{notice.source_id}'.encode()).hexdigest()
        def write():
            rows = ensure(self.master, OUTBOX)
            _, existing = find(rows, key)
            if existing:
                if existing[4] != text: raise ValueError('Same key has different content; no overwrite')
                return {'key': key, 'status': existing[6], 'duplicate': True}
            status = 'shadow' if notice.mode == 'shadow' else 'pending'
            self.master.append(OUTBOX, [key, notice.kind, notice.source_id, notice.mode, text,
                                       expiry.isoformat(), status, '', now().isoformat()])
            return {'key': key, 'status': status, 'duplicate': False}
        return await self.a.sheet(write)

    async def deliver(self, key):
        # Never auto-retry sending/uncertain: Telegram has no idempotency key.
        # Serialize across all bridge deliveries, plus use the durable status on restart.
        async with self.a.queues.hold(('codex-delivery',)):
            def claim():
                rows = ensure(self.master, OUTBOX)
                n, row = find(rows, key)
                if row is None: raise ValueError('Unknown notification')
                row = list(row)
                if row[6] != 'pending': return n, row, False
                permitted = ((row[3] == 'test' and self.a.settings.codex_test_delivery) or
                             (row[3] == 'live' and self.a.settings.codex_live_delivery))
                if not permitted: return n, row, False
                if datetime.fromisoformat(row[5]) <= now():
                    row[6] = 'expired'; row[8] = now().isoformat()
                    self.master.update(OUTBOX, n, row)
                    return n, row, False
                row[6] = 'sending'; row[8] = now().isoformat()
                self.master.update(OUTBOX, n, row)  # MUST succeed before network send.
                return n, row, True
            n, row, claimed = await self.a.sheet(claim)
            if not claimed: return {'key': key, 'status': row[6], 'sent': False}
            try:
                result = await self.a.bot_call('sendMessage', {'chat_id': self.owner, 'text': row[4],
                    'link_preview_options': {'is_disabled': True}})
                row[7] = str(result['result']['message_id']); row[6] = 'sent'
            except Exception:
                row[6] = 'uncertain'
            row[8] = now().isoformat()
            await self.a.sheet(self.master.update, OUTBOX, n, row)
            return {'key': key, 'status': row[6], 'sent': row[6] == 'sent'}


def install_routes(app_module):
    """Always return 404 when disabled; no bridge secrets/tokens in request URLs."""
    from fastapi import Header, Query
    a = app_module
    def bridge(secret):
        if a.workflow_bridge is None: raise HTTPException(404, 'Disabled')
        a.workflow_bridge.authorize(secret)
        return a.workflow_bridge

    @a.app.get('/workflow/inbox')
    async def inbox(cursor: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                    x_codex_bridge_secret: str | None = Header(default=None)):
        try: return await bridge(x_codex_bridge_secret).inbox(cursor, limit)
        except ValueError as exc: raise HTTPException(409, str(exc))

    @a.app.post('/workflow/sync')
    async def sync(x_codex_bridge_secret: str | None = Header(default=None)):
        return await bridge(x_codex_bridge_secret).sync()

    @a.app.post('/workflow/outbox')
    async def enqueue(notice: Notice, x_codex_bridge_secret: str | None = Header(default=None)):
        try: return await bridge(x_codex_bridge_secret).enqueue(notice)
        except ValueError as exc: raise HTTPException(409, str(exc))

    @a.app.post('/workflow/outbox/{key}/deliver')
    async def deliver(key: str, x_codex_bridge_secret: str | None = Header(default=None)):
        try: return await bridge(x_codex_bridge_secret).deliver(key)
        except ValueError as exc: raise HTTPException(409, str(exc))
