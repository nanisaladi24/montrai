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
class BotState:
    positions: Dict[str, Position] = field(default_factory=dict)
    daily_spent: float = 0.0
    daily_date: str = ""
    peak_equity: float = 0.0
    is_halved: bool = False         # True when daily loss triggered halving
    total_realized_pnl: float = 0.0

    def reset_daily_if_new_day(self):
        today = date.today().isoformat()
        if self.daily_date != today:
            self.daily_spent = 0.0
            self.daily_date = today
            self.is_halved = False
            logger.info("Daily counters reset for new trading day.")

    def save(self):
        data = {
            "positions": {sym: asdict(p) for sym, p in self.positions.items()},
            "daily_spent": self.daily_spent,
            "daily_date": self.daily_date,
            "peak_equity": self.peak_equity,
            "is_halved": self.is_halved,
            "total_realized_pnl": self.total_realized_pnl,
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
            state.daily_date = data.get("daily_date", "")
            state.peak_equity = data.get("peak_equity", 0.0)
            state.is_halved = data.get("is_halved", False)
            state.total_realized_pnl = data.get("total_realized_pnl", 0.0)
            for sym, pdata in data.get("positions", {}).items():
                state.positions[sym] = Position(**pdata)
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
