import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from typing import Dict, Optional
from pathlib import Path
from config.settings import STATE_FILE
from monitoring.logger import get_logger

logger = get_logger("position_tracker")


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    entry_date: str
    stop_loss: float
    take_profit: float
    regime_at_entry: str = "unknown"

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.entry_price

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity

    def unrealized_pnl_pct(self, current_price: float) -> float:
        return (current_price - self.entry_price) / self.entry_price


@dataclass
class OptionsPosition:
    """A single-leg long option position. entry_premium is per-contract mid in $,
    so total cost = entry_premium * qty * 100."""
    contract_symbol: str    # OCC: SPY250620C00450000
    underlying: str
    side: str               # "call" | "put"
    strike: float
    expiry: str             # ISO date YYYY-MM-DD
    qty: int                # signed: +N = long, -N = short (covered calls, future spreads)
    entry_premium: float    # per-contract mid at entry (dollars)
    entry_date: str
    regime_at_entry: str = "unknown"
    strategy: str = "long_call"   # long_call | long_put | short_call_covered | (future: spreads)

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def cost_basis(self) -> float:
        # Absolute capital at risk / collateral in play.
        return abs(self.entry_premium * self.qty * 100)

    def unrealized_pnl(self, current_premium: float) -> float:
        # Signed qty handles direction automatically.
        # Long (+5), current>entry → positive ✓
        # Short (-5), current<entry → (neg)*(neg) = positive ✓
        return (current_premium - self.entry_premium) * self.qty * 100

    def unrealized_pnl_pct(self, current_premium: float) -> float:
        if self.entry_premium <= 0 or self.qty == 0:
            return 0.0
        sign = 1 if self.qty > 0 else -1
        return sign * (current_premium - self.entry_premium) / self.entry_premium

    def dte(self) -> int:
        from datetime import date as _d
        try:
            exp = _d.fromisoformat(self.expiry)
            return (exp - _d.today()).days
        except Exception:
            return 0


@dataclass
class BotState:
    positions: Dict[str, Position] = field(default_factory=dict)
    options_positions: Dict[str, OptionsPosition] = field(default_factory=dict)
    daily_spent: float = 0.0                  # stock-buy notional today
    options_daily_spent: float = 0.0          # option-premium outlay today
    daily_date: str = ""
    peak_equity: float = 0.0
    is_halved: bool = False         # True when daily loss triggered halving
    total_realized_pnl: float = 0.0
    options_realized_pnl: float = 0.0
    cycles: int = 0                 # Number of completed main-loop iterations

    def reset_daily_if_new_day(self):
        today = date.today().isoformat()
        if self.daily_date != today:
            self.daily_spent = 0.0
            self.options_daily_spent = 0.0
            self.daily_date = today
            self.is_halved = False
            logger.info("Daily counters reset for new trading day.")

    def save(self):
        data = {
            "positions": {sym: asdict(p) for sym, p in self.positions.items()},
            "options_positions": {k: asdict(p) for k, p in self.options_positions.items()},
            "daily_spent": self.daily_spent,
            "options_daily_spent": self.options_daily_spent,
            "daily_date": self.daily_date,
            "peak_equity": self.peak_equity,
            "is_halved": self.is_halved,
            "total_realized_pnl": self.total_realized_pnl,
            "options_realized_pnl": self.options_realized_pnl,
            "cycles": self.cycles,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls) -> "BotState":
        if not Path(STATE_FILE).exists():
            return cls()
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            state = cls()
            state.daily_spent = data.get("daily_spent", 0.0)
            state.options_daily_spent = data.get("options_daily_spent", 0.0)
            state.daily_date = data.get("daily_date", "")
            state.peak_equity = data.get("peak_equity", 0.0)
            state.is_halved = data.get("is_halved", False)
            state.total_realized_pnl = data.get("total_realized_pnl", 0.0)
            state.options_realized_pnl = data.get("options_realized_pnl", 0.0)
            state.cycles = data.get("cycles", 0)
            for sym, pdata in data.get("positions", {}).items():
                state.positions[sym] = Position(**pdata)
            for key, odata in data.get("options_positions", {}).items():
                state.options_positions[key] = OptionsPosition(**odata)
            return state
        except Exception as e:
            logger.error(f"Failed to load state: {e}. Starting fresh.")
            return cls()

    def close_position(self, symbol: str, exit_price: float):
        pos = self.positions.pop(symbol, None)
        if pos:
            pnl = pos.unrealized_pnl(exit_price)
            self.total_realized_pnl += pnl
            self.save()
            return pnl
        return 0.0

    def close_option_position(self, contract_symbol: str, exit_premium: float):
        pos = self.options_positions.pop(contract_symbol, None)
        if pos:
            pnl = pos.unrealized_pnl(exit_premium)
            self.options_realized_pnl += pnl
            self.save()
            return pnl
        return 0.0
