"""Model-created invoice cards. A worker's project click authorizes submission.

Each card has its own durable state in the existing private pending ledger;
it does not share the single conversational draft or attachment queue.
"""
import hashlib
from decimal import Decimal, InvalidOperation

from payable_invoices import invoice_caption, one_line
from projects import approved_projects, canonical_project


class InvoiceIntake:
    def __init__(self, runtime):
        self.r = runtime
        self.a = runtime.a

    def key(self, token):
        return f'invoice:{self.r.uid}:{token}'

    async def load(self, token):
        pending = await self.a.sheet(self.a.store.get_pending, self.key(token))
        if not pending or pending['payload'].get('kind') != 'invoice_card':
            raise ValueError('Ця картка вже недоступна. Надішли файл ще раз.')
        return pending['payload']

    async def save(self, card):
        await self.a.sheet(self.a.store.save_pending, self.key(card['token']),
                           'Рахунки', card, 60 * 24 * 30)

    @staticmethod
    def keyboard(card, page=0):
        projects = card['projects']
        page = max(0, min(page, (len(projects)-1)//8))
        token = card['token']
        buttons = [{'text': p[:60], 'callback_data': f'inv:{token}:p:{i}'}
                   for i, p in enumerate(projects) if page*8 <= i < (page+1)*8]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        nav = []
        if page:
            nav.append({'text': '←', 'callback_data': f'inv:{token}:n:{page-1}'})
        if (page+1)*8 < len(projects):
            nav.append({'text': '→', 'callback_data': f'inv:{token}:n:{page+1}'})
        if nav:
            rows.append(nav)
        return {'inline_keyboard': rows}

    async def offer(self, file_id, supplier, invoice_no, amount, currency, document_type, concerns):
        await self.r.rights('Рахунки', write=True)
        photo = next((x for x in self.r.state['photos'] if x['file_id'] == file_id), None)
        doc = next((x for x in self.r.state['documents'] if x['file_id'] == file_id), None)
        if not photo and not doc:
            raise ValueError('Файл не належить поточному користувачу.')
        file = dict(photo or doc)
        identity = file.get('unique_id') or file_id
        token = hashlib.sha256(f'{self.r.uid}:{identity}'.encode()).hexdigest()[:24]
        existing = await self.a.sheet(self.a.store.get_pending, self.key(token))
        if existing:
            card = existing['payload']
        else:
            projects = await self.a.sheet(approved_projects, self.a.store)
            if not projects:
                raise ValueError('Власнику потрібно додати проєкти до довідника.')
            warnings = [one_line(x) for x in concerns if str(x).strip()][:6]
            amount = amount.strip().replace(' ', '').replace('\u00a0', '').replace(',', '.')
            try:
                number = Decimal(amount)
                if not number.is_finite() or number <= 0:
                    raise InvalidOperation
                amount = str(number)
            except InvalidOperation:
                amount = ''
                warnings.append('Не вдалося визначити суму.')
            if not supplier.strip():
                warnings.append('Постачальника не визначено.')
            if not invoice_no.strip():
                warnings.append('Номер документа не визначено.')
            if document_type != 'Рахунок':
                warnings.append('Тип документа: '+document_type+'. Перевір підставу для оплати.')
            card = {'kind': 'invoice_card', 'token': token, 'record_id': 'IN-'+token.upper(),
                    'file': file, 'is_photo': bool(photo), 'projects': projects,
                    'supplier': one_line(supplier), 'invoice_no': one_line(invoice_no),
                    'amount': amount, 'currency': one_line(currency) or 'UAH',
                    'document_type': document_type, 'warnings': list(dict.fromkeys(warnings)),
                    'project': '', 'receipts': {}, 'complete': False}
            await self.save(card)
        if card['complete']:
            await self.a.send(self.r.chat, 'Цей документ уже передано власнику.')
        else:
            label = card['document_type'] + (' №'+card['invoice_no'] if card['invoice_no'] else '')
            payload = {'chat_id': self.r.chat, 'caption': label+'\nОбери проєкт — файл одразу піде власнику.',
                       'reply_markup': self.keyboard(card)}
            method = 'sendPhoto' if card['is_photo'] else 'sendDocument'
            field = 'photo' if card['is_photo'] else 'document'
            if card['is_photo'] and file.get('as_document'):
                raw = await self.r.fetch_media(file_id, 6_000_000)
                reply = await self.a.bot_call(method, payload, {field: ('invoice.jpg', raw, file['mime'])})
            else:
                payload[field] = file_id
                reply = await self.a.bot_call(method, payload)
            card['message_id'] = reply['result']['message_id']
            await self.save(card)
        # The durable card now owns this attachment; later files cannot erase it.
        for field in ('photos', 'documents'):
            self.r.state[field] = [x for x in self.r.state[field] if x['file_id'] != file_id]
        if not self.r.state['photos'] and not self.r.state['documents']:
            self.r.state['notes'] = ''
        await self.r.save()
        self.r.ui_presented = True
        return {'ok': True, 'card_id': token, 'project_buttons_shown': not card['complete'],
                'already_submitted': card['complete'], 'user_message_already_sent': True}

    async def select(self, token, project_index):
        await self.r.rights('Рахунки', write=True)
        card = await self.load(token)
        if not 0 <= project_index < len(card['projects']):
            raise ValueError('Невідомий проєкт у кнопці.')
        project = card['projects'][project_index]
        if card['project'] and card['project'] != project:
            raise ValueError('Цей документ уже подано з іншим проєктом.')
        if card['complete']:
            return card
        if not await self.a.sheet(canonical_project, self.a.store, project):
            raise ValueError('Цей проєкт більше не активний. Потрібна перевірка власника.')
        card['project'] = project
        await self.save(card)
        values = {'Проєкт': project, 'Постачальник': card['supplier'],
                  'Номер рахунку': card['invoice_no'], 'Сума': card['amount'], 'Валюта': card['currency'],
                  'Тип документа': card['document_type'], 'Статус оплати': 'Не оплачено',
                  'Статус власника': 'Очікує перевірки', 'Передано бухгалтерії': 'Ні',
                  'Потрібна перевірка': 'Так', 'Коментар': '\n'.join(card['warnings']),
                  'Telegram файли': card['file']['file_id'],
                  'Документ / посилання': 'Фото Telegram: 1; PDF: 0' if card['is_photo'] else 'Фото Telegram: 0; PDF: 1',
                  'Джерело': 'Telegram'}
        await self.a.write_as(self.r.uid, 'Рахунки', self.a.store.append_workflow,
                              'Рахунки', values, card['record_id'], self.a.user_label(self.r.person))
        if not self.a.owners:
            raise ValueError('Не налаштовано одержувача. Документ збережено.')
        for owner in sorted(self.a.owners):
            receipt = card['receipts'].setdefault(str(owner), {})
            if not receipt.get('file_message_id'):
                payload = {'chat_id': owner, 'caption': invoice_caption(values)}
                if card['is_photo'] and card['file'].get('as_document'):
                    raw = await self.r.fetch_media(card['file']['file_id'], 6_000_000)
                    reply = await self.a.bot_call('sendPhoto', payload, {'photo': ('invoice.jpg', raw, card['file']['mime'])})
                else:
                    method, field = ('sendPhoto', 'photo') if card['is_photo'] else ('sendDocument', 'document')
                    payload[field] = card['file']['file_id']
                    reply = await self.a.bot_call(method, payload)
                receipt['file_message_id'] = reply['result']['message_id']
                await self.save(card)
            if card['warnings'] and not receipt.get('warning_sent'):
                await self.a.bot_call('sendMessage', {'chat_id': owner,
                    'reply_parameters': {'message_id': receipt['file_message_id']},
                    'text': '⚠️ Перевір цей документ:\n'+'\n'.join('• '+x for x in card['warnings'])})
                receipt['warning_sent'] = True
                await self.save(card)
        card['complete'] = True
        await self.save(card)
        return card

    async def callback(self, callback):
        await self.r.rights('Рахунки', write=True)
        _, token, action, index = callback['data'].split(':')
        card = await self.load(token)
        message_id = callback['message']['message_id']
        if action == 'n':
            await self.a.bot_call('editMessageReplyMarkup', {'chat_id': self.r.chat, 'message_id': message_id,
                'reply_markup': self.keyboard(card, int(index))})
            return
        if action != 'p':
            raise ValueError('Невідома кнопка.')
        card = await self.select(token, int(index))
        try:
            await self.a.bot_call('editMessageCaption', {'chat_id': self.r.chat, 'message_id': message_id,
                'caption': '✅ Передано власнику · '+card['project'], 'reply_markup': {'inline_keyboard': []}})
        except Exception:
            self.a.log.warning('Invoice delivered; card caption could not be refreshed')
            await self.a.send(self.r.chat, '✅ Передано власнику · '+card['project'])
