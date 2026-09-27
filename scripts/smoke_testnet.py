"""Read-only testnet check.

Reads XROCKET_API_TOKEN from the environment and calls symbols, candles, and
both balance endpoints. It does not place, cancel, or estimate orders.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from core.config import Settings
from xrocket.rest_client import XRocketRestClient


async def _run() -> int:
    load_dotenv()
    token = os.environ.get("XROCKET_API_TOKEN", "").strip()
    if token == "":
        print("XROCKET_API_TOKEN is not set", file=sys.stderr)
        return 2
    settings = Settings()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=2)
    async with XRocketRestClient.from_settings(settings, token=token) as client:
        symbols = await client.get_symbols()
        symbol = symbols[0].symbol if symbols else "BTC-USDT"
        candles = await client.get_candles(symbol, "15min", start, end)
        trading = await client.get_trading_balances()
        funding = await client.get_funding_balances()
    print(
        f"symbols={len(symbols)} candles={len(candles)} "
        f"trading_assets={len(trading)} funding_assets={len(funding)}"
    )
    return 0


def main() -> None:
    try:
        code = asyncio.run(_run())
    except Exception as exc:
        print(f"smoke failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
