"""Real-model media tool eval with generated documents and fake Telegram only."""
import argparse,asyncio,json,time
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock,patch
from PIL import Image,ImageDraw,ImageFont
from openai import AsyncOpenAI
from agent import run_agent
from agent_fixture import Harness


def invoice_image():
    img=Image.new('RGB',(900,900),'white');d=ImageDraw.Draw(img);font=ImageFont.load_default(size=32)
    for y,line in enumerate(['INVOICE DEMO-005','Supplier: DEMO TOOLS','Date: 02 November 2026',
                             'Steel washers - 10 units','TOTAL: 30.00 UAH','UNPAID']):
        d.text((50,60+y*85),line,font=font,fill='black')
    out=BytesIO();img.save(out,format='JPEG');return out.getvalue()

async def main(config):
    import yaml
    cfg=yaml.safe_load(Path(config).read_text());model=cfg.get('OPENAI_MODEL','gpt-6.1-sol')
    c=AsyncOpenAI(api_key=cfg['OPENAI_API_KEY'],timeout=90,max_retries=1)
    results=[]
    for name,text,photo,confirm in [
        ('photo_invoice','Оформи цей рахунок із фото для проєкту Тестовий проєкт.',True,True),
        ('pdf_invoice','Це PDF рахунку DEMO-8 від Демо на 50 грн для проєкту Тестовий проєкт. Подай власнику на перевірку.',False,True),
        ('photo_to_pdf','Збери з цього фото PDF накладної для проєкту Тестовий проєкт і надішли мені.',True,False)]:
        h=Harness();start=time.monotonic();r=h.runtime(456)
        if photo:r.state['photos']=[{'file_id':'synthetic_photo','group':'','mime':'image/jpeg'}]
        else:r.state['documents']=[{'file_id':'synthetic_pdf','file_name':'demo.pdf','mime':'application/pdf'}]
        await r.save()
        answer=''
        try:
            with patch('agent_tools.telegram_file_bytes',AsyncMock(return_value=invoice_image())):
                answer=await run_agent(c,model,r,text)
                if confirm:
                    p=h.store.get_pending(456)
                    assert p,'No invoice draft'
                    result=await r.confirm(p['payload']['token'],True)
                    assert result.get('ok'),result
                    media=[(m,p,f) for m,p,f in h.sent if m in ('sendPhoto','sendDocument') and p['chat_id']==123]
                    assert len(media)==1,media
                    assert media[0][1]['caption']=='Катана\nТестовий проєкт\nШакін'
                    assert media[0][0]==('sendPhoto' if photo else 'sendDocument')
                else:
                    docs=[f for m,p,f in h.sent if m=='sendDocument' and p['chat_id']==456]
                    assert len(docs)==1 and docs[0]['document'][1].startswith(b'%PDF-'),'No real PDF bytes'
            status='PASS';error=''
        except Exception as exc:status='FAIL';error=type(exc).__name__+': '+str(exc)[:400]
        result={'case':name,'status':status,'seconds':round(time.monotonic()-start,2),'answer':answer,'error':error,
                'calls':[{'tool':t['tool'],'arguments':t.get('arguments'),'result':t['result']} for turn in h.store.get_dialogue(456)['turns'] for t in turn['tools']]}
        results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True);h.close()
    report={'model':model,'passed':sum(r['status']=='PASS' for r in results),'cases':len(results),'results':results}
    Path('/tmp/shuher-agent-media-eval.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('SUMMARY',report['passed'],'/',report['cases'],flush=True);await c.close()
    if report['passed']!=report['cases']:raise SystemExit(1)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args();asyncio.run(main(a.config))
