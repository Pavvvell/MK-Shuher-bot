"""Typed capabilities exposed to the model; these do not route user phrases."""
def obj(**properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
def string(description='', **kw):
    return {'type':'string','description':description, **kw}
def array(items, limit=100):
    return {'type':'array','items':items,'maxItems':limit}
def function(tool_name,description,**properties):
    return {'type':'function','name':tool_name,'description':description,
            'parameters':obj(**properties),'strict':True}
S=string
IDS=array(S(),200)
FIELDS=array(obj(name=S(),value=S(maxLength=4000)),50)
SCHEMAS=[
 function('read_records','Читає актуальні рядки дозволеної таблиці. Порожні filters = всі. Повертає загальну кількість і сторінку, не обрізаний звіт.',
          table=S(),query=S('Пошукові слова або порожньо'),ids=IDS,offset={'type':'integer','minimum':0},limit={'type':'integer','minimum':1,'maximum':50}),
 function('table_schema','Поля конкретної доступної таблиці й допустимі статуси. Використовуй перед підготовкою запису.',table=S()),
 function('overview','Актуальне зведення з дозволених робочих реєстрів: заявки, закупка, доручення, рахунки, блокери, строки.',project=S('Точна назва одного проєкту або порожньо для всіх')),
 function('stock_balance','Розрахунок залишків за складськими рухами.',query=S()),
 function('search_history','Пошук в окремому архіві. Історичні дані не є поточними залишками.',query=S(minLength=2)),
 function('list_projects','Затверджені проєкти.'),
 function('list_people','Зареєстровані працівники та їх ID для адресації повідомлень/доручень. Лише власник.'),
 function('send_invoices','Надсилає власнику збережені PDF/фото рахунків із підписами. Порожній ids = усі неоплачені. Повертає конкретні delivered/failed ID. Нічого не оплачує.',ids=IDS),
 function('send_staff_message','Негайно передає текст зареєстрованим працівникам за явним дорученням власника. Не для чернеток, обговорень чи повідомлень самому власнику.',
          recipient_ids=array({'type':'integer'},5),text=S(minLength=1,maxLength=3500)),
 function('prepare_record','Готує нову заявку, доручення чи складську операцію з кнопкою підтвердження. Поля бери з table_schema. Нові рахунки подаються лише через offer_invoice_projects.',
          table=S(),fields=FIELDS,photo_ids=array(S(),6),document_ids=array(S(),4)),
 function('prepare_status','Готує зміну одного статусу наявного запису. ID спершу отримай читанням. Рахунки оновлюй через prepare_invoice_payment.',
          table=S(),record_id=S(),field=S(),value=S()),
 function('prepare_invoice_payment','Готує оновлення статусу конкретних раніше прочитаних/виданих рахунків. На оплаті = вже передав на оплату, Оплачено = повністю оплатили. Строк майбутньої оплати запиши датою в note, без вигаданого часу. Показує точний перелік для підтвердження.',
          ids=IDS,status=S(enum=['На оплаті','Оплачено']),note=S(maxLength=600)),
 function('cancel_draft','Скасовує незавершену чернетку поточного користувача.'),
 function('prepare_project_request','Готує запит власнику на новий проєкт.',name=S(minLength=1,maxLength=90),reason=S(maxLength=500)),
 function('prepare_project_decision','Готує рішення власника за конкретним запитом на проєкт.',request_id=S(),approve={'type':'boolean'}),
 function('prepare_table','Готує створення нової вкладки в робочій Google-таблиці. Тільки власник; не створює файлів у чужому Google Drive.',name=S(minLength=1,maxLength=90),columns=array(S(),40)),
 function('export_table','Надсилає CSV доступної таблиці поточному користувачу.',table=S()),
 function('show_access_panel','Відкриває власнику кнопки керування доступами. Модель сама права не змінює.'),
 function('retry_task_notifications','Повторює недоставлені повідомлення за записаними дорученнями. Тільки власник.'),
 function('inspect_photos','Показує моделі фото поточного користувача. PDF не читає. Порожній ids = усі прикріплені фото.',ids=array(S(),6)),
 function('inspect_documents','Читає PDF поточного користувача, включно зі сканами. Повертає вміст моделі. Не проси працівника переписувати поля.',ids=array(S(),4)),
 function('offer_invoice_projects','Показує оригінальний файл із кнопками проєктів. Натискання працівником одразу подає документ власнику. Для кожного рахунку/замовлення окремий виклик. Невідомі поля залиш порожніми: суму, валюту, постачальника у працівника НЕ уточнюй. Усі сумніви в concerns зберігаються в реєстрі. Власнику надсилається лише файл із підписом, без супровідних повідомлень. Інструмент уже надсилає картку; повторювати її текстом не потрібно.',
          file_id=S(),supplier=S(maxLength=160),invoice_no=S(maxLength=160),amount=S(maxLength=40),currency=S(maxLength=20),
          document_type=S(enum=['Рахунок','Замовлення','Документ на перевірку']),concerns=array(S(maxLength=160),6)),
 function('build_photo_pdf','Збирає вибрані фото накладних у PDF та надсилає поточному користувачу. Не записує надходження на склад.',project=S(),photo_ids=array(S(),6)),
 function('clear_attachments','Очищує поточні фото й PDF з контексту, не видаляє файли з обліку.'),
 function('prepare_purchase_bundle','Готує підтверджені карткові покупки з фото оплати й товару. Нові неоплачені рахунки подавай через offer_invoice_projects. Порядкові номери фото з inspect_photos; проєкти явно від користувача.',
          images=array(obj(index={'type':'integer','minimum':1,'maximum':6},type=S(enum=['card_payment','goods_photo','invoice','other'])),6),
          groups=array(obj(type=S(enum=['card_purchase','invoice']),images=array({'type':'integer'},6),project=S(),supplier=S(),amount=S(),currency=S(),description=S(),invoice_no=S(),quantity=S(),note=S()),5)),
]
BY_NAME={t['name']:t for t in SCHEMAS}
