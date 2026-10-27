# Шухер Lite — Telegram + GPT + Google Sheets

Мінімальний бот для МК Склад. Ніякого PostgreSQL, Cloud SQL, Mini App, оркестратора і міграції даних.

## Що працює

- **Власник:** `/users` (Telegram ID усіх, хто писав боту); `/create` (створити бізнес-вкладку); `/grant ID read|write Назва`; `/revoke ID Назва`.
- **Усі:** `/start` (реєстрація), `/id` (власний ID), `/tables` (тільки свої доступні), `/use Назва`, `/ask запит`, `/add дані`, `/export` (CSV), `/cancel`.
- ШІ може тільки коротко відповідати за даними доступної таблиці або готувати **новий рядок**; кожний запис підтверджується «так».
- **Створення таблиці тільки власником і тільки явною командою `/create`**. ChatGPT/OpenAI не має можливості створювати таблиці, видавати доступи, видаляти або змінювати існуючі записи.
- Власник може переглядати всі Telegram ID; чужі ID недоступні звичайним користувачам. `/id` показує кожному лише його власний.
- Користувачі **не отримують прямого Google Drive доступу**. Права `read/write` перевіряє бот за Telegram ID. Якщо комусь вручну надати доступ до всього Sheets-файлу в Google, він зможе обійти Telegram-обмеження.

## Таблиці

**Один Google Spreadsheet-файл** — у ньому кожна бізнес-таблиця є окремою вкладкою (sheet tab). Це дозволяє додавати їх без Google Drive OAuth та без окремих платних баз даних.

Сховище технічних параметрів — приховані вкладки `_Shuher_Users`, `_Shuher_Access`, `_Shuher_Pending`, `_Shuher_Updates`. Вони створюються один раз під час запуску; це НЕ бізнес-таблиці. Власник не повинен видаляти або перейменовувати їх.

Нові бізнес-вкладки створюються **тільки** через:
```
/create Інструмент | Найменування, Кількість, Од. вим., Статус
/create Комуналка
```
Бот автоматично додає `ID`, `Дата` та `Автор` до нових вкладок; вони потрібні для аудиту й повторної доставки повідомлень. У вже наявних вкладках можна читати дані, але **додавання через бот вимагає першої колонки `ID`** (нова структура автоматично її містить). Зміни існуючих позицій поки не підтримуються.

## Найкоротший деплой у Google Cloud Run (без Cloud SQL)

1. Проєкт GCP: `mk-shuher-bot`. У Google Sheets сервісний акаунт `shuher-sheets@mk-shuher-bot.iam.gserviceaccount.com` повинен мати **Editor** на вибраний Spreadsheet. Уже є тестова копія `1cACUDeIyCLWFMpzJ5LP-udj5jqIkZ1s_etsX5dQyHP4`; бойовий реєстр поки не зачіпати.
2. Запусти на Mac `gcloud auth login` під новим Gmail, `gcloud config set project mk-shuher-bot`.
3. `gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com sheets.googleapis.com`.
4. Відкрий `shuher_simple/`. Створи приватний локальний файл `deploy.env.yaml` (у .gitignore, .gcloudignore та .dockerignore). Не надсилай його в чат і не коміть:
```yaml
TELEGRAM_BOT_TOKEN: "ЧИННИЙ_ТОКЕН_БОТА"
TELEGRAM_WEBHOOK_SECRET: "ЧИННИЙ_WEBHOOK_SECRET"
OPENAI_API_KEY: "ЧИННИЙ_OPENAI_КЛЮЧ"
OPENAI_MODEL: "gpt-4.1-mini"
SPREADSHEET_ID: "1cACUDeIyCLWFMpzJ5LP-udj5jqIkZ1s_etsX5dQyHP4"
OWNER_IDS: "ТВІЙ_ЧИСЛОВИЙ_TELEGRAM_ID"
REGISTER_TELEGRAM_WEBHOOK: "false"
```
5. Із каталогу `shuher_simple/`:
```sh
gcloud run deploy shuher-lite \
  --source . \
  --region=europe-west3 \
  --service-account=shuher-sheets@mk-shuher-bot.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --cpu=1 --memory=512Mi --concurrency=1 --max-instances=1 --min-instances=0 \
  --env-vars-file=deploy.env.yaml
```
6. Перевір:
```sh
URL=$(gcloud run services describe shuher-lite --region=europe-west3 --format='value(status.url)')
curl "$URL/health"
```
Очікується `{"status":"ok","backend":"google-sheets"}`.
7. Після успішного здоров'я, щоб **переключити саме наявного Telegram-бота**:
```sh
gcloud run services update shuher-lite \
  --region=europe-west3 \
  --update-env-vars="PUBLIC_BASE_URL=$URL,REGISTER_TELEGRAM_WEBHOOK=true"
curl "$URL/health"
```
Новий сервіс зареєструє Telegram webhook на Cloud Run. Не запускай старий Railway-сервіс з автоматичною реєстрацією webhook; він може перехопити webhook назад після рестарту.
8. У Telegram: `/start`, `/users`, `/create ТЕСТ-ШУХЕР | Найменування, Кількість, Одиниця`, `/use ТЕСТ-ШУХЕР`, `/add 7 гайок М12`, `так`.

Цей бот читає дані вже наявних вкладок вибраної таблиці, нічого не мігрує. Перший запуск — тільки на тестовому Spreadsheet; після перевірки виберемо остаточний робочий файл.

## Безпека та компроміси

- Чужі Telegram ID можна отримати тільки командою `/users` з `OWNER_IDS`; `/grant` і `/create` додатково перевіряють власника.
- Webhook приймає лише запити з правильним `X-Telegram-Bot-Api-Secret-Token`.
- CSV `/export` надсилається тільки користувачу з доступом через приватний Telegram-чат.
- Поточний Telegram webhook містить лише одного активного отримувача; перед зміною хостингу не запускати два сервіси зі встановленням webhook одночасно.
- Повідомлення та текстові дані читає OpenAI API при AI-запиті. Не використовувати для секретів, не призначених стороннім процесорам.
- Поточна версія не обробляє PDF/фото/аудіо з Telegram і не редагує існуючі рядки.
- Більші таблиці й дуже часті одночасні записи потребуватимуть окремих гарантій ідемпотентності. Для декількох користувачів `max-instances=1`, `concurrency=1` та перевірки ID достатні для початкового тесту, але це не транзакційна БД.
- Прямий доступ до Spreadsheet-файлу у Google НЕ обмежується ботом по вкладках. Якщо потрібні прямі редактори Gmail із різними дозволами, доведеться створювати окремі Spreadsheet-файли або залишити доступ тільки через Telegram.
- Cloud Run має безплатний рівень, але API OpenAI, вихідний трафік і збірки можуть оплачуватись окремо.

## Offline smoke

```sh
pip install -r requirements.txt
python -m compileall -q app.py store.py
```
