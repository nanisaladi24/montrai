"""
Opening Range Breakout (ORB) strategy.

Defines the first `opening_range_min` minutes of the regular session (9:30 ET
onwards) as the "opening range." A bullish breakout is the first close above
OR-high after the range is set; bearish breakout is first close below OR-low.

Breakouts are filtered by the current HMM regime — we only fire bullish breakouts
in bull/neutral/euphoria and bearish breakouts in bear/crash/neutral. The
intraday book flattens at 15:55 ET regardless of signal state.

Data: Polygon minute aggregates (core.polygon_client.fetch_minute_bars).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

import pandas as pd
import pytz

import config.runtime_config as rc
from core.polygon_client import fetch_minute_bars
from core.options_data import get_option_chain, pick_contract
from monitoring.logger import get_logger

logger = get_logger("orb")
_ET = pytz.timezone("America/New_York")

_BULLISH_REGIMES = {"bull", "neutral", "euphoria"}
_BEARISH_REGIMES = {"crash", "bear", "neutral"}


@dataclass
class OpeningRange:
    symbol: str
    high: float
    low: float
    start_ts: pd.Timestamp
    end_ts: pd.Timestamp

    @property
    def width_pct(self) -> float:
        return (self.high - self.low) / self.low if self.low > 0 else 0.0


@dataclass
class ORBPick:
    underlying: str
    direction: str                # "bullish" | "bearish"
    contract_symbol: str
    qty: int
    limit_price: float
    total_cost: float
    orb_high: float
    orb_low: float
    current_price: float
    reasoning: str


def _et_now() -> datetime:
    return datetime.now(_ET)


def is_range_building_window(opening_range_min: int = 15) -> bool:
    """True when we're still inside the opening-range window (9:30 → 9:30 + N)."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return time(9, 30) <= t < time(9, 30 + opening_range_min)


def is_range_tradeable_window(opening_range_min: int = 15,
                              force_close_hour: int = 15,
                              force_close_min: int = 55) -> bool:
    """True when ORB signals are actionable (post-range, pre-EOD-flatten)."""
    now = _et_now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    start = time(9, 30 + opening_range_min)
    end = time(force_close_hour, force_close_min)
    return start <= t < end


def is_force_close_window(force_close_hour: int = 15, force_close_min: int = 55) -> bool:
    """True when EOD flatten must run."""
    now = _et_now()
    if now.weekday() >= 5:
        return True  # defensive — flatten everything on weekends
    return now.time() >= time(force_close_hour, force_close_min)


def compute_opening_range(symbol: str, opening_range_min: int = 15) -> Optional[OpeningRange]:
    """Pull today's 9:30→9:30+N minute bars from Polygon; return the range."""
    df = fetch_minute_bars(symbol, days=1)
    if df.empty:
        logger.warning(f"ORB {symbol}: no minute bars returned")
        return None
    # Bars are UTC-indexed; convert to ET for the 9:30 cutoff comparison
    df_et = df.copy()
    df_et.index = df_et.index.tz_convert(_ET)
    now_et = _et_now()
    today = now_et.date()
    start_dt = _ET.localize(datetime.combine(today, time(9, 30)))
    end_dt   = _ET.localize(datetime.combine(today, time(9, 30 + opening_range_min)))
    window = df_et[(df_et.index >= start_dt) & (df_et.index < end_dt)]
    if window.empty:
        return None
    return OpeningRange(
        symbol=symbol,
        high=float(window["high"].max()),
        low=float(window["low"].min()),
        start_ts=window.index[0],
        end_ts=window.index[-1],
    )


def detect_breakout(
    symbol: str,
    opening_range: OpeningRange,
    current_price: float,
) -> Optional[str]:
    """Return 'bullish' if current price is above OR high, 'bearish' if below OR low, else None."""
    if current_price <= 0:
        return None
    if current_price > opening_range.high:
        return "bullish"
    if current_price < opening_range.low:
        return "bearish"
    return None


def select_orb_trade(
    underlying: str,
    direction: str,
    regime_name: str,
    opening_range: OpeningRange,
    current_price: float,
    per_trade_budget_usd: float,
) -> Optional[ORBPick]:
    """Pick an ATM short-dated option to express the breakout."""
    # Regime filter — only fire in agreeing regimes
    rg = (regime_name or "").lower()
    if direction == "bullish" and rg not in _BULLISH_REGIMES:
        return None
    if direction == "bearish" and rg not in _BEARISH_REGIMES:
        return None

    cfg = rc.load()
    dte_min = int(cfg.get("intraday_target_dte_min", 1))
    dte_max = int(cfg.get("intraday_target_dte_max", 7))
    target_delta = float(cfg.get("intraday_target_delta", 0.50))
    min_width_pct = float(cfg.get("intraday_min_orb_width_pct", 0.003))

    if opening_range.width_pct < min_width_pct:
        logger.info(f"ORB {underlying}: range too tight ({opening_range.width_pct:.2%}) — skip")
        return None

    side = "call" if direction == "bullish" else "put"
    chain = get_option_chain(underlying, dte_min=dte_min, dte_max=dte_max, sides=[side])
    if not chain:
        logger.info(f"ORB {underlying} {side}: no chain in {dte_min}-{dte_max} DTE")
        return None
    contract = pick_contract(chain, target_delta=target_delta, side=side)
    if not contract or contract.mid <= 0:
        return None

    limit_price = round(contract.mid, 2)
    per_contract_cost = limit_price * 100
    qty = int(per_trade_budget_usd // per_contract_cost)
    if qty < 1:
        return None
    total_cost = qty * per_contract_cost
    reasoning = (
        f"{direction} ORB on {underlying} — "
        f"spot ${current_price:.2f} vs OR [${opening_range.low:.2f}, ${opening_range.high:.2f}] "
        f"(width {opening_range.width_pct:.2%}), regime {rg.upper()}, "
        f"Δ={contract.delta:+.2f}, DTE={contract.dte}"
    )
    return ORBPick(
        underlying=underlying,
        direction=direction,
        contract_symbol=contract.symbol,
        qty=qty,
        limit_price=limit_price,
        total_cost=total_cost,
        orb_high=opening_range.high,
        orb_low=opening_range.low,
        current_price=current_price,
        reasoning=reasoning,
    )
