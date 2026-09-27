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

Что в документации биржи всё ещё неясно, собрано в конце `docs/XROCKET_API.md`.
