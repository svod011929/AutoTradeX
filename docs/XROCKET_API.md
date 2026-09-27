# xRocket Exchange API — выжимка для AutoTrade X

Дата сверки: 2026-09-27.

Источник правды — только официальная документация. Ниже пересказано то, что там есть. Всё, чего в доках нет, помечено **NOT DOCUMENTED**. Эндпоинты не выдуманы.

Просмотрены:

- [Обзор](https://docs.xrocket.exchange/api/exchange/exchange-api-overview)
- [REST API](https://docs.xrocket.exchange/api/exchange/reference/exchange-rest-api) и страницы методов (OpenAPI, который рендерит сайт документации)
- [Getting Started / Account](https://docs.xrocket.exchange/api/exchange/guides/getting-started/account-setup)
- [Testing](https://docs.xrocket.exchange/api/exchange/guides/getting-started/testing)
- [Entities](https://docs.xrocket.exchange/api/exchange/guides/getting-started/entities)
- [Error handling](https://docs.xrocket.exchange/api/exchange/guides/error-handling)
- [WebSocket](https://docs.xrocket.exchange/api/exchange/websocket) и страницы каналов
- Справка по спотовой торговле: [Exchange Trading](https://docs.xrocket.exchange/help/exchange-trading)

Старый Trade API v1 (`https://trade.xrocket.exchange/api`) в доках помечен как deprecated и скоро будет удалён. Его не используем.

## Прямые ответы

| Вопрос | Ответ |
| --- | --- |
| Есть ли testnet? | Да. Отдельный REST, отдельный WebSocket, отдельный бот `@xrocket_testnet_bot`, отдельные токены и тестовые средства. Mainnet-токен на testnet не работает. |
| Какой режим по умолчанию? | Testnet существует, поэтому дефолт API — **testnet**, как в ТЗ. PAPER остаётся режимом исполнения бота (без ордеров на биржу), а не заменой testnet. |
| Есть ли client order id / идемпотентность? | Поле `clientOrderId` есть. Отдельного заголовка Idempotency-Key **NOT DOCUMENTED**. Повтор с тем же id — это ошибка 400, а не возврат старого ордера. |
| Рыночные ордера? | Да. `type=market`. |
| Шорт / маржа? | **NOT DOCUMENTED**. В API только `side=buy\|sell`. В справке биржа описана как spot. Позиций, плеча и margin-эндпоинтов в списке методов нет. |
| Свечи? | Да. REST `GET /api/v1/candles` и WS-канал `candles`. Интервал `15min` есть. Порядок полей не OHLCV. |

## Базовые URL

| | Mainnet | Testnet |
| --- | --- | --- |
| REST | `https://exchange.api.xrocket.exchange` | `https://exchange.api.testnet.xrocket.exchange` |
| WebSocket | `wss://exchange.app-api.xrocket.exchange/` | `wss://exchange.app-api.testnet.xrocket.exchange/` |
| Telegram | `@xRocket` | `@xrocket_testnet_bot` |

Токен: Menu → Settings → Exchange settings → API token. Новый токен сразу гасит старый.

Тестовые монеты: `@testgiver_ton_bot` или [faucet Chainstack](https://faucet.chainstack.com/ton-testnet-faucet). В доках это «Testnet TON, Testnet USDT».

У кошелька два баланса: **funding** (основной кошелёк) и **trading** (биржа). Ордер можно поставить только с trading-баланса. Перевод между ними — отдельные методы Transfer. Автовывод в боте запрещён ТЗ; методы вывода в API есть, бот их не вызывает.

## Аутентификация

Публичные методы (рынок) — без токена.

Приватные методы:

```
Authorization: Bearer <YOUR_API_TOKEN>
Accept: application/json
```

POST ещё требует `Content-Type: application/json`.

Ограничить права токена нельзя: он умеет торговлю, переводы и вывод. В логи его писать нельзя.

WebSocket: сразу после connect метод `auth`:

```json
{ "id": "1", "method": "auth", "params": { "token": "<YOUR_API_TOKEN>" } }
```

Успех: `{ "id": "1", "result": { "success": true } }`.

Публичные каналы auth не требуют. Приватные (`balances`, `activeOrders`) требуют.

## Общие типы

Деньги и количества в REST — **строки**, не числа (`"123.456"`).

Сторона ордера: `buy` | `sell`.

Тип ордера: `limit` | `market` | `stopLimit` | `stopMarket`.

Статус ордера: `working` | `rejected` | `cancelled` | `completed` | `expired` | `pending` | `sending`.

Time in force: `GTC` | `IOC` | `FOK`.

`clientOrderId`: необязательное поле, строка 1–64, только буквы, цифры, `_` и `-`. В ответе тоже необязательное.

Ошибка — JSON:

| Поле | Смысл |
| --- | --- |
| `type` | URI, например `/api/problems/client_data_validation` |
| `title` | короткий заголовок |
| `status` | HTTP-код |
| `detail` | текст |
| `instance` | id экземпляра ошибки |
| `kind` | категория |
| `info` | доп. поля, не у всех ошибок |

### Объект ордера в ответах

Дискриминатор — `type`. Общие поля (обязательные, если не сказано иное):

`id`, `clientOrderId` (не обязателен), `symbol`, `side`, `status`, `createdAt`, `updatedAt`, `dealSize` (исполнено в базовом активе), `dealFunds` (исполнено в котируемом), `fee`, `feeAsset`, `remark` (не обязателен), `timeInForce`, `type`.

Дальше по типу:

| type | Дополнительно |
| --- | --- |
| `limit` | `size`, `funds`, `price` обязательны |
| `market` | `size` и `funds` есть, но не в required |
| `stopLimit` | `size`, `funds`, `price`, `stopTriggered`, `stopPrice` |
| `stopMarket` | `size`, `stopTriggered`, `stopPrice` |

Поля `averagePrice` / `filledQuantity` **NOT DOCUMENTED**. Исполненный объём — `dealSize`. Среднюю цену можно посчитать как `dealFunds / dealSize`, если `dealSize` не ноль. Это наша интерпретация, не поле API.

`POST /api/v1/orders` и `POST /api/v1/orders/estimate` отвечают **201**, не 200.

## REST

Префикс пути: `/api/v1`. Ниже — то, что нужно боту. Выводы и история переводов в торговый контур не входят; в обзоре они есть (`Create withdrawal`, `Get withdrawals`, `Transfer`, `Get transfers`), бот их не вызывает.

### Рынок

#### `GET /api/v1/symbols` — публичный

Ответ 200, `SymbolsResponse`:

`symbols[]`: `symbol`, `baseAsset`, `quoteAsset`, `baseMinSize`, `quoteMinSize`, `baseMaxSize`, `quoteMaxSize`, `minPrice`, `maxPrice`, `baseIncrement`, `priceIncrement`, `enableTrading` (bool), `openTime` (не обязателен), `precisions[]` (шаги стакана), `labels[]` (`new`, `xrock`, `contest`, `featured`, `delisting_warning`, `delisting`).

`baseIncrement` — шаг количества базового актива. `priceIncrement` — шаг цены. Это min size и precision из ТЗ.

Ошибки: 500 `internal_error`.

#### `GET /api/v1/symbols/{symbol}` — публичный

Path: `symbol` (пример `BTC-USDT`).

Ответ 200 — один объект с теми же полями, что элемент списка.

Ошибки: 400 `exchange_symbol_not_available_for_trading`, `exchange_symbol_incorrect`; 404 `exchange_symbol_not_found`; 500.

#### `GET /api/v1/ticker/{tickerType}` — публичный

Path: `tickerType`, в схеме единственное значение `24h`.

Query: `symbols` — массив строк, необязательный. Пусто = все пары. Как массив кодируется в query (**csv или повтор ключа**) — **NOT DOCUMENTED**.

Ответ 200, `tickers[]`: `symbol`, `startTime`, `endTime`, `open`, `close`, `high`, `low`, `changeRate`, `changePrice`, `baseVolume`, `quoteVolume`, `last`. Все строки, все обязательны.

Других `tickerType`, кроме `24h`, в REST-схеме нет. Свечи 15m берутся из candles, не из ticker.

Ошибки: 400 `client_data_validation`; 404 `exchange_symbol_not_found`; 500.

#### `GET /api/v1/candles` — публичный

Query, все обязательные:

| Параметр | Значения |
| --- | --- |
| `symbol` | пара, пример `BTC-USDT` |
| `type` | `1min`, `5min`, `15min`, `30min`, `1hour`, `2hour`, `4hour`, `8hour`, `12hour`, `1day`, `1week`, `1month` |
| `startAt`, `endAt` | ISO 8601 |

Ответ 200: `candles` — массив массивов строк:

`[start, open, close, high, low, baseVolume]`

Это **не** обычный OHLCV: high и low стоят после close. Quote volume в свече **NOT DOCUMENTED**.

Слишком широкий интервал: 400 `exchange_too_big_interval_validation`, в `info.maxIntervalInSeconds` приходит число. Фиксированного лимита в доках нет.

Другие 400: `client_data_validation`, `exchange_symbol_not_opened`, `exchange_symbol_not_available_for_trading`, `exchange_symbol_incorrect`. Ещё 403 `user_blocked`, 404 `exchange_symbol_not_found`, 500.

#### `GET /api/v1/orderbook` — публичный

Query: `symbol` обязателен; `depth` необязателен, enum `5|10|20|50|100|200|500`; `precision` необязателен (пример `0.01`, допустимые шаги пары — в `precisions` символа).

Ответ 200: `sequence` (string), `bids` и `asks` как `[[price, size], ...]`, `askTotalAmount`, `bidTotalAmount`.

Ошибки: 400 `client_data_validation`, `exchange_symbol_incorrect`, `exchange_symbol_not_opened`, `exchange_incorrect_precision` (в `info` детали precision); 404 `exchange_symbol_not_found`; 500.

#### `GET /api/v1/trades` — публичный

Query: `symbol` обязателен.

Ответ 200, `trades[]`: `tradeId`, `price`, `side` (`buy|sell`), `time`, `size`.

Ошибки: 400 `client_data_validation`, `exchange_symbol_incorrect`, `exchange_symbol_not_opened`; 403 `user_blocked`; 404 `exchange_symbol_not_found`; 500.

#### `GET /api/v1/trade-fees`

В обзоре метод указан у аккаунта. В схеме метода заголовок Authorization на странице не расписан отдельно; рядом стоят 403 `user_blocked`. Считаем метод приватным, пока живой вызов без токена не проверялся. Это не выдуманный путь: `GET /api/v1/trade-fees`.

Query: `symbols` — необязательный массив. Пусто = все пары. Кодировка массива — **NOT DOCUMENTED**.

Ответ 200, `fees[]`: `symbol`, `standard.taker`, `standard.maker`. Пример ставки `"0.01"`. Доки называют это fee rate. Что именно значит `0.01` (1% или 0.01%) — **NOT DOCUMENTED**. Для сравнения: у тикера `changeRate` `"0.024"` прямо назван долей +2.4%. У комиссии такой фразы нет.

Ошибки: 400 `client_data_validation`, `exchange_symbol_not_opened`; 403; 404 `exchange_symbol_not_found`; 500.

### Балансы

#### `GET /api/v1/accounts/trading/balances` — Bearer

Ответ 200, `balances[]`: `asset`, `balance` (всего), `available`, `holds` (в ордерах). Все строки, все обязательны.

Ошибки: 403 `user_blocked`; 500.

#### `GET /api/v1/accounts/funding/balances` — Bearer

Та же форма ответа (`asset`, `balance`, `available`, `holds`). Это не торговый баланс. Ошибки: 403, 500.

Эндпоинта «позиции» **NOT DOCUMENTED**. Для spot позиция — это учёт бота, не объект биржи.

### Ордера

#### `POST /api/v1/orders` — Bearer, тело JSON, ответ 201

Тело — `oneOf` четырёх запросов.

Общее: `symbol`, `side`, `type`, `clientOrderId` (не обязателен, 1–64, `[A-Za-z0-9_-]`).

| type | Обязательные поля | Прочее |
| --- | --- | --- |
| `limit` | `symbol`, `side`, `type`, `size`, `price`, `timeInForce` (`GTC\|IOC\|FOK`) | |
| `market` | `symbol`, `side`, `type`, `timeInForce` (`IOC\|FOK` только) | `size` **или** `funds`, но не оба и не ни одного |
| `stopLimit` | `symbol`, `side`, `type`, `stopPrice`, `size`, `price`, `timeInForce` (`GTC\|IOC\|FOK`) | |
| `stopMarket` | `symbol`, `side`, `type`, `stopPrice`, `size`, `timeInForce` (`IOC\|FOK`) | |

Ответ 201 — объект ордера (см. выше).

Ошибки 400:

- `client_data_validation`
- `exchange_symbol_not_available_for_trading`
- `exchange_amount_more_than_user_balance`
- `exchange_incorrect_decimal_places` (`info.maxDecimals`, `info.field`)
- `exchange_asset_not_available`
- `exchange_client_order_id_duplicate` — такой `clientOrderId` уже есть. Тела с id старого ордера в схеме нет
- `exchange_symbol_incorrect`
- `exchange_symbol_not_opened`
- `order_size_and_funds_missing`
- `order_size_and_funds_mutual_exclusion`
- `exchange_insufficient_liquidity`
- `exchange_price_out_of_range` (`info.min`, `info.max`)
- `exchange_stop_price_out_of_range`
- `exchange_size_out_of_range`
- `exchange_funds_out_of_range`

Ещё 403 `user_blocked`; 404 `asset_not_found`, `exchange_symbol_not_found`; 500.

Идемпотентность: поля `clientOrderId` достаточно, чтобы **найти** ордер (`GET /api/v1/order?clientOrderId=`), но повторный POST с тем же id не является безопасным replay. При потерянном ответе CREATE повторять нельзя: сначала поиск.

#### `POST /api/v1/orders/estimate` — Bearer, ответ 201

Тело такое же, как у place. Ордер не создаётся (по названию метода и по ответу без `id`).

Ответ — оценка: `symbol`, `side`, `fee`, `feeAsset`, `timeInForce`, `type` и поля размера/цены того же типа. `clientOrderId` в ответе оценки нет. У market `size` и `funds` не обязательны.

Ошибки 400 почти как у place, включая duplicate client order id **нет** в списке estimate (duplicate есть только у place). Есть liquidity, price/size/funds range, decimals, balance, symbol.

403, 404 (`exchange_symbol_not_found`, `asset_not_found`), 500.

#### `GET /api/v1/order` — Bearer

Query: `orderId` и/или `clientOrderId`. Хотя бы один обязателен. Если переданы оба, используется `orderId`.

Ответ 200 — объект ордера.

Ошибки: 400 `client_data_validation`, `exchange_missing_order_identifier`; 403; 404 `exchange_order_not_found`; 500.

История не фильтруется по `clientOrderId`. Точечный поиск — этот метод.

#### `GET /api/v1/orders/active` — Bearer

Без query-параметров в схеме.

Ответ 200: `orders[]` — те же четыре формы ордера.

Ошибки: 403, 500.

#### `DELETE /api/v1/order` — Bearer

Query как у get: `orderId` и/или `clientOrderId`, хотя бы один, при обоих берётся `orderId`.

Ответ 200: `cancelledOrderIds: string[]`.

Ошибки: 400 `client_data_validation`, `exchange_missing_order_identifier`; 403; 404 `exchange_order_not_found`; 500.

#### `GET /api/v1/orders/history` — Bearer

Query, все необязательные:

| Параметр | Смысл |
| --- | --- |
| `symbol` | пара |
| `side` | `buy` \| `sell`. В обзоре: не передавать поле, если нужны обе стороны |
| `startAt`, `endAt` | ISO 8601 |
| `currentPage` | default 1 |
| `pageSize` | default 20, maximum 100 |
| `hideCanceled` | default false |

Ответ 200: `orders[]` (четыре формы), `currentPage`, `pageSize`, `totalNum`, `totalPage`.

Ошибки: 400 `client_data_validation`, `exchange_symbol_incorrect`; 403; 404 `exchange_symbol_not_found`; 500.

## WebSocket

Один сокет на окружение (URL выше). Сообщения JSON:

```json
{ "id": "12345", "method": "subscribe", "params": {} }
```

`id` — строка или число, уникален в рамках запросов, чтобы склеить ответ.

Пуш:

```json
{ "method": "subscription", "params": { "channel": "...", "data": {} } }
```

Методы: `ping`, `auth`, `subscribe`, `unsubscribe`, `unsubscribeAll`.

`unsubscribe` — `{ "id", "method": "unsubscribe", "params": { "channel": "channel_name" } }`.

`unsubscribeAll` — без params.

### Ping

Чтобы сервер не закрыл простой через 60 секунд, клиент шлёт ping каждые 30 секунд:

```json
{ "id": "12345", "method": "ping" }
```

Формат ответа на ping (pong) — **NOT DOCUMENTED**. Серверного ping **NOT DOCUMENTED**.

### Каналы

| Канал | Auth | Подписка | Данные |
| --- | --- | --- | --- |
| `ticker` | нет | `symbol`, `interval` (`1min`…`12hour`, `1day`; месяц/неделя в списке тикера нет) | `data.ticker`: те же поля, что REST-тикер |
| `allTickers` | нет | только `interval` | `data.tickers[]` |
| `orderbook` | нет | `symbol`, `depth` (те же 5…500), `precision` | см. ниже |
| `trades` | нет | `symbol` | `data.trades[]`: `tradeId`, `price`, `side`, `time`, `size` |
| `candles` | нет | `symbol`, `type` (те же интервалы, что REST candles, включая `15min`), необязательный `snapshot.startAt/endAt` | см. ниже |
| `balances` | да | `channel=balances` | `data.balances[]`: `asset`, `balance`, `available`, `holds` |
| `activeOrders` | да | `channel=activeOrders` | `data.orders[]` тех же форм, что REST |

Успех подписки обычно `{ "id", "result": { "success": true } }`. У candles и orderbook в первый `result` кладётся ещё и снимок.

Свечи, пуш: `params.data.candle = [start, open, close, high, low, baseVolume]`. Если `snapshot` в подписке не передан, в первом ответе свечей нет, но апдейты идут.

Стакан: первый ответ — полный снимок. Дальше инкременты. Поле `snapshot`: `true` полный, `false` инкремент. Есть `sequence`. Как применять инкремент (абсолютные объёмы или дельта, удаление нулевого размера) — **NOT DOCUMENTED**.

Какой кошелёк у канала `balances` (trading, funding или оба) — **NOT DOCUMENTED**. Для решений по ордерам источник — REST trading balances плюс сверка.

Поток тиков в SQLite не пишем (ТЗ). Свечи 15m для стратегии — отдельное хранение закрытых свечей.

## Ошибки

### HTTP (гайд)

| Код | Смысл |
| --- | --- |
| 200 | ок (создание ордера и estimate в OpenAPI — 201) |
| 400 | кривой запрос, символ, валидация |
| 401 | нет или плохой Bearer (гайд; на карточках методов чаще 400/403/404/500, отдельной схемы 401 там нет) |
| 403 | нет прав / пользователь заблокирован |
| 404 | нет ресурса |
| 429 | rate limit |
| 500 | внутренняя ошибка |
| 503 | обслуживание или перегрузка (гайд; на карточках методов не расписан) |

### WebSocket (JSON-RPC)

| Код | Смысл |
| --- | --- |
| -32700 | parse error |
| -32600 | invalid request |
| -32601 | method not found |
| -32602 | invalid params |
| -32603 | internal error |
| -32000 | неверный шаг десятичных |
| -32001 | ордер не найден |
| -32002 | уже подписан |
| -32003 | не подписан |
| -32005 | символ не найден |
| -32006 | символ некорректен |
| -32007 | символ недоступен |
| -32008 | интервал слишком большой |
| -32009 | торги по символу не открыты |
| -32010 | актив недоступен |
| -32011 | неверная precision |
| -32030 | плохой токен |
| -32032 | ошибка валидации |
| -32033 | unauthorized |
| -32034 | пользователь заблокирован |
| -32050 | rate limit |

Форма: `{ "id", "error": { "code", "message", "data": { "detail": "..." } } }`.

Кода -32004 в таблице доков нет. Не добавляем его сами.

## Rate limits

Числовых лимитов (запросов в секунду, веса, отдельных лимитов по ордерам) в доках нет. **NOT DOCUMENTED**.

Что сказано:

- HTTP 429. Гайд пишет, что заголовки подскажут, когда повторить, и приводит пример `Retry-After`. Гарантирован ли именно этот заголовок — формулировка «e.g.», точного контракта нет.
- WS `-32050`.
- При превышении нужно остановиться и сделать backoff. Юридический текст: ключ могут зажать без предупреждения.

Пока лимиты не опубликованы, клиент бота должен: уважать 429 и `Retry-After`, если он пришёл; не слать пачку ордеров; не крутить REST там, где хватает WS.

## Что это меняет для бота

Решения заказчика на этап 3 записаны в `docs/CLIENT_DECISIONS.md` и в `core/trading_policy.py`. Кратко:

1. Testnet есть. `XROCKET_ENV` по умолчанию `testnet`. Режим исполнения по умолчанию `paper`: ордера на биржу не уходят, пока режим не сменят и не включат флаг вручную.
2. На каждый ордер генерируем `clientOrderId` и храним его до запроса. Если ответ потерян — не POST повторно, а `GET /api/v1/order?clientOrderId=`. Дубликат `exchange_client_order_id_duplicate` тоже значит «сначала найти», не «создать ещё раз».
3. Рыночный вход: `type=market`, `timeInForce=IOC`, сумма в `funds` (USDT). `size` в этом запросе не передаём. Рыночный выход: `side=sell`, `size` — точное количество базы, вниз до `baseIncrement`, `timeInForce=IOC`. `funds` на выходе не передаём.
4. Шорт и маржа в API не описаны. MVP LONG на spot этому не противоречит. Продать можно только то, что есть на trading-балансе (`side=sell`).
5. Индикаторы 15m строим по `GET /api/v1/candles?type=15min` и WS `candles`. Парсер читает порядок `[start, open, close, high, low, baseVolume]`.
6. REST-тикер — только `24h`. Для сигнала он не заменяет закрытую свечу.
7. Min size / precision / min-max — из `GET /api/v1/symbols`. Комиссия `0.01` по решению читается как доля (1%), флаг `FEE_RATE_IS_FRACTION`. Единицу всё ещё нужно подтвердить крошечным ордером на testnet позже.
8. Средняя цена и filled quantity в ответе не называются так. Маппинг: `filled_quantity = dealSize`, `average_price = dealFunds/dealSize`, `fee = fee`.
9. Баланс для риска — trading `available`. Бот сам не переводит funding → trading. Если на trading не хватает, только предупреждение. Вывод не делаем.
10. Числовой rate limit неизвестен. Клиентский потолок по умолчанию 2 запроса в секунду — это наш запас, не лимит биржи. 429 и `Retry-After` уважаем. POST создания ордера при 429, таймауте, обрыве и 5xx не повторяем.

## Что осталось неясным

Решения этапа 4 закрыли выход, закрытие свечи, округление `funds`, комиссию и источник стакана. Пробелы документации биржи ниже код по-прежнему не додумывает.

- Как кодировать массив `symbols` в query (csv или повтор ключа) — **NOT DOCUMENTED**. Клиент шлёт повтор ключа.
- Шаг округления `funds` / quote — **NOT DOCUMENTED**. По решению этапа 4 пол остаётся от масштаба `quoteMinSize`.
- Как применять инкремент стакана (абсолютный объём или дельта, удаление нуля) — **NOT DOCUMENTED**. Для решений берётся полный снимок REST. Инкремент книгу не меняет и помечает её ненадёжной.
- Какой кошелёк у WS `balances` — **NOT DOCUMENTED**. Для решений берётся trading-баланс REST. В paper своего токена нет, поэтому бумажный капитал считается локально.
- Флага «свеча закрыта» нет — **NOT DOCUMENTED**. Для стратегии свеча закрыта, когда прошло `start + timeframe + grace` (по умолчанию 5 секунд) и эта свеча есть в ответе REST. Следующее сообщение сокета не ждём. Незакрытую свечу не используем.
- Форма ответа на ping и серверный ping — **NOT DOCUMENTED**.
- Как часто биржа пушит рыночные данные — **NOT DOCUMENTED**. Порог устаревания 90 секунд — наша настройка.
- Числовой RPS и обязательность заголовка `Retry-After` — **NOT DOCUMENTED**.
- Единица fee rate — **NOT DOCUMENTED**. Решение «0.01 = 1%» нужно проверить крошечным testnet-ордером. Этот этап ордер не ставит.
- `endAt` у свечей включительный или нет — **NOT DOCUMENTED**. Страницы режутся по `maxIntervalInSeconds`, дубликаты схлопываются по времени открытия. На testnet это поле пришло как `259200000` при лимите ровно 3 суток, то есть как миллисекунды, хотя имя говорит «секунды». Если сырое число не уменьшает отклонённое окно и делится на 1000, клиент делит его на 1000.
