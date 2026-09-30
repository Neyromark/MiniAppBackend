# CurrentMiniApp (Live): бэкенд + фронтенд

* `CurrentMiniAppBackend/` — FastAPI + WebSocket `/ws` + SQLAlchemy ORM (модели совместимы с `CurrentChatBot`).
* `CurrentMiniAppFrontendLive/` — копия `CurrentMiniAppFrontend` без демо-данных; открывается через `index.html`.

## Запуск
```bash
cd CurrentMiniAppBackend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py            # ws://127.0.0.1:8000/ws, БД SQLite (miniapp.db) + демо-данные
```
Затем откройте `CurrentMiniAppFrontendLive/index.html` (двойной клик или любым статическим сервером).
* другой пользователь (только разработка, нужен `DEV_AUTH=true`): `index.html?dev=1&user=1..5` (1 — владелец, 2/3 — админы, 4/5 — сотрудники);
* другой адрес бэкенда: `index.html?ws=ws://192.168.1.42:8000/ws` (и `HOST=0.0.0.0` в `.env`).

Общая БД с ботом (MSSQL): см. `.env.example` (`DATABASE_URL` или `MSSQL_CONNECTION_STRING`, нужен `pyodbc`).
`Task_Availability` — единственная новая таблица (создаётся автоматически).

## Протокол WebSocket
`→ {id, action, payload}`  `← {type:"response", id, ok, data | error, message, details}`  
`← {type:"data_changed", reason, by}` — рассылка всей организации; клиенты запрашивают `snapshot`.  
Действия: `hello` (initData), `snapshot`, `task.create|update|delete`, `pool.take|return`,
`deadline.create|accept|delete`, `ping`.

## Двойная проверка (как в AdvancedMiniAppBackend, строже)
1. Клиент валидирует форму; сервер валидирует всё заново (типы, диапазоны, права по роли на каждый запрос).
2. Конфликты проверяются до записи и повторно после `flush()`, до `commit`; при ошибке транзакция откатывается.
3. «Взять из пула» и «принять дедлайн» — условный `UPDATE ... WHERE AssigneeUserId IS NULL` (защита от гонок).

Проверка: `./venv/bin/pip install websockets && ./venv/bin/python smoke_test.py` (временная БД, 32 проверки).

## Связь с чат-ботом (отдельные микросервисы)
Бот — WebSocket-клиент: подключается к `ws://<мини-апп>:8000/ws/bot` (переподключается сам, порядок запуска любой).
* мини-апп → бот: `{"type":"notify","id","maxId","text"}` → бот шлёт сообщение в MAX и отвечает `{"type":"ack","id","ok"}`;
  уведомление хранится в `Bot_Notification` (принадлежит мини-аппу) и помечается отправленным **только после ack**;
  пока бот офлайн — копится и уходит при подключении (не старше 2 суток, до 5 попыток).
* бот → мини-апп: `{"type":"event","reason":"changed"}` после каждого коммита, затронувшего `Task`/`Task_Instance`
  → клиентам приходит `data_changed` (`bot.changed`).
* Настройка: `BRIDGE_TOKEN` в `.env` обоих сервисов (без него — только localhost); в боте ещё `MINIAPP_WS_URL`
  (по умолчанию `ws://127.0.0.1:8000/ws/bot`) и `MINIAPP_LINK=false` для отключения.
* Бот **не читает таблицы мини-аппа**. Общая БД остаётся только для данных задач; опрос БД (`watcher.py`) — запасной.

## Авторизация (WebAppData от MAX)
Клиент отправляет серверу `window.WebApp.initData` **как есть** (строкой). Сервер (`max_auth.py`) по ТЗ MAX:
убирает `hash`, декодирует значения, сортирует ключи, считает `HMAC-SHA256(HMAC-SHA256("WebAppData", BOT_TOKEN), launch_params)`
и сравнивает с `hash` за постоянное время. Сверх ТЗ проверяются `auth_date` (не старше `AUTH_MAX_AGE_SECONDS`, не из будущего),
единственность каждого ключа и длина. Пользователь берётся только из **подписанного** поля `user` и ищется по `User.MaxId`;
роль читается из БД на каждый запрос. Без `BOT_TOKEN` сервер не стартует. `DEV_AUTH=true` (неподписанный `user.id`) — только для разработки.
Проверки: `./venv/bin/python test_max_auth.py` (сверка с эталонным TypeScript из ТЗ через Node + атаки) и `smoke_test.py`.
