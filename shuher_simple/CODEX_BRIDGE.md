# Codex ↔ Шухер

The existing webhook remains the only Telegram receiver. No polling or webhook
registration is introduced. Ordinary bot features still use the model-first tool loop.

## Data flow

Owner messages are durably staged in the bot spreadsheet before handling. The sync
endpoint exports deduplicated events to Master inbox. The local adapter pulls them
into JSONL using an atomic cursor. Capture itself records input. In the owner's
private conversation the model can apply explicit work updates directly through
workflow_update_master, without a second confirmation card.

The edit tool requires a current-turn read, validates live dropdowns, rejects
stale snapshots, and writes only allowed fields in Tasks/Projects/Recurring.
Closing a record clears its active control dates and waiting fields. Related
project changes can be included in the same batch. Changes and an idempotency
receipt commit atomically in Master/Journal, followed by a fresh readback.
Retries of the same operation inspect the receipt instead of replaying the write.
This relies on the existing single Sheets worker; Google Sheets does not provide
a conditional-write transaction against concurrent edits by external editors.

Morning dispatch reads the resulting Master state rather than replaying raw inbox
history. Closed and paused work, superseded dates and stale-update diagnostics do
not belong in the daily notice. The local prompt limits it to five short items.

Morning and mail producers read connected Google sources and generate typed notices.
The bridge stores notices in Master outbox and sends them to the configured owner.
One key per owner/mode/kind/source prevents duplicate delivery. Daily notices use the
Kyiv date; mail notices use the Gmail message ID. Distinct Gmail messages in one
thread remain distinct notices.

The model can read recent notices, current Master rows, and retained mail context.
Mail search ranks normalized terms and falls back to recent notices rather than
falsely claiming that no mail exists. Explicit Telegram replies take priority.
The reply tool prepares a Gmail compose button using the source email address,
subject and model-written body. It does not send an email or claim a draft was saved.
The compose URL creates a new message with a reply subject; in-thread reply is not
guaranteed. The user presses Send in Gmail.

## Configuration

- CODEX_BRIDGE_ENABLED: enable bridge; default false.
- CODEX_MASTER_SPREADSHEET_ID: target Master spreadsheet.
- CODEX_OWNER_ID: a member of the existing bot owner list.
- CODEX_BRIDGE_SECRET: separate shared bridge credential, minimum 32 characters.
- CODEX_TEST_DELIVERY / CODEX_LIVE_DELIVERY: independent delivery gates.
- REGISTER_TELEGRAM_WEBHOOK: keep false for an existing webhook installation.

Secrets belong in private deployment configuration/Keychain, never in the repository.
The runtime service account requires access to the target Master. Grant that only
after the owner has approved the specific permission.

## API and delivery semantics

All workflow routes require X-Codex-Bridge-Secret and return 404 when disabled.
POST /workflow/sync exports staging. GET /workflow/inbox uses an append-only cursor.
POST /workflow/outbox enqueues; POST /workflow/outbox/{key}/deliver sends explicitly.
Shadow notices never send. Test/live notices require their matching gate and expiry.

The ledger changes pending → sending before contacting Telegram, then records sent
and the Telegram receipt. An ambiguous timeout becomes uncertain and is not retried
automatically. A lost receipt write leaves sending, also without automatic retry.
This favors preventing duplicate messages over guaranteed delivery in ambiguous cases.
Inspect the ledger and Telegram before manually resolving an uncertain delivery.
Do not delete/reorder inbox rows. Keep the single-instance, single-worker deployment
assumption while using in-process coordination with Google Sheets.

## Verification and local schedules

The regression suite covers capture, permissions, mode gates, duplicate attempts,
ambiguous delivery, cursor recovery, reply context, mail search and compose encoding.
Master edit tests cover stale reads, validation, permission boundaries, lost commit
acknowledgments, replay after a newer edit, and atomic failure. For a real-model
evaluation with synthetic storage run tests/live_master.py with private config;
it never writes production Sheets or sends Telegram messages.
Production checks additionally require observing Telegram delivery, a real user
message reaching Master/local inbox, and the model preparing the correct email reply.
Verify Gmail fields without pressing Send. Test repeat delivery before enabling live.

Local workflow uses existing morning/mail launchd jobs plus a periodic sync job.
It requires an awake Mac and network; Cloud Run itself does not run the local Codex
producers. Preserve old ChatGPT Automations unless the user explicitly disables them.
See local codex-workflow README for operator setup and schedule details.
Deployment work for this integration uses feature/codex-workflow-bridge; do not
assume other repository branches have the same code. Operational test records,
account identifiers, real email context and delivery receipts stay outside this doc.
