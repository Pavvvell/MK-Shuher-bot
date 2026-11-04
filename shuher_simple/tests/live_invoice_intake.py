"""Real model + synthetic files/Sheets/Telegram; no real invoices or recipients."""
import argparse
import asyncio
import json
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from openai import AsyncOpenAI
from agent import run_agent
from agent_fixture import Harness
from invoice_intake import InvoiceIntake


def document(kind='INVOICE', total=True, pdf=False):
    image=Image.new('RGB',(1000,1000),'white');draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=34)
    lines=[kind+' DEMO-42','Supplier: DEMO TOOLS','Date: 04 November 2026','Steel washers - 10 units']
    if total:lines+=['TOTAL: 3449.88 UAH']
    lines+=['UNPAID']
    for i,line in enumerate(lines):draw.text((50,60+i*95),line,font=font,fill='black')
    output=BytesIO();image.save(output,format='PDF' if pdf else 'JPEG');return output.getvalue()


async def main(config):
    import yaml
    cfg=yaml.safe_load(Path(config).read_text());model=cfg.get('OPENAI_MODEL','gpt-6.1-sol')
    c=AsyncOpenAI(api_key=cfg['OPENAI_API_KEY'],timeout=90,max_retries=1)
    results=[]
    for name, files, text in [
        ('scanned_pdf',{'pdf1':document(pdf=True)},'Надсилаю PDF. Прочитай документ.'),
        ('unknown_price',{'photo1':document(total=False)},'На оплату'),
        ('order_document',{'photo1':document(kind='CUSTOMER ORDER')},'На оплату'),
        ('mixed_batch',{'pdf1':document(pdf=True),'photo2':document(total=False)},'Ось пачка рахунків на оплату'),
    ]:
        h=Harness();r=h.runtime(456);start=time.monotonic();error=''
        h.store.client.data['Проєкти'].append(['P2','','Інший проєкт','Активний',''])
        r.state['photos']=[{'file_id':fid,'mime':'image/jpeg'} for fid in files if fid.startswith('photo')]
        r.state['documents']=[{'file_id':fid,'file_name':'test.pdf','mime':'application/pdf'} for fid in files if fid.startswith('pdf')]
        r.incoming_ids=list(files);await r.save()
        async def fetch(bot,token,fid,limit):return files[fid]
        try:
            with patch('agent_tools.telegram_file_bytes',fetch):
                answer=await run_agent(c,model,r,text)
            cards=[json.loads(row[2]) for row in h.store.data('_Shuher_Pending')[1:]
                   if row[0].startswith('invoice:456:')]
            assert len(cards)==len(files),f'Expected {len(files)} project cards, got {len(cards)}; answer={answer}'
            assert answer is None,answer
            assert not h.texts(456),'Worker received extra message: '+h.texts(456)
            assert not h.store.get_pending(456),'Unwanted second confirmation'
            if name=='scanned_pdf':assert cards[0]['amount']=='3449.88',cards[0]['amount']
            if name=='unknown_price':assert not cards[0]['amount'],cards[0]['amount']
            if name=='order_document':assert cards[0]['document_type']=='Замовлення',cards[0]['document_type']
            flow=InvoiceIntake(h.runtime(456))
            for i,card in enumerate(cards):
                cb={'id':'cb'+str(i),'data':f'inv:{card["token"]}:p:{i%2}',
                    'from':{'id':456},'message':{'message_id':card['message_id'],'chat':{'id':456,'type':'private'}}}
                await h.app.handle_callback({'update_id':100+i,'callback_query':cb})
                assert (await flow.load(card['token']))['complete']
            delivered=[p for m,p,f in h.sent if m in ('sendPhoto','sendDocument') and p['chat_id']==123]
            assert len(delivered)==len(files),len(delivered)
            if name in ('unknown_price','mixed_batch'):
                assert any(m=='sendMessage' and p['chat_id']==123 and 'сум' in p['text'].lower() for m,p,f in h.sent)
            status='PASS'
        except Exception as exc:status='FAIL';error=type(exc).__name__+': '+str(exc)[:1000]
        turns=h.store.get_dialogue(456)['turns']
        result={'case':name,'status':status,'seconds':round(time.monotonic()-start,2),'error':error,
                'tools':[x['tool'] for turn in turns for x in turn['tools']]}
        results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True);h.close()
    await c.close()
    Path('/tmp/shuher-invoice-live.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print('SUMMARY',sum(x['status']=='PASS' for x in results),'/',len(results),flush=True)
    if any(x['status']!='PASS' for x in results):raise SystemExit(1)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args();asyncio.run(main(a.config))
