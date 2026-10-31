"""Real OpenAI timings for synthetic prompts captured by simulate.py.
Run only in Cloud Shell beside existing deploy.env.yaml; secrets never printed.
Does not import app, write Sheets, or send Telegram messages.
"""
import asyncio, hashlib, json, sys, time
from pathlib import Path
import yaml
from openai import AsyncOpenAI
sys.path.insert(0,sys.argv[1])
from llm import response_input

ROOT=Path(__file__).resolve().parent
async def main():
    cfg=yaml.safe_load(open('deploy.env.yaml'))
    model=cfg.get('OPENAI_MODEL','gpt-6-astra')
    client=AsyncOpenAI(api_key=cfg['OPENAI_API_KEY'],timeout=90,max_retries=1)
    prompts=json.loads((ROOT/'prompts.json').read_text())
    unique={}
    for p in prompts:
        key=hashlib.sha256(json.dumps(p['messages'],sort_keys=True).encode()).hexdigest()
        if key not in unique:unique[key]=p
    out=[]
    async def run(p,rep,effort=None,variant='baseline',use_model=None):
        kw=dict(model=use_model or model,input=response_input(p['messages']),reasoning={'effort':effort or ('low' if p['json_mode'] else 'medium')},max_output_tokens=max(1800 if p['json_mode'] else 2600,p['max_tokens']*5),store=False)
        if p['json_mode']:kw['text']={'format':{'type':'json_object'}}
        t=time.perf_counter();row={'case':p['case'],'variant':variant,'rep':rep,'json':p['json_mode'],'model':kw['model'],'effort':kw['reasoning']['effort']}
        try:
            r=await client.responses.create(**kw)
            row.update(s=round(time.perf_counter()-t,4),status=r.status,tier=r.service_tier,usage=r.usage.model_dump() if r.usage else {},answer=r.output_text)
        except Exception as ex:
            row.update(s=round(time.perf_counter()-t,4),error=type(ex).__name__,http_status=getattr(ex,'status_code',None))
        out.append(row);(ROOT/'ai-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in row.items() if k not in ('answer','usage')},ensure_ascii=False),flush=True)
    for rep in range(3):
        for p in unique.values():await run(p,rep)
    chat=next(p for p in prompts if p['case']=='chat' and not p['json_mode'])
    for rep in range(3):
        await run(chat,rep,'low','astra-low')
        await run(chat,rep,'low','sol-low','gpt-6.1-sol')
    await client.close()
    print('COMPLETE',len(out),'UNIQUE_PROMPTS',len(unique),flush=True)
asyncio.run(main())
