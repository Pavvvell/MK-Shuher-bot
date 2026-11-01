"""In-memory Sheets API and synthetic business records; no network."""
import copy,re
import store as sm
EVENTS=[]
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
    d[sm.USERS]+=[['123','owner','Тестовий власник','Заявки',''],['456','worker','Yuriy','Заявки','']]
    tables={
      'Проєкти':['Назва проєкту','Статус'],
      'Запити на проєкти':['Назва проєкту','Причина','Статус','Погодив','Дата рішення'],
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
    add('Доручення',ID='T1',**{'Завдання':'Перевірити болти','Виконавець':'Yuriy','Проєкт':'Тестовий проєкт','Статус':'Нове'})
    for i in range(3):add('Рахунки',ID=f'I{i}',**{'Проєкт':'Тестовий проєкт','Постачальник':'Демо','Тип документа':'Рахунок','Telegram файли':f'demo_pdf{i}','Документ / посилання':'Фото Telegram: 0; PDF: 1','Статус':'Очікує перевірки'})
    d[sm.GRANTS]+=[['456',t,'write','123'] for t in ['Заявки','Закупка','Склад','Доручення','Рахунки','Накладні']]
    return d

def make_store():
    s=sm.SheetsStore.__new__(sm.SheetsStore);s.id='synthetic';s.client=API(fixtures());return s
