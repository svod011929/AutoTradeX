"""Startup recovery order and shutdown order from the spec.

Startup: exchange, balance, active orders, reconciliation, positions, websocket, then trading.
Shutdown: block new entries, stop the engine, stop the socket, stop notifications, close the database.
Open positions are left open.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

Step = Callable[[], Awaitable[None]]


@dataclass
class StartupPlan:
    prepare_exchange: Step
    load_balance: Step
    load_active_orders: Step
    reconcile: Step
    load_positions: Step
    start_websocket: Step
    allow_trading: Step
    trace: list[str] = field(default_factory=list)

    async def run(self) -> list[str]:
        ordered = (
            ("exchange", self.prepare_exchange),
            ("balance", self.load_balance),
            ("active_orders", self.load_active_orders),
            ("reconciliation", self.reconcile),
            ("positions", self.load_positions),
            ("websocket", self.start_websocket),
            ("trading", self.allow_trading),
        )
        for name, step in ordered:
            await step()
            self.trace.append(name)
        return list(self.trace)


@dataclass
class ShutdownPlan:
    block_new_entries: Step
    stop_engine: Step
    stop_websocket: Step
    stop_notifications: Step
    close_database: Step
    trace: list[str] = field(default_factory=list)
    positions_closed: bool = False

    async def run(self) -> list[str]:
        ordered = (
            ("block_entries", self.block_new_entries),
            ("engine", self.stop_engine),
            ("websocket", self.stop_websocket),
            ("notifications", self.stop_notifications),
            ("database", self.close_database),
        )
        for name, step in ordered:
            await step()
            self.trace.append(name)
        return list(self.trace)
