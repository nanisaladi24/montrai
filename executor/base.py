"""Abstract broker interface — all broker implementations must satisfy this."""
from abc import ABC, abstractmethod
from typing import Optional


class BrokerBase(ABC):

    @abstractmethod
    def get_portfolio_value(self) -> float:
        """Total equity (cash + market value of positions)."""

    @abstractmethod
    def get_cash(self) -> float:
        """Available buying power."""

    @abstractmethod
    def buy_notional(self, symbol: str, dollars: float, regime_name: str = "") -> Optional[str]:
        """Buy $dollars worth of symbol. Returns order_id or None on failure."""

    @abstractmethod
    def sell_position(self, symbol: str, current_price: float, reason: str = "", regime_name: str = "") -> Optional[str]:
        """Close the entire position in symbol. Returns order_id or None."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by ID."""

    @abstractmethod
    def get_open_positions(self) -> dict[str, float]:
        """Return {symbol: quantity} for all open positions."""
