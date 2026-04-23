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

    # ── Multi-leg options (verticals + iron condor) ──────────────────────────
    def supports_multi_leg(self) -> bool:
        return False

    def submit_multi_leg_order(
        self,
        legs: list[dict],          # [{"contract_symbol", "side" ("buy"|"sell"), "position_intent" ("open"|"close")}, ...]
        qty: int,                  # # of spread units (always positive — direction encoded in order_side)
        net_limit_price: float,    # net debit (positive number, always)
        order_side: str,           # "buy" = net debit / "sell" = net credit
        strategy: str = "",        # for logging
        regime_name: str = "",
    ) -> Optional[str]:
        raise NotImplementedError("Broker does not support multi-leg orders")

    # ── Account history (for dashboard perf charts) ──────────────────────────
    def get_portfolio_history(self, period: str, timeframe: str) -> dict:
        """Return {timestamps, equity, profit_loss, profit_loss_pct, base_value}.

        period: "1D" | "1W" | "1M" | "3M" | "6M" | "1A" | "all"
        timeframe: "1Min" | "5Min" | "15Min" | "1H" | "1D"
        """
        raise NotImplementedError("Broker does not expose portfolio history")

    # ── Order lifecycle (stale cleanup + fill polling) ───────────────────────
    def cancel_stale_orders(self, max_age_seconds: int = 180) -> int:
        return 0

    def wait_for_order_fill(self, order_id: str, timeout_sec: float = 8.0, poll_sec: float = 0.5) -> str:
        return "unknown"

    def get_account_baseline(self) -> float:
        """Authoritative peak-equity for drawdown calc.

        Should return max(starting_capital, historical_max_equity) so that
        the bot's drawdown is always measured against a real high-water
        mark (including post-deposit spikes), never against a snapshot the
        bot happened to take while already drawn-down.
        """
        raise NotImplementedError("Broker does not expose account baseline")
