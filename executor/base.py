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

    # ── Options (optional — raise NotImplementedError when unsupported) ────────
    #
    # Broker implementations that don't support options (e.g. Robinhood via
    # robin_stocks) can leave these as the default. The options execute loop
    # gates on supports_options().

    def supports_options(self) -> bool:
        return False

    def buy_option(self, contract_symbol: str, qty: int, limit_price: float,
                   regime_name: str = "") -> Optional[str]:
        raise NotImplementedError("Broker does not support options")

    def sell_option(self, contract_symbol: str, qty: int, limit_price: float,
                    reason: str = "", regime_name: str = "") -> Optional[str]:
        raise NotImplementedError("Broker does not support options")

    def get_option_positions(self) -> list[dict]:
        return []

    def get_stock_positions(self) -> dict[str, float]:
        return self.get_open_positions()
