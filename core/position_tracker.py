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
    strategy: str = "long_call"   # long_call | long_put | short_call_covered | intraday_orb | forced_paper
    intraday: bool = False        # True → force-close at EOD flatten window

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
class OptionLeg:
    """One leg of a multi-leg options position."""
    contract_symbol: str   # OCC: O:SPY260619P00700000
    side: str              # "long" | "short"
    contract_type: str     # "call" | "put"
    strike: float
    expiry: str            # ISO date
    entry_price: float     # per-contract mid at entry
    ratio_qty: int = 1     # # of this leg per spread unit (always 1 for verticals & condors)


@dataclass
class MultiLegPosition:
    """
    A vertical spread (2 legs) or iron condor (4 legs).

    net_entry is absolute per-unit dollars; `qty > 0` = long (paid the debit),
    `qty < 0` = short (received the credit). Both are tracked as positive
    dollar amounts — direction-aware math lives in unrealized_pnl + exit rules.
    """
    key: str                      # unique ID per position
    strategy: str                 # bull_put_credit | bear_call_credit | bull_call_debit | bear_put_debit | iron_condor
    underlying: str
    legs: list                    # list of OptionLeg
    qty: int                      # +N = long spread (debit paid), -N = short spread (credit received)
    net_entry: float              # positive dollars per unit (|net debit| or |net credit|)
    entry_date: str
    regime_at_entry: str = "unknown"

    @property
    def is_credit(self) -> bool:
        return self.qty < 0

    @property
    def unit_count(self) -> int:
        return abs(self.qty)

    @property
    def width(self) -> float:
        """Strike distance between short and long legs. For iron condor: max wing width."""
        if not self.legs:
            return 0.0
        if len(self.legs) == 2:
            return abs(self.legs[0].strike - self.legs[1].strike)
        # Iron condor: compute max of put wing and call wing
        calls = [l for l in self.legs if l.contract_type == "call"]
        puts  = [l for l in self.legs if l.contract_type == "put"]
        call_w = abs(calls[0].strike - calls[1].strike) if len(calls) == 2 else 0.0
        put_w  = abs(puts[0].strike - puts[1].strike) if len(puts) == 2 else 0.0
        return max(call_w, put_w)

    @property
    def max_profit(self) -> float:
        """Per unit, in dollars (before × 100 and × qty)."""
        if self.is_credit:
            return self.net_entry
        # Debit spread max profit = width - debit (assuming no black-swan)
        return max(self.width - self.net_entry, 0.0)

    @property
    def max_loss(self) -> float:
        """Per unit, in dollars (before × 100 and × qty)."""
        if self.is_credit:
            return max(self.width - self.net_entry, 0.0)
        # Debit spread max loss = debit paid
        return self.net_entry

    @property
    def capital_at_risk(self) -> float:
        """Total $ the position can lose. Governs risk-approval sizing."""
        return self.max_loss * self.unit_count * 100

    def unrealized_pnl(self, current_net_value: float) -> float:
        """current_net_value: current per-unit absolute mid cost-to-close in dollars.

        Debit (qty>0): we own the spread worth $current → pnl = (current - entry) * qty * 100.
        Credit (qty<0): we sold the spread; to close we pay $current → pnl = (entry - current) * |qty| * 100.
        Unified with signed qty: pnl = (current - entry) * qty * 100.
        """
        return (current_net_value - self.net_entry) * self.qty * 100

    def dte(self) -> int:
        from datetime import date as _d
        if not self.legs:
            return 0
        try:
            # All legs share expiry for verticals/condors
            exp = _d.fromisoformat(self.legs[0].expiry)
            return (exp - _d.today()).days
        except Exception:
            return 0


@dataclass
class BotState:
    positions: Dict[str, Position] = field(default_factory=dict)
    options_positions: Dict[str, OptionsPosition] = field(default_factory=dict)
    multi_leg_positions: Dict[str, "MultiLegPosition"] = field(default_factory=dict)
    daily_spent: float = 0.0                  # stock-buy notional today
    options_daily_spent: float = 0.0          # option-premium outlay today
    intraday_daily_spent: float = 0.0         # intraday option premium today
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
            self.intraday_daily_spent = 0.0
            self.daily_date = today
            self.is_halved = False
            logger.info("Daily counters reset for new trading day.")

    def save(self):
        data = {
            "positions": {sym: asdict(p) for sym, p in self.positions.items()},
            "options_positions": {k: asdict(p) for k, p in self.options_positions.items()},
            "multi_leg_positions": {k: asdict(p) for k, p in self.multi_leg_positions.items()},
            "daily_spent": self.daily_spent,
            "options_daily_spent": self.options_daily_spent,
            "intraday_daily_spent": self.intraday_daily_spent,
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
            state.intraday_daily_spent = data.get("intraday_daily_spent", 0.0)
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
            for key, mdata in data.get("multi_leg_positions", {}).items():
                # Rebuild leg dataclasses from dict form
                legs = [OptionLeg(**lg) for lg in mdata.pop("legs", [])]
                state.multi_leg_positions[key] = MultiLegPosition(legs=legs, **mdata)
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

    def close_multi_leg_position(self, key: str, exit_net_value: float):
        pos = self.multi_leg_positions.pop(key, None)
        if pos:
            pnl = pos.unrealized_pnl(exit_net_value)
            self.options_realized_pnl += pnl
            self.save()
            return pnl
        return 0.0
