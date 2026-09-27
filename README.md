# AutoTrade X

Telegram-бот автоторговли через xRocket Exchange API. Прибыль не гарантируется.

Сейчас в репозитории этапы 1 и 2: разбор официального API (`docs/XROCKET_API.md`) и инфраструктура SQLite. Торговля, Telegram-интерфейс и WebSocket-клиент ещё не реализованы. `python -m app.main` поднимает базу, прогоняет миграции и `PRAGMA integrity_check`, затем выходит.

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

Вставьте напечатанный ключ в `ENCRYPTION_KEY` в `.env`. Токен бота и токен xRocket на этом этапе не обязательны: торговля не стартует.

```bash
python -m app.main
pytest
```

База: `data/autotrade.db`. Бэкапы: `backups/autotrade_YYYY-MM-DD_HH-MM-SS.db` (SQLite backup API, хранится `BACKUP_RETENTION` файлов).

## Docker

```bash
docker compose up -d --build
```

Контейнер перезапускается, пока его не остановят (`restart: unless-stopped`). Каталоги `./data` и `./backups` смонтированы в контейнер.

## Режимы

У xRocket есть testnet. `XROCKET_ENV` по умолчанию `testnet`, URL в `.env.example` тоже testnet. Режим исполнения сделки в таблице `bot_settings` по умолчанию `paper`: до отдельного этапа бот не отправляет ордера. Mainnet требует явной смены окружения и подтверждения, это будет позже.

Токен биржи хранится только в зашифрованном виде (`encrypted_api_token`). В логи не попадают токен, заголовок Authorization и ключ шифрования.

## Что уже есть

- `docs/XROCKET_API.md` — REST, WebSocket, ошибки, лимиты, пробелы документации.
- Схема SQLite: пользователи, аккаунты xRocket, стратегии и настройки, позиции, ордера, сигналы, сделки, дневная статистика, очередь уведомлений, настройки бота, системные события, heartbeats, свечи.
- Повтор при `database is locked`: 100 мс, 250 мс, 500 мс, 1 с, 2 с, затем ошибка.
- Проверка целостности и аварийный бэкап, если `integrity_check` не равен `ok`.

## Тесты

```bash
pytest
```

`tests/test_sqlite.py`, `tests/test_backup.py`, `tests/test_security.py`.
