# Codex ↔ Шухер: isolated implementation and rollout review

Status: implemented on `feature/codex-workflow-bridge`; **not deployed**.
All new features default OFF. No existing automation, launchd job, Gmail data,
Master data, bot webhook, production environment or production branch was changed.

## Verified baseline

- Repository: https://github.com/Pavvvell/MK-Shuher-bot
- Production source branch: `feature/simple-sheets-bot`, baseline commit `cdb63bc2641e180a3b88fc012efb3de093531eb3`.
- GCP: project `mk-shuher-bot`, service `shuher-lite`, region `europe-west3`.
- Last observed deployed revision: `shuher-lite-00038-zdx` (previous deployment).
- Live read-only `/health` check during this audit: status `ok`, Sheets backend,
  `gpt-6.1-sol`, per-user queues, parallel_requests 4, model-first-tools.
- Service URL: https://shuher-lite-qydspxcqya-ey.a.run.app
- Runtime service account configured in deploy script: `shuher-sheets@mk-shuher-bot.iam.gserviceaccount.com`.
- `POST /telegram/webhook` authenticates `X-Telegram-Bot-Api-Secret-Token`,
  deduplicates update_id, uses per-user locks, dispatches to `handle_message`
  or `handle_callback`, then records processed update.
- Only Sheets API is implemented for Google data. Credentials: optional
  `GOOGLE_SERVICE_ACCOUNT_JSON_B64`, otherwise Application Default Credentials.
  Existing variables: `SPREADSHEET_ID`, optional `HISTORY_SPREADSHEET_ID`,
  `OWNER_IDS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `OPENAI_API_KEY`,
  `OPENAI_MODEL`, `REGISTER_TELEGRAM_WEBHOOK`, `PUBLIC_BASE_URL`.
- Files remain Telegram file IDs; bot downloads them server-side. Dialogue is
  bounded and unsuitable as a permanent inbox. Existing metadata tabs:
  `_Shuher_Users`, `_Shuher_Access`, `_Shuher_Pending`, `_Shuher_Updates`, `_Shuher_Dialog`.
- No Gmail sender/reader, Drive file store, Firebase, or distributed queue in bot.
- Current live webhook URL and service-account access to the new Master have NOT
  been independently queried during this audit. Verify before any rollout.

## Smallest compatible scheme

1. Keep the existing webhook and bot token. After private-user authorization,
   before session reset/media/model/business work, stage **only Pavlo's** raw
   message/caption + attachment IDs in `_Shuher_Codex_Staging` in the existing bot
   spreadsheet. A unique `telegram:<owner_id>:<update_id>` key prevents replay.
2. An explicit `POST /workflow/sync` exports staged events to
   `_Shuher_Codex_Inbox` in Master
   `1P3v-A5Obzfwbw1CpunZEztKJ_tYORZOr5KSrQXvWias`.
   Master outages cannot block the original bot handler. Staging failures in the
   existing Sheets dependency return HTTP 503 **before** model/business actions,
   allowing Telegram retries. Sync retries safely after append/ack interruptions.
3. Codex reads `GET /workflow/inbox?cursor=0&limit=100`; no Telegram polling.
   Each returned event has a stable key, raw text and media metadata. Voice remains
   an attachment ID here; the existing bot's transcription is not a permanent
   mirrored transcript. Edits are not subscribed by the existing webhook and are
   not added by this change. Ordinary messages, captions, PDFs and photos are covered.
4. Existing Codex Gmail access reads `metal.unit.llc@gmail.com`; no additional Gmail
   credentials or permissions are introduced into the bot. Codex generates РРД from
   the live Master and mail notifications from Gmail. No automatic changes to either.
5. Codex posts a prepared notice to `/workflow/outbox`. Master
   `_Shuher_Codex_Outbox` is both outbox and delivery ledger. Body:
   `kind` morning/mail, `source_id`, `mode` shadow/test/live, `text`, `expires_at`.
   No arbitrary recipient field. Recipient is fixed by the owner's configured ID.
   Telegram text always starts with `📋 РРД` or `📬 Пошта`; test notices additionally
   start with `🧪 ТЕСТ`. All content is plain text, under one Telegram message.
6. Explicit `POST /workflow/outbox/<key>/deliver` sends only eligible records.
   There is no new scheduler or background fire-and-forget worker.

The API uses a distinct `X-Codex-Bridge-Secret` header, never a query parameter.
All bridge endpoints are 404 while disabled. The client never needs the bot token.

## Configuration (names only; do not commit real secrets)

- `CODEX_BRIDGE_ENABLED=false` — default, no bridge stores or calls.
- `CODEX_MASTER_SPREADSHEET_ID` — defaults to the user-specified Master above.
- `CODEX_OWNER_ID` — explicit numeric Pavlo ID; must already be in `OWNER_IDS`.
  Do not infer it from a name or pick the first owner.
- `CODEX_BRIDGE_SECRET` — separate 32+ character secret, different from webhook secret.
- `CODEX_TEST_DELIVERY=false` — test delivery gate.
- `CODEX_LIVE_DELIVERY=false` — live delivery gate, stays false until approved cutover.
- Local client only: `CODEX_BRIDGE_URL`, `CODEX_BRIDGE_SECRET` via secure environment.

For an eventual bridge deployment, explicitly keep `REGISTER_TELEGRAM_WEBHOOK=false`
so startup does not call setWebhook at all. Do not use deleteWebhook, setWebhook or
getUpdates during integration. Preserve all existing service settings and token.
This design depends on existing **one instance / one Uvicorn worker** deployment;
it is not a distributed lock or a transaction across two spreadsheets.

## Idempotency and failure behavior

- Inbox keys are immutable. Do not delete/reorder inbox/staging rows; cursor is append order.
- Outbox key is SHA-256 of fixed owner + mode + kind + source ID. Morning source:
  canonical Kyiv `YYYY-MM-DD`. Mail source: the exact Gmail message ID (one notice
  per email, not a time-window digest). Repeated key/content returns original row;
  different content under the same key is rejected rather than overwritten.
- `shadow` records never send, even if delivery flags later change. Test keys and
  live keys are separate; an old shadow backlog is never promoted automatically.
- Durable `pending → sending` is written before Telegram. Then `sent` + Telegram
  message ID is saved. Concurrent requests and restarts do not repeat `sent`.
- Telegram timeouts/errors become `uncertain`; failure to save its receipt leaves
  `sending`. Neither is automatically retried. This prioritizes avoiding duplicates
  over guaranteed automatic delivery. Telegram does not support idempotency keys:
  **exactly-once delivery cannot be guaranteed**. Reconcile uncertain records with
  the actual chat before any operator action; never simply reset them to pending.
- Notices expire within 24 hours. No stale catch-up flood.
- Only bridge code should write delivery status/key columns. A privileged editor
  changing ledger rows can invalidate guarantees. Do not sort/delete these rows.
- Existing ChatGPT Automations have independent delivery state. Leave them active
  as requested; keep the new live channel OFF. Cross-system duplicate suppression
  is not implemented. Final cutover requires a separate explicit decision.

## Local adapter, without changing running jobs

`tools/codex_bridge_client.py` uses only Python standard library. Commands:

```text
python tools/codex_bridge_client.py sync
python tools/codex_bridge_client.py pull --output <test-inbox.jsonl> --cursor-file <test-cursor>
python tools/codex_bridge_client.py enqueue --result <result.json> --source-id <date-or-Gmail-ID> --expires-at <ISO-time-with-zone>
python tools/codex_bridge_client.py deliver --key <returned-key>
```

Enqueue defaults to shadow. `--mode test` and `--mode live` still require the server
flag. The client supports the current local `result.schema.json` shape. It skips
notify=false/error/no_action. Mail summaries containing multiple message IDs are
rejected: producer must split them into per-email results, retaining their IDs.
Pull writes data atomically before advancing cursor; replay merges by event key.
This JSONL is a message cache, not a second task-state database.

**Do not run the old `codex-workflow/bin/telegram-sync.sh` against Shuher**: it uses
getUpdates. Old `send-telegram.sh` also bypasses the delivery ledger. Neither file
nor their launchd configuration was changed. Replace/integrate these only after
review; keep current jobs shadow. Do not count the adapter alone as a completed
production cutover or an installed scheduled integration.

## Exact end-to-end acceptance plan — no production action performed yet

1. Record `getWebhookInfo` privately (print only URL comparison and counts), current
   revision/traffic and `/health`; confirm Pavlo ID, existing Master sharing and
   service-account Sheets access. Record the two old automations active and both
   local jobs in shadow. Do not modify them.
2. On an isolated staging service and spreadsheet copies, use a separate **test bot**.
   Set bridge enabled, live=false, test=false, register_webhook=false. Exercise the
   new routes with recorded synthetic updates; verify original bot commands,
   invoices, project buttons and staff tasks against synthetic storage.
3. Send a synthetic private owner message, caption/photo and PDF through test bot
   input; stage, sync and pull. Each appears exactly once in the shared inbox and
   the Codex runtime context. Worker/group messages must not appear. Replay the
   same update; no duplicate. /new must not erase inbox history.
4. Simulate Master downtime: original bot responds, staged event stays; restored
   sync produces one row. Interrupt after Master append before export ack; repeat
   sync still gives one row. Interrupt local pull before cursor save; no duplicate.
5. Generate a РРД from staging Master and one mail result from a synthetic message;
   enqueue in shadow. Check headers, dates/IDs, text and provenance. Request
   delivery: **zero Telegram sends**. Live request must be rejected.
6. Enable **only test delivery** on staging. Send one labeled РРД and one labeled
   mail notice to the agreed test recipient. Retry enqueue/deliver concurrently
   and after restart: exactly one test message per key, with saved Telegram IDs.
   Simulate response loss: uncertain/sending is blocked, never blindly retried.
   Check expiry, bad secret, invalid source and conflicting payload cases.
7. Present staging evidence and exact production configuration diff for separate
   user approval. Before that approval: no production deployment, Master setup,
   secret provisioning, live toggle, automation edits, or job rewiring.
8. After explicit approval, deploy with live=false and webhook registration=false;
   compare original webhook URL unchanged and original bot checks passed. Use a
   real owner test message through the existing webhook and verify Master→Codex
   end-to-end; then two labeled test notices with test-only gate and repeated keys.
9. Keep local jobs shadow and old automations active until the user reviews this
   real test. Only a further approved cutover may enable live scheduled delivery and
   coordinate old channels. Rollback: disable bridge/return previous revision;
   preserve inbox/ledger, never reset webhook or replay uncertain deliveries.

## Offline verification

Run from `shuher_simple`: `python -m unittest discover -s tests -q`.
Tests cover original bot suite plus real ASGI webhook→staging→sync→inbox→outbox→
mock Telegram roundtrip, authorization/gates, concurrent delivery, restart,
ambiguous response, lost receipt, expiry, sync replay, and client crash recovery.
This is an offline integration test, **not** a real Google/Telegram/Gmail E2E pass.
