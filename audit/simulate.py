"""Audit production handlers with synthetic data. No real network or credentials.
Usage: python simulate.py ../shuher-work/shuher_simple
All Sheets SDK operations, Telegram messages, model calls and downloads are traced.
Local elapsed time is CPU/mock time, NOT end-to-end production latency.
"""
import asyncio, base64, copy, importlib, io, json, os, re, sys, time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(Path(sys.argv[1]).resolve()))
import store as sm
from dialogue import new_dialogue,queue_photo,queue_document
from PIL import Image,ImageDraw

EVENTS=[]; PROMPTS=[]; ANSWERS=[]; CASE=''
class Req:
    def __init__(self,fn):self.fn=fn
    def execute(self):return self.fn()
class API:
    def __init__(self,data):self.data=copy.deepcopy(data)
    def values(self):return self
    def loc(self,ran):
        tab,_,coords=ran.partition('!');tab=tab.strip("'").replace("''", "'")
        m=re.match(r'([A-Z]+)(\d*)',coords)
        col=0
        for c in m.group(1) if m else 'A':col=col*26+ord(c)-64
        return tab,int(m.group(2) or 1)-1 if m else 0,col-1
    def read(self,ran):
        tab,row,col=self.loc(ran)
        return [r[col:] for r in self.data.get(tab,[])[row:]]
    def get(self,**kw):
        def go():
            EVENTS.append({'stage':'sheets','op':'get' if 'range' in kw else 'metadata','range':kw.get('range','titles')})
            if 'range' in kw:return {'values':self.read(kw['range'])}
            return {'sheets':[{'properties':{'title':t}} for t in self.data]}
        return Req(go)
    def batchGet(self,**kw):
        def go():
            EVENTS.append({'stage':'sheets','op':'batchGet','range':kw['ranges']})
            return {'valueRanges':[{'values':self.read(r)} for r in kw['ranges']]}
        return Req(go)
    def update(self,**kw):
        def go():
            EVENTS.append({'stage':'sheets','op':'update','range':kw['range']})
            tab,row,col=self.loc(kw['range']);rows=self.data.setdefault(tab,[])
            for n,vals in enumerate(kw['body']['values']):
                while len(rows)<=row+n:rows.append([])
                while len(rows[row+n])<col+len(vals):rows[row+n].append('')
                rows[row+n][col:col+len(vals)]=vals
            return {'updatedRows':len(kw['body']['values'])}
        return Req(go)
    def append(self,**kw):
        def go():
            EVENTS.append({'stage':'sheets','op':'append','range':kw['range']})
            tab,_,_=self.loc(kw['range']);self.data.setdefault(tab,[]).extend(copy.deepcopy(kw['body']['values']))
            return {'updates':{'updatedRows':len(kw['body']['values'])}}
        return Req(go)
    def batchUpdate(self,**kw):
        def go():
            EVENTS.append({'stage':'sheets','op':'batchUpdate','range':'batch'})
            for r in kw['body'].get('requests',[]):
                if 'addSheet' in r:self.data[r['addSheet']['properties']['title']]=[]
            for r in kw['body'].get('data',[]):self.update(**r).execute()
            return {}
        return Req(go)

def fixtures():
    d={k:[list(v)] for k,v in sm.META.items()}
    d[sm.USERS]+=[['123','owner','Тестовий власник','Заявки',''],['456','worker','Юрій','Заявки','']]
    tables={
      'Проєкти':['Назва проєкту','Статус'],
      'Запити на проєкти':['Назва проєкту','Причина','Статус','Погодив','Дата погодження'],
      'Заявки':['Найменування','Кількість','Од. вим.','Проєкт','Статус','Потрібен пошук','Тип потреби'],
      'Закупка':['ID заявки','Найменування','Кількість','Од. вим.','Проєкт','Статус замовлення','Статус оплати','Постачальник'],
      'Склад':['Тип операції','Найменування','Кількість','Од. вим.','Проєкт','ID закупівлі','ID доручення'],
      'Доручення':['Завдання','Виконавець','Проєкт','Статус','Термін','Пріоритет'],
      'Рахунки':['Проєкт','Постачальник','Номер рахунку','Сума','Валюта','Тип документа','Статус','Статус оплати','Telegram файли','Документ / посилання','Коментар','Потрібна перевірка'],
      'Накладні':['Проєкт','Документ / посилання'],
      'Правила Шухера':['Правило','Статус'],
      'Уточнення відомостей':['Найменування','Проєкт'],
      'Інвентаризація':['Найменування','Кількість']}
    for name,cols in tables.items():d[name]=[['ID','Дата']+cols+['Автор']]
    def add(tab,**vals):d[tab].append([vals.get(h,'') for h in d[tab][0]])
    add('Проєкти',ID='P1',**{'Назва проєкту':'Тестовий проєкт','Статус':'Активний'})
    add('Запити на проєкти',ID='PR1',**{'Назва проєкту':'Новий тест','Статус':'Очікує погодження'})
    add('Заявки',ID='R1',**{'Найменування':'болти М8','Кількість':'10','Од. вим.':'шт','Проєкт':'Тестовий проєкт','Статус':'Нова'})
    add('Закупка',ID='Z1',**{'Найменування':'болти М8','Кількість':'10','Од. вим.':'шт','Проєкт':'Тестовий проєкт','Статус замовлення':'Не замовлено','Статус оплати':'Не оплачено'})
    add('Склад',ID='S1',**{'Тип операції':'Надходження','Найменування':'болти М8','Кількість':'100','Од. вим.':'шт'})
    add('Доручення',ID='T1',**{'Завдання':'Перевірити болти','Виконавець':'Юрій','Проєкт':'Тестовий проєкт','Статус':'Нове'})
    for i in range(3):add('Рахунки',ID=f'I{i}',**{'Проєкт':'Тестовий проєкт','Постачальник':'Демо','Тип документа':'Рахунок','Telegram файли':f'demo_pdf{i}','Документ / посилання':'Фото Telegram: 0; PDF: 1','Статус':'Очікує перевірки'})
    d[sm.GRANTS]+=[['456',t,'write','123'] for t in ['Заявки','Закупка','Склад','Доручення','Рахунки','Накладні']]
    return d

def make_store():
    s=sm.SheetsStore.__new__(sm.SheetsStore);s.id='synthetic';s.client=API(fixtures());return s

im=Image.new('RGB',(1400,1900),'white');ImageDraw.Draw(im).text((100,100),'TEST INVOICE 001\n10 bolts x 20 = 200 UAH',fill='black',font_size=40)
buf=io.BytesIO();im.save(buf,format='JPEG');PHOTO=buf.getvalue()

async def fake_bot(method,payload=None,files=None):
    EVENTS.append({'stage':'telegram','op':method,'text':str((payload or {}).get('text',''))[:220]})
    return {'ok':True,'result':{'message_id':1,'file_path':'demo.jpg'}}
async def fake_download(*args,**kw):
    EVENTS.append({'stage':'download','op':'file','bytes':len(PHOTO)});return PHOTO
async def fake_voice(*args,**kw):
    EVENTS.append({'stage':'voice','op':'transcribe'});return 'покажи залишки'
async def fake_model(messages,max_tokens=500,json_mode=False):
    PROMPTS.append({'case':CASE,'messages':messages,'max_tokens':max_tokens,'json_mode':json_mode})
    EVENTS.append({'stage':'model','op':'json' if json_mode else 'text','chars':len(json.dumps(messages))})
    if ANSWERS:return json.dumps(ANSWERS.pop(0),ensure_ascii=False) if json_mode else str(ANSWERS.pop(0))
    return '{"intent":"other"}' if json_mode else 'Тестова відповідь на основі наданих даних.'

async def main():
    global CASE,ANSWERS
    env={'TELEGRAM_BOT_TOKEN':'test','TELEGRAM_WEBHOOK_SECRET':'test','OPENAI_API_KEY':'test','SPREADSHEET_ID':'test','OWNER_IDS':'123','HISTORY_SPREADSHEET_ID':''}
    with patch.dict(os.environ,env),patch('store.SheetsStore'),patch('openai.AsyncOpenAI'):
        a=importlib.import_module('app')
    a.bot_call=fake_bot;a.ask_model=fake_model;a.telegram_file_bytes=fake_download;a.transcribe_telegram_voice=fake_voice
    async def sender(chat,text):return await fake_bot('sendMessage',{'chat_id':chat,'text':text})
    a.send=sender
    cases=[]
    def case(name,text='',uid=123,**kw):cases.append(dict(name=name,text=text,uid=uid,**kw))
    for text in ['/start','/help','/id','/users','/access','/tables','/use Склад','/new','/clear','/analyze','/cancel','/projects','/projectrequest Новий тест два','/projectapprove PR1','/projectreject PR1','/receiptpdf','/invoice','/today','/history болти','/balance','/status R1 | У закупці','/export Заявки','/create Тестова | Назва, Сума','/grant 456 read Заявки','/revoke 456 Заявки','/invoices','/unknown']:
        case('command '+text,text,confirm=text.startswith(('/projectapprove','/projectreject','/status','/projectrequest')))
    for text in ['/start','/tables','/users','/today','/invoices','/balance','/export Заявки']:
        case('worker '+text,text,456)
    case('new worker /start','/start',789)
    case('no access chat','Привіт',789)
    case('natural payable','мені скинь рахунки')
    case('natural users','покажи користувачів')
    case('natural new project','додай проєкт Новий тест два')
    case('chat','Який диск обрати для сталі 3 мм?',answers=[{'intent':'other'},'Тестова консультація'])
    case('worker chat','Який диск обрати для сталі 3 мм?',456,answers=[{'intent':'other'},'Тестова консультація'])
    for intent,text in [('today','покажи сьогоднішні закупівлі'),('balance','покажи залишки'),('history','які болти раніше купували'),('read','скільки заявок на болти')]:
        case('natural '+intent,text,answers=[{'intent':intent,'table':'Заявки','target':'болти'}])
    vals={'Найменування':'болти М8','Кількість':'10','Од. вим.':'шт','Проєкт':'Тестовий проєкт'}
    for tab in ['Заявки','Склад','Доручення']:
        v=vals if tab=='Заявки' else (dict(vals,**{'Тип операції':'Надходження'}) if tab=='Склад' else {'Завдання':'Перевірити болти','Виконавець':'Юрій','Проєкт':'Тестовий проєкт'})
        case('natural append '+tab,'оформи запис на 10 болтів для тестового проєкту',answers=[{'intent':'append','table':tab},{'table':tab,'values':v}],confirm=True)
    case('slash ask','/ask скільки болтів')
    case('slash add','/add 10 болтів, проєкт Тестовий проєкт',answers=[{'table':'Заявки','values':vals}],confirm=True)
    for target in ['болти М8','немає такої позиції']:
        case('natural status '+target,'болти вже замовили',answers=[{'intent':'status','table':'Закупка','field':'Статус замовлення','value':'Замовлено','target':target}],confirm=True)
    case('photo upload',media={'photo':[{'file_id':'demo','file_size':len(PHOTO)}]})
    case('worker photo upload',uid=456,media={'photo':[{'file_id':'demo','file_size':len(PHOTO)}]})
    case('image document upload',media={'document':{'file_id':'demo','mime_type':'image/jpeg','file_size':len(PHOTO)}})
    case('pdf upload',media={'document':{'file_id':'pdf','mime_type':'application/pdf','file_name':'demo.pdf','file_size':10000}})
    case('unsupported document',media={'document':{'file_id':'xls','mime_type':'application/vnd.ms-excel'}})
    for n in [1,6]:
        case(f'analyze {n} photos','/analyze',photos=n)
        case(f'create PDF {n} photos','/receiptpdf Тестовий проєкт',photos=n)
    case('invoice PDF draft','/invoice проєкт Тестовий проєкт, Демо, 200 грн',docs=True,answers=[{'Проєкт':'Тестовий проєкт','Постачальник':'Демо','Сума':'200','Валюта':'грн','Тип документа':'Рахунок'}],confirm=True)
    case('voice balance',media={'voice':{'file_id':'v','duration':5}},answers=[{'intent':'balance','target':''}])
    for cb in ['su:l:0','su:u:456','su:d:456','su:invalid:456']:
        case('callback '+cb,callback=cb)
    case('callback forbidden',uid=456,callback='su:u:456')
    case('staff outreach','напиши Юрію: перевір болти')
    case('owner task','доручи Юрію перевірити болти на складі',answers=[{'Завдання':'Перевірити болти на складі','Проєкт':'Тестовий проєкт'}],confirm=True)
    from access_ui import token_for
    for mode in ['r','w','b','n']:
        case('access change '+mode,callback='su:q:456:'+token_for('Заявки')+':'+mode)
    package={'images':[{'index':1,'type':'invoice'}],'groups':[{'type':'invoice','images':[1],'supplier':'Демо','amount':'200','currency':'грн','invoice_no':'001','description':'Болти'}]}
    case('invoice photo package','це рахунок на оплату, проєкт Тестовий проєкт',456,photos=1,answers=[package],confirm=True)
    case('invoice missing project','це рахунок на оплату',456,photos=1,answers=[package])
    case('payable photos','/invoices',invoice_photos=True)
    case('duplicate webhook','/id',duplicate=True)
    case('forbidden webhook','/id',secret='wrong')
    case('cancel pending','ні',pending={'record_id':'TEST','values':vals})
    case('pending clarification','уточнення',pending={'record_id':'TEST','values':vals})
    case('voice confirmation safety',media={'voice':{'file_id':'v','duration':5}},voice_text='так',pending={'record_id':'TEST','values':vals})
    case('task notification sync',sync=True)
    results=[]
    for c in cases:
        a.store=make_store();a.history_store=make_store();a.inflight=asyncio.Lock()
        uid=c['uid'];CASE=c['name'];ANSWERS=copy.deepcopy(c.get('answers',[]))
        state=new_dialogue()
        for n in range(c.get('photos',0)):state,_=queue_photo(state,f'photo{n}','album','','image/jpeg')
        if c.get('docs'):state=queue_document(state,'demo_pdf','demo.pdf','')
        if c.get('photos') or c.get('docs'):a.store.save_dialogue(uid,state)
        if c.get('pending'):a.store.save_pending(uid,'Заявки',c['pending'])
        if c.get('duplicate'):a.store.mark_processed(1000)
        if c.get('invoice_photos'):
            col=a.store.client.data['Рахунки'][0].index('Документ / посилання')
            for row in a.store.client.data['Рахунки'][1:]:row[col]='Окремі фото: 1'
        async def voice(*args,**kw):
            EVENTS.append({'stage':'voice','op':'transcribe'});return c.get('voice_text','покажи залишки')
        a.transcribe_telegram_voice=voice
        EVENTS.clear();t=time.perf_counter()
        person={'id':uid,'first_name':'Тестовий власник' if uid==123 else 'Юрій'}
        msg={'from':person,'chat':{'id':uid,'type':'private'},'text':c['text'],**c.get('media',{})}
        body={'update_id':1000,'message':msg}
        if c.get('callback'):body={'update_id':1000,'callback_query':{'id':'cb','from':person,'message':dict(msg,message_id=1),'data':c['callback']}}
        class Request:
            async def json(self):return body
        error=None
        try:
            if c.get('sync'):await a.sync_task_notifications()
            else:await a.webhook(Request(),c.get('secret','test'))
        except Exception as ex:error=repr(ex)
        elapsed=time.perf_counter()-t
        trace=copy.deepcopy(EVENTS)
        results.append({'case':CASE,'local_ms':round(elapsed*1000,3),'counts':dict(Counter(e['stage'] for e in trace)),'trace':trace,'error':error})
        if c.get('confirm') and a.store.get_pending(uid):
            EVENTS.clear();CASE=c['name']+' → confirm';msg['text']='так';body['update_id']=1001;t=time.perf_counter()
            try:await a.webhook(Request(),'test')
            except Exception as ex:error=repr(ex)
            results.append({'case':CASE,'local_ms':round((time.perf_counter()-t)*1000,3),'counts':dict(Counter(e['stage'] for e in EVENTS)),'trace':copy.deepcopy(EVENTS),'error':error})
    # Prompts and fixtures contain only synthetic business records.
    (ROOT/'simulation.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    (ROOT/'prompts.json').write_text(json.dumps(PROMPTS,ensure_ascii=False))
    for r in results:print(r['case'],r['counts'],r['local_ms'],r['error'] or '')
    print('SCENARIOS',len(results),'PROMPTS',len(PROMPTS))

if __name__=='__main__':asyncio.run(main())
