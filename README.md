<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:00F0FC,100:FF00FF&height=220&section=header&text=AutoTrade%20X&fontSize=58&fontColor=00F0FC&animation=twinkling&fontAlignY=32&desc=%D0%B7%D0%B0%D0%BC%D0%BE%D1%80%D0%BE%D0%B6%D0%B5%D0%BD%20%C2%B7%20xRocket%20%C2%B7%20Telegram&descAlignY=54&descAlign=58" alt="AutoTrade X" width="100%">

<img src="https://readme-typing-svg.demolab.com?font=Share+Tech+Mono&weight=500&size=22&duration=3200&pause=900&color=FF00FF&background=00000000&center=true&vCenter=true&width=820&height=48&lines=%D0%9F%D0%BB%D0%B0%D1%82%D1%84%D0%BE%D1%80%D0%BC%D0%B0+%D0%B0%D0%B2%D1%82%D0%BE%D1%82%D1%80%D0%B5%D0%B9%D0%B4%D0%B8%D0%BD%D0%B3%D0%B0+%D0%B4%D0%BB%D1%8F+xRocket+Exchange;Telegram-%D0%B8%D0%BD%D1%82%D0%B5%D1%80%D1%84%D0%B5%D0%B9%D1%81+%C2%B7+paper-%D1%80%D0%B5%D0%B6%D0%B8%D0%BC;%D0%91%D1%8D%D0%BA%D1%82%D0%B5%D1%81%D1%82+%D0%B1%D0%B5%D0%B7+look-ahead;%D0%A1%D1%82%D0%B0%D1%82%D1%83%D1%81%3A+%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82+%D0%B7%D0%B0%D0%BC%D0%BE%D1%80%D0%BE%D0%B6%D0%B5%D0%BD" alt="Платформа автотрейдинга для xRocket Exchange">

<br>

[![Python](https://img.shields.io/badge/Python-3.12+-00F0FC?style=for-the-badge&logo=python&logoColor=000000&labelColor=000000)](https://www.python.org/)
[![Aiogram](https://img.shields.io/badge/Aiogram-3-FF00FF?style=for-the-badge&logo=telegram&logoColor=000000&labelColor=000000)](https://docs.aiogram.dev/)
[![SQLite](https://img.shields.io/badge/SQLite-WAL-00F0FC?style=for-the-badge&logo=sqlite&logoColor=000000&labelColor=000000)](https://www.sqlite.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2-FF00FF?style=for-the-badge&labelColor=000000)](https://www.sqlalchemy.org/)
[![Статус](https://img.shields.io/badge/статус-заморожен-FF00FF?style=for-the-badge&labelColor=000000)](#статус-заморожен)
[![Лицензия](https://img.shields.io/badge/license-MIT-00F0FC?style=for-the-badge&labelColor=000000)](LICENSE)

</div>

## Статус: заморожен

> **Живое исполнение реальных ордеров не включено.** Проект остановлен и сохранён как есть. Режим по умолчанию — `paper`: на биржу ничего не отправляется. Флаги `EXECUTION_MODE=paper` и `ALLOW_EXCHANGE_ORDERS=false` трогать для «боевого» запуска не нужно — запуск реальных ордеров в этом состоянии не предусмотрен.

Исследование входов (режим по длинной средней, пробой Donchian, вход лимиткой maker, сетка в боковике) на истории Binance и на публичных свечах xRocket mainnet **не дало стратегии, которая обыграла бы buy-and-hold с учётом риска при комиссии taker 0,3%**. Подробные таблицы in-sample, слепого года и проверки на xRocket: [docs/RESEARCH_ENTRIES.md](docs/RESEARCH_ENTRIES.md).

**Не финансовый совет. Используйте на свой риск.** Прибыль не гарантируется. Прошлый бэктест не обещает будущий результат.

## Что это

AutoTrade X — платформа автотрейдинга для [xRocket Exchange](https://exchange.xrocket.exchange) с интерфейсом в Telegram. Бот умеет читать публичный рынок, вести бумажный счёт, считать риск и прогонять историю. Решения заказчика и пробелы документации лежат в `docs/`.

## Возможности

- REST- и WebSocket-клиенты xRocket: свечи, стакан, публичные комиссии, приватные методы под Bearer.
- Paper-режим: бумажный капитал, рыночные сделки локально, ордера на биржу не уходят.
- Риск-менеджер: размер позиции с учётом комиссии и ожидаемого проскальзывания, пропуск сделки при чистом R:R ниже порога.
- Идемпотентные ордера: `clientOrderId` записывается до запроса, повторный POST после таймаута не делается.
- Reconciliation: сверка локальных позиций и ордеров с ответами биржи.
- Бэктест без look-ahead: решение по закрытой свече, вход по открытию следующей, стоп раньше тейка, гэп за стопом — выход по open.
- Research-модуль: отдельные стратегии (режим, Donchian, сетка) и слой исполнения лимитками. В боевой цикл они не подключены.

## Стек

Python 3.12+, Aiogram 3, SQLAlchemy 2 (async) + aiosqlite, SQLite в режиме WAL, Alembic, Pydantic Settings, cryptography (Fernet).

## Быстрый старт

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Сгенерированный ключ вставьте в `ENCRYPTION_KEY` локального `.env`. Файл `.env` в git не входит. Токен биржи для paper и бэктеста не нужен: публичные свечи и стакан открыты.

```bash
python -m app.main
python -m app.main --paper --cycles 1
python -m app.main --telegram
python -m backtest --pair BTC-USDT --days 30
python -m backtest --env mainnet --pair BTC-USDT --days 90 --timeframe 15m
python -m research
pytest
```

| Команда | Что делает |
| --- | --- |
| `python -m app.main` | Поднимает базу и выходит. |
| `python -m app.main --paper` | Бумажный цикл на публичных данных. Без `--cycles` работает до SIGINT/SIGTERM. Ордера не отправляет. |
| `python -m app.main --telegram` | Меню Aiogram и тот же бумажный цикл. Пустой `BOT_TOKEN` завершает процесс с кодом 2. |
| `python -m backtest` | Прогон боевой стратегии по свечам. `--env mainnet` читает публичную историю, без токена и без ордеров. |
| `python -m research` | Исследование входов. Дефолты живого режима не меняет. |

`--paper` без `--cycles` не закрывает открытые бумажные позиции при остановке. Живой таймфрейм по умолчанию — 15m. `--timeframe` у бэктеста задаёт интервал одного прогона.

Docker:

```bash
docker compose up -d --build
```

Каталоги `./data` и `./backups` монтируются в контейнер.

Опциональная проверка testnet только чтением (символы, свечи, балансы, без ордеров): `python -m scripts.smoke_testnet`. Токен для этого скрипта задайте в окружении и не коммитьте.

## Конфигурация

Образец без секретов: [`.env.example`](.env.example). Скопируйте его в `.env` и заполните локально.

| Переменная | Зачем |
| --- | --- |
| `BOT_TOKEN` | Токен BotFather. Пустой — Telegram не стартует. |
| `ADMIN_IDS` | Числовые id для команды `/admin`. |
| `ENCRYPTION_KEY` | Ключ Fernet. Генерируется локально, в репозиторий не кладётся. |
| `XROCKET_ENV` | `testnet` по умолчанию. URL в примере тоже testnet. |
| `EXECUTION_MODE` | `paper`. Реальные ордера этим репозиторием не включаются. |
| `ALLOW_EXCHANGE_ORDERS` | `false`. |
| `DEFAULT_FEE_RATE` | `0.003` — taker 0,3%, доля, не проценты. |
| `DATABASE_URL` | SQLite, файл `data/autotrade.db`. |
| `XROCKET_API_TOKEN` | Только для ручного smoke-скрипта. В примере пустой. |

Токен биржи, если его когда-нибудь сохраняют через бота, лежит в базе в зашифрованном виде. В логи не попадают токен, заголовок Authorization и ключ шифрования.

## Структура

```text
app/            точка входа, paper-цикл
bot/            Telegram: команды, клавиатуры, состояния
xrocket/        REST, WebSocket, комиссии, точность лотов
trading/        стратегия, риск, ордера, позиции, бумажное исполнение
services/       сверка, уведомления, бэкап, watchdog
database/       модели, репозитории, Alembic
backtest/       движок без look-ahead и CLI
research/       исследование входов, стратегии отдельно от бота
docs/           API, решения заказчика, отчёт исследования
tests/          pytest
```

База: `data/autotrade.db`. Бэкапы: `backups/`. Кэш свечей бэктеста и research (`data/backtests/`, `data/research/`) и файлы `*.db` в git не входят.

## Тесты

```bash
pytest
```

Покрыты SQLite и бэкап, шифрование, REST и WebSocket, политика торговли, индикаторы, стратегия, риск, ордера, позиции, сверка, уведомления, пять критических сценариев, команды Telegram, бэктест (в том числе отсутствие look-ahead) и research (лимитки, сетка, стоп).

## Контакты

Автор: **KodoDrive**

| | |
| --- | --- |
| Telegram | [t.me/gveom](https://t.me/gveom) · `@gveom` |
| Email | [antihype2205@yandex.ru](mailto:antihype2205@yandex.ru) |
| GitHub | [github.com/svod011929](https://github.com/svod011929) |

<div align="center">

[![Telegram](https://img.shields.io/badge/Telegram-@gveom-00F0FC?style=for-the-badge&logo=telegram&logoColor=000000&labelColor=000000)](https://t.me/gveom)
[![Email](https://img.shields.io/badge/email-KodoDrive-FF00FF?style=for-the-badge&labelColor=000000)](mailto:antihype2205@yandex.ru)
[![GitHub](https://img.shields.io/badge/GitHub-svod011929-00F0FC?style=for-the-badge&logo=github&logoColor=000000&labelColor=000000)](https://github.com/svod011929)

</div>
