# AutoTrade X

Telegram-бот автоторговли через xRocket Exchange API. Прибыль не гарантируется.

Сейчас в репозитории этапы 1–5: разбор API, SQLite, клиенты REST/WebSocket, бумажная торговля, Telegram-меню и бэктест. `python -m app.main` поднимает базу и выходит. `python -m app.main --paper` и `python -m app.main --telegram` крутят движок на публичных данных и ордера на биржу не отправляют.

## Стек

Python 3.12+, Aiogram 3, SQLAlchemy 2 async, aiosqlite, SQLite (WAL), Alembic, Pydantic Settings, cryptography (Fernet).

## Локальный запуск

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Вставьте напечатанный ключ в `ENCRYPTION_KEY` в `.env`. Токен xRocket для paper и бэктеста не нужен: публичные свечи и стакан открыты, ордера не отправляются. Для Telegram нужен `BOT_TOKEN` от BotFather и, если хотите команду `/admin`, ваш числовой id в `ADMIN_IDS`.

```bash
python -m app.main
python -m app.main --paper --cycles 1
python -m app.main --telegram
python -m backtest --pair BTC-USDT --days 30
python -m backtest --env mainnet --pair BTC-USDT --days 90 --timeframe 15m
python -m backtest --env mainnet --pair ETH-USDT --days 180 --timeframe 1h
pytest
```

`--paper` без `--cycles` работает, пока процесс не получит SIGINT или SIGTERM. Открытые бумажные позиции при остановке не закрываются. `--telegram` показывает меню, принимает команды и крутит тот же бумажный цикл. Пустой `BOT_TOKEN` завершает процесс с кодом 2.

Исследование входов (`python -m research`) гоняет отдельные модули в `research/` по публичным свечам Binance и кэшу xRocket. Оно не включает стратегию в бота и не меняет дефолты живого режима. Кэш свечей лежит в `data/research/` и в репозиторий не входит.

Бэктест пишет отчёт в консоль и файлы `data/backtests/reports/`. `--env` выбирает публичный хост свечей: `testnet` по умолчанию или `mainnet`. Кэш лежит отдельно в `data/backtests/testnet` и `data/backtests/mainnet`. Mainnet в этом режиме только читает публичные свечи, карточку пары и публичные `trade-fees`, без токена и без ордеров. Комиссия прогона — taker из этого ответа, иначе 0.3%. `--fee` меняет комиссию одного прогона и не трогает дефолт. `--timeframe` задаёт интервал свечей (`15m`, `1h`, `4h`); живой режим по умолчанию остаётся на 15m. Решение принимается по закрытой свече, сделка считается по открытию следующей. Порог спреда 0.5% в бэктесте не подставляется как фактический спред.

Проверка testnet только чтением (символы, свечи, балансы, без ордеров):

```bash
XROCKET_API_TOKEN=... python -m scripts.smoke_testnet
```

База: `data/autotrade.db`. Бэкапы: `backups/autotrade_YYYY-MM-DD_HH-MM-SS.db` (SQLite backup API, хранится `BACKUP_RETENTION` файлов).

## Docker

```bash
docker compose up -d --build
```

Контейнер перезапускается, пока его не остановят (`restart: unless-stopped`). Каталоги `./data` и `./backups` смонтированы в контейнер.

## Режимы

У xRocket есть testnet. `XROCKET_ENV` по умолчанию `testnet`, URL в `.env.example` тоже testnet. Режим исполнения по умолчанию `paper`. Реальные ордера включаются только вручную: `EXECUTION_MODE=testnet` и `ALLOW_EXCHANGE_ORDERS=true`. В paper флаг ордера не открывает. Решения заказчика: `docs/CLIENT_DECISIONS.md`.

Токен биржи хранится только в зашифрованном виде (`encrypted_api_token`). В логи не попадают токен, заголовок Authorization и ключ шифрования.

## Что уже есть

- `docs/XROCKET_API.md` — REST, WebSocket, ошибки, лимиты, пробелы документации.
- `xrocket/` — REST и WebSocket. Свечи и стакан в SQLite из сокета не пишутся. Таймаут создания ордера не приводит к повторному POST. Выход — рыночный SELL с `size`.
- `trading/` — индикаторы, тренд (только LONG) на таймфрейме настроек, по умолчанию 15m, риск, бумажное исполнение, ордера и позиции. Ордер создаётся только в order manager, `clientOrderId` пишется до запроса. Размер позиции учитывает комиссии и ожидаемое проскальзывание. Сделка с чистым R:R после издержек ниже 1.5 пропускается.
- `python -m app.main --paper` — цикл на публичном REST testnet. Капитал бумажный, с `PAPER_STARTING_EQUITY`.
- `python -m app.main --telegram` — меню Aiogram 3 и тот же бумажный цикл. Токен биржи в чате сразу шифруется, сообщение удаляется. MAINNET записывается только после фразы `START LIVE`, `XROCKET_ENV=mainnet` и проверки, что TON-USDT есть на бирже.
- `python -m backtest --pair BTC-USDT --days 30` — прогон стратегии по истории testnet. `--env mainnet` читает публичные свечи основной сети. Ордера не отправляются.
- Схема SQLite: пользователи, аккаунты xRocket, стратегии и настройки, позиции, ордера, сигналы, сделки, дневная статистика, очередь уведомлений, настройки бота, системные события, heartbeats, свечи. У настроек бота есть `paper_cash`.
- Повтор при `database is locked`: 100 мс, 250 мс, 500 мс, 1 с, 2 с, затем ошибка.
- Проверка целостности и аварийный бэкап, если `integrity_check` не равен `ok`.

## Тесты

```bash
pytest
```

`tests/test_sqlite.py`, `tests/test_backup.py`, `tests/test_security.py`, `tests/test_rest_client.py`, `tests/test_websocket.py`, `tests/test_trading_policy.py`, плюс тесты индикаторов, стратегии, риска, ордеров, позиций, сверки, уведомлений, пяти критических сценариев, Telegram-команд и бэктеста.
