#!/usr/bin/env python3
"""Opt-in client; no Telegram token, Gmail changes, scheduler changes, or webhook calls."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Bridge redirects are forbidden')


def request(path, body=None):
    base=os.environ.get('CODEX_BRIDGE_URL','').rstrip('/')
    secret=os.environ.get('CODEX_BRIDGE_SECRET','')
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Set CODEX_BRIDGE_URL to the HTTPS service URL')
    if len(secret)<32:raise ValueError('Set CODEX_BRIDGE_SECRET (32+ chars)')
    req=urllib.request.Request(base+path, data=None if body is None else json.dumps(body).encode(),
        headers={'X-Codex-Bridge-Secret':secret,'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Bridge HTTP {exc.code}; no local cursor advanced') from None
    except urllib.error.URLError:
        raise RuntimeError('Bridge unavailable; no local cursor advanced') from None


def atomic_write(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=path.name+'.')
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def pull(output,cursor_file):
    output,cursor_file=Path(output),Path(cursor_file)
    if output.resolve()==cursor_file.resolve():raise ValueError('Output and cursor must differ')
    cursor_file.parent.mkdir(parents=True,exist_ok=True)
    with open(str(cursor_file)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        cursor=int(cursor_file.read_text()) if cursor_file.exists() else 0
        reply=request(f'/workflow/inbox?cursor={cursor}&limit=100')
        events=reply['events'];next_cursor=reply['next_cursor']
        if next_cursor!=cursor+len(events):raise ValueError('Invalid inbox cursor')
        existing=[json.loads(x) for x in output.read_text().splitlines() if x] if output.exists() else []
        by_key={x['key']:x for x in existing}
        for item in events:by_key[item['key']]=item
        atomic_write(output,''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in by_key.values()))
        # Data first: a crash before cursor write only replays deduplicated event keys.
        atomic_write(cursor_file,str(next_cursor))
        return {'received':len(events),'next_cursor':next_cursor}


def notice_from_result(result,source_id,mode,expiry):
    if not result.get('notify') or result.get('status') in ('error','no_action'):
        return None
    kind=result['job']
    if kind not in ('morning','mail'):raise ValueError('Unsupported job')
    if kind=='mail' and result.get('processed_message_ids')!=[source_id]:
        raise ValueError('Mail requires one notice per Gmail message ID; split multi-mail summaries first')
    return {'kind':kind,'source_id':source_id,'mode':mode,
            'text':result['title']+'\n'+result['summary'],'expires_at':expiry}


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('sync')
    s=sub.add_parser('pull');s.add_argument('--output',required=True);s.add_argument('--cursor-file',required=True)
    s=sub.add_parser('enqueue');s.add_argument('--result',required=True);s.add_argument('--source-id',required=True)
    s.add_argument('--expires-at',required=True);s.add_argument('--mode',choices=['shadow','test','live'],default='shadow')
    s=sub.add_parser('deliver');s.add_argument('--key',required=True)
    a=p.parse_args()
    if a.action=='sync':result=request('/workflow/sync',{})
    elif a.action=='pull':result=pull(a.output,a.cursor_file)
    elif a.action=='enqueue':
        payload=notice_from_result(json.loads(Path(a.result).read_text()),a.source_id,a.mode,a.expires_at)
        result=request('/workflow/outbox',payload) if payload else {'skipped':True}
    else:
        if len(a.key)!=64 or any(x not in '0123456789abcdef' for x in a.key):raise ValueError('Invalid delivery key')
        result=request('/workflow/outbox/'+a.key+'/deliver',{})
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,KeyError,OSError) as exc:
        raise SystemExit(str(exc)) from None
