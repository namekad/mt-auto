from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.services.mt5_service import OrderResult, Quote
    from app.trading.models import Direction


@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    sl: float
    tp: float
    volume: float
    comment: str
    magic: int


class SetupBroker(Protocol):
    verified: bool

    def quote(self, symbol: str) -> Quote | None: ...

    def list_tracked_positions(self) -> list[BrokerPosition]: ...

    def history_contains(self, ticket: int) -> bool: ...

    def open_market_leg(
        self,
        *,
        symbol: str,
        direction: Direction,
        volume: float,
        stop_loss: float,
        take_profit: float,
        comment: str,
    ) -> OrderResult: ...

    def modify_stop_loss(self, ticket: int, stop_loss: float) -> OrderResult: ...

    def close_position(self, ticket: int) -> OrderResult: ...
