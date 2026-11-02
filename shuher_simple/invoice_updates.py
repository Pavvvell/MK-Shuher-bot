"""Validated invoice changes; no language interpretation or routing."""
IDENTITY=('Проєкт','Постачальник','Номер рахунку','Сума','Валюта','Telegram файли','Статус власника','Статус')


def prepare_invoice_update(store,ids,status,note):
    from payable_invoices import payable_rows
    if status not in ('На оплаті','Оплачено') or not ids or len(ids)>200 or len(set(ids))!=len(ids):
        raise ValueError('Некоректна зміна рахунків.')
    headers,rows=store.read_table('Рахунки',None)
    if any(k not in headers for k in ('ID','Статус оплати','Коментар')):
        raise ValueError('У реєстрі рахунків потрібні колонки «Статус оплати» та «Коментар».')
    eligible={r['ID'] for r in payable_rows(headers,rows)}
    changes=[]
    for rid in ids:
        matches=[r for r in rows if r and str(r[0])==rid]
        if len(matches)!=1 or rid not in eligible:
            raise ValueError('Перелік рахунків змінився. Покажи рахунки ще раз перед оновленням.')
        row={k:str(matches[0][i] or '') if i<len(matches[0]) else '' for i,k in enumerate(headers)}
        if status=='На оплаті' and row['Статус оплати'].strip() not in ('','Не оплачено','На оплаті'):
            raise ValueError('У переліку є рахунок з іншим станом оплати. Потрібне окреме уточнення.')
        old={k:row.get(k,'') for k in (*IDENTITY,'Статус оплати','Коментар')}
        comment=(row['Коментар'].rstrip()+'\n'+note).strip()
        if len(comment)>45000:raise ValueError('Коментар рахунку завеликий для доповнення.')
        changes.append({'id':rid,'expected':old,'values':{'Статус оплати':status,'Коментар':comment},
                        'label':(row.get('Номер рахунку') or rid)+' · '+row.get('Проєкт','')})
    return changes


def commit_invoice_update(store,changes):
    """Validate the complete confirmed selection, then update only its cells."""
    from store import quoted
    if not isinstance(changes,list) or not 1<=len(changes)<=200:
        raise ValueError('Некоректний перелік рахунків.')
    headers,rows=store.read_table('Рахунки',None)
    updates=[]
    for change in changes:
        matches=[(i,r) for i,r in enumerate(rows,2) if r and str(r[0])==change['id']]
        if len(matches)!=1:raise ValueError('Рахунок змінено або видалено. Покажи рахунки ще раз.')
        number,cells=matches[0]
        row={k:str(cells[i] or '') if i<len(cells) else '' for i,k in enumerate(headers)}
        expected,values=change['expected'],change['values']
        if set(values)!={'Статус оплати','Коментар'} or values['Статус оплати'] not in ('На оплаті','Оплачено'):
            raise ValueError('Неприпустима зміна рахунку.')
        if any(row.get(k,'')!=expected.get(k,'') for k in IDENTITY):
            raise ValueError('Реквізити рахунку змінилися після підготовки. Покажи рахунки ще раз.')
        if all(row.get(k,'')==v for k,v in values.items()):continue
        if any(row.get(k,'')!=expected.get(k,'') for k in ('Статус оплати','Коментар')):
            raise ValueError('Статус або коментар рахунку змінився. Покажи рахунки ще раз.')
        for key,value in values.items():
            if key not in headers:raise ValueError('Змінилися колонки реєстру рахунків.')
            index=headers.index(key)+1;column=''
            while index:
                index,rem=divmod(index-1,26);column=chr(65+rem)+column
            updates.append({'range':quoted('Рахунки')+'!'+column+str(number),'values':[[value]]})
    if updates:
        store._invalidate('Рахунки')
        store.client.values().batchUpdate(spreadsheetId=store.id,
            body={'valueInputOption':'RAW','data':updates}).execute()
    return len(updates)//2
