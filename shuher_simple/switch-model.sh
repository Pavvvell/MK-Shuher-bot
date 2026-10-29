#!/bin/sh
# Upgrade Shuher's private Cloud Run environment to GPT-6 Astra.
# Do not print, upload or commit deploy.env.yaml (contains secrets).
set -eu
cd "$(dirname "$0")"
if [ ! -f deploy.env.yaml ]; then
  echo "deploy.env.yaml is missing. Nothing has been changed."
  exit 1
fi
python3 - <<'PY'
import os
import re
from pathlib import Path

path=Path("deploy.env.yaml")
raw=path.read_text(encoding="utf-8")
target="gpt-6-astra"
matches=list(re.finditer(r"(?m)^OPENAI_MODEL:[ \t]*[^\r\n]*$",raw))
if len(matches)>1:
    raise SystemExit("Duplicate OPENAI_MODEL entries; no changes applied.")
if matches:
    current=matches[0].group(0)
    new_raw=raw[:matches[0].start()]+'OPENAI_MODEL: "'+target+'"'+raw[matches[0].end():]
else:
    new_raw=raw.rstrip("\n")+'\nOPENAI_MODEL: "'+target+'"\n'
backup=Path.home()/".shuher-deploy-env-before-astra.yaml"
if raw!=new_raw:
    if not backup.exists():
        backup.write_text(raw,encoding="utf-8")
        os.chmod(backup,0o600)
    path.write_text(new_raw,encoding="utf-8")
    os.chmod(path,0o600)
print("OPENAI_MODEL = "+target)
print("Other env vars and credentials have been preserved.")
PY
sh deploy.sh
