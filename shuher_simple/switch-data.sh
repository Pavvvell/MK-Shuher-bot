#!/bin/sh
# One-time, secrets-safe migration from old Shuher Lite to new data workbook.
set -eu
cd "$(dirname "$0")"
if [ ! -f deploy.env.yaml ]; then
  echo "Missing private deploy.env.yaml. Migration canceled."
  exit 1
fi

python3 - <<'PY'
import os
import re
from pathlib import Path

path=Path("deploy.env.yaml")
raw=path.read_text(encoding="utf-8")
previous="1ubvlvl4zzjMwsS6CCHfe8kym7F3p2ITLKSqm1dGkr4s"
current="1krTe5d89Ke6tyl3mHjyBltf33QrXgB3zdsjfvOo8g_U"
history="1ZSmctpTVh5NsCa4pW0acpo6CZGrddhOj9xjmTlsB03c"
match=re.search(r'(?m)^SPREADSHEET_ID:[ \t]*(.*?)\s*$',raw)
if not match:
    raise SystemExit("SPREADSHEET_ID not found; no changes applied.")
existing=match.group(1).strip().strip("\"'")
if existing not in (previous,current):
    raise SystemExit("Unknown SPREADSHEET_ID: do not switch the wrong system.")
hist=re.search(r'(?m)^HISTORY_SPREADSHEET_ID:[ \t]*(.*?)\s*$',raw)
if hist:
    old_hist=hist.group(1).strip().strip("\"'")
    if old_hist not in ("",history):
        raise SystemExit("Unexpected archive ID; no changes applied.")
backup=Path.home()/".shuher-deploy-env-before-switch.yaml"
if not backup.exists():
    backup.write_text(raw,encoding="utf-8")
    os.chmod(backup,0o600)
updated=raw[:match.start(1)]+'"'+current+'"'+raw[match.end(1):]
if hist:
    updated=re.sub(r'(?m)^HISTORY_SPREADSHEET_ID:.*$',
                   'HISTORY_SPREADSHEET_ID: "'+history+'"',updated,count=1)
else:
    updated=updated.rstrip("\n")+"\nHISTORY_SPREADSHEET_ID: \""+history+"\"\n"
path.write_text(updated,encoding="utf-8")
os.chmod(path,0o600)
print("Configured new active workbook and read-only history source.")
print("Private credentials preserved; backup stored in the home directory.")
PY

sh deploy.sh
