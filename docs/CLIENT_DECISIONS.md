# Решения заказчика (этап 3)

Эти правила уже в конфиге. Этап 4 (бумажная торговля и стратегия) берёт их отсюда, а не заново.

| Решение | Где лежит |
| --- | --- |
| Рыночный вход — сумма в `funds` (USDT), `timeInForce=IOC` | `MARKET_ENTRY_FIELD`, `MARKET_TIME_IN_FORCE` в `core/trading_policy.py` |
| На каждый ордер свой `clientOrderId`, он сохраняется до запроса | `generate_client_order_id()`, колонка `orders.client_order_id` |
| Ставка `"0.01"` читается как доля, то есть 1%. Можно переключить | `FEE_RATE_IS_FRACTION=true` |
| Funding сам в trading не переводится. Не хватает trading-баланса — только предупреждение | `AUTO_TRANSFER_FUNDING_TO_TRADING = False` |
| `atr_sl_multiplier=2.0`, `trailing_activation_atr=1.5`, `trailing_atr_multiplier=1.5`, `min_volume_ratio=1.0` | настройки и колонки `strategy_settings` |
| `take_profit_rr=2.0` | то же |
| Режим исполнения по умолчанию PAPER. Реальные ордера testnet только если режим сменить и включить флаг вручную | `EXECUTION_MODE=paper`, `ALLOW_EXCHANGE_ORDERS=false` |

Проверка единицы комиссии крошечным ордером на testnet — отдельный шаг позже. Скрипт `python -m scripts.smoke_testnet` ордера не ставит.

## Этап 4

| Решение | Где лежит |
| --- | --- |
| Выход из позиции — рыночный SELL, `size` равен базе на руках, вниз до `baseIncrement`. `funds` на выходе нет | `MARKET_EXIT_FIELD`, `create_market_exit` |
| Свеча закрыта по времени: старт + таймфрейм + пауза (по умолчанию 5 с), затем свеча должна быть в REST. Следующее WS-сообщение не ждём. Незакрытую свечу не используем | `CANDLE_CLOSE_GRACE_SECONDS`, `candle_close_grace_seconds` |
| `funds` по-прежнему округляем вниз до точности `quoteMinSize` | `floor_funds` |
| Комиссия по умолчанию 1%, настройка остаётся. Проверка на testnet позже | `DEFAULT_FEE_RATE`, `default_fee_rate` |
| Решение по спреду, ликвидности и проскальзыванию — по полному снимку стакана. Баланс для живой торговли — trading по REST | `USE_FULL_ORDERBOOK_SNAPSHOT`, `USE_REST_TRADING_BALANCE` |

В paper баланс trading по REST недоступен без токена. Бумажный капитал считается локально от `PAPER_STARTING_EQUITY` и хранится в `bot_settings.paper_cash`. Свечи и стакан в paper берутся с публичного REST, токен не нужен.

## Этап 5

Пороги этапа 4 оставлены как есть. ATR по-прежнему без верхней границы.

| Решение | Где лежит |
| --- | --- |
| RSI для входа строго между 30 и 70 | `RSI_ENTRY_MIN`, `RSI_ENTRY_MAX` |
| Период средней объёма 20 | `VOLUME_MA_PERIOD` |
| Максимальный спред 0.5% | `MAX_SPREAD_FRACTION` |
| Проскальзывание по стакану 0.3% | `MAX_SLIPPAGE_FRACTION` |
| Дополнительное бумажное проскальзывание 0.1% | `PAPER_SLIPPAGE_FRACTION` |
| Резерв кэша 2% | `CASH_RESERVE_FRACTION` |
| ATR без верхней границы | `ATR_HAS_UPPER_BOUND = False` |
| Бумажная комиссия 1%, пока крошечный ордер на testnet не подтвердит единицу | `DEFAULT_FEE_RATE` |
| На paper и testnet пары по умолчанию BTC-USDT и ETH-USDT | `PAPER_DEFAULT_SYMBOLS`, `TESTNET_DEFAULT_SYMBOLS` |
| На mainnet пара по умолчанию TON-USDT. Перед стартом бот проверяет, что пара есть на бирже. Если её нет — запуск mainnet отменяется с понятным текстом | `MAINNET_DEFAULT_SYMBOLS`, `select_listed_symbols` |
| Пара, которой нет в списке биржи, отбрасывается с предупреждением | `select_listed_symbols` |
| Переход в MAINNET: второе подтверждение и точная фраза `START LIVE`, плюс `XROCKET_ENV=mainnet` | `MAINNET_CONFIRM_PHRASE` |

Пустой `DEFAULT_SYMBOLS` означает «взять пары режима». Явный список в окружении важнее.

Telegram-обработчики читают состояние и ставят команды (пауза, аварийная остановка, просьба закрыть позиции). Сделки считает ядро. Бэктест гоняет ту же стратегию и тот же риск-менеджер по уже закрытым свечам, а исполнение берёт цену открытия следующей свечи.

Что в документации биржи всё ещё неясно, собрано в конце `docs/XROCKET_API.md`.
