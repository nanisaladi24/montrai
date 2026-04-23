"""
Hot-reloadable runtime configuration.
The bot reads this every cycle so changes in runtime.json take effect
without a restart. Falls back to settings.py defaults if the file is
missing or a key is absent.
"""
import json
import os
from pathlib import Path
from typing import Any

from config.settings import (
    WATCHLIST,
    MAX_DAILY_SPEND_USD,
    MAX_POSITION_SIZE_PCT,
    MAX_OPEN_POSITIONS,
    DAILY_LOSS_HALT_PCT,
    PEAK_DRAWDOWN_LOCKOUT_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    MIN_HOLD_DAYS,
    MAX_HOLD_DAYS,
    SIGNAL_INTERVAL_MINUTES,
    EXTENDED_HOURS_ENABLED,
    REGIME_ALLOCATION,
    STOCK_TRADING_ENABLED,
    OPTIONS_TRADING_ENABLED,
    INTRADAY_ENABLED,
    STOCK_MAX_DAILY_USD,
    OPTIONS_MAX_DAILY_USD,
    OPTIONS_MAX_DEPLOYED_USD,
    STOCK_MAX_DEPLOYED_USD,
    OPTIONS_MAX_PER_TRADE_PCT,
    OPTIONS_TAKE_PROFIT_PCT,
    OPTIONS_STOP_LOSS_PCT,
    OPTIONS_MIN_DTE_EXIT,
    OPTIONS_TARGET_DTE,
    OPTIONS_TARGET_DELTA,
    COVERED_CALL_ENABLED,
    COVERED_CALL_TARGET_DELTA,
    COVERED_CALL_TARGET_DTE,
    COVERED_CALL_AUTO_ACQUIRE,
    DYNAMIC_WATCHLIST_ENABLED,
    DYNAMIC_WATCHLIST_LIMIT,
    DYNAMIC_WATCHLIST_MIN_PRICE,
    DYNAMIC_WATCHLIST_MIN_OI,
    DYNAMIC_WATCHLIST_REFRESH_HOUR_ET,
    SIGNAL_SCORE_THRESHOLD_LONG,
    SIGNAL_SCORE_THRESHOLD_SHORT,
    PAPER_FORCE_TOP_SCORE,
    PAPER_FORCE_AFTER_HOUR_ET,
    PAPER_FORCE_AFTER_MIN_ET,
    INTRADAY_OPENING_RANGE_MIN,
    INTRADAY_FORCE_CLOSE_HOUR_ET,
    INTRADAY_FORCE_CLOSE_MIN_ET,
    INTRADAY_SCAN_INTERVAL_MIN,
    INTRADAY_MAX_DAILY_USD,
    INTRADAY_TARGET_DTE,
    INTRADAY_TARGET_DELTA,
    INTRADAY_STOP_LOSS_PCT,
    INTRADAY_TAKE_PROFIT_PCT,
    INTRADAY_MIN_ORB_WIDTH_PCT,
    SPREADS_ENABLED,
    IRON_CONDOR_ENABLED,
    SPREAD_TARGET_SHORT_DELTA,
    SPREAD_WING_WIDTH,
    SPREAD_TAKE_PROFIT_PCT,
    SPREAD_STOP_LOSS_PCT,
    SPREAD_MIN_CREDIT,
    IRON_CONDOR_SHORT_DELTA,
    IRON_CONDOR_WING_WIDTH,
)

_RUNTIME_FILE = Path(__file__).parent / "runtime.json"

_DEFAULTS: dict[str, Any] = {
    "watchlist": WATCHLIST,
    "max_daily_spend_usd": MAX_DAILY_SPEND_USD,
    "max_position_size_pct": MAX_POSITION_SIZE_PCT,
    "max_open_positions": MAX_OPEN_POSITIONS,
    "daily_loss_halt_pct": DAILY_LOSS_HALT_PCT,
    "peak_drawdown_lockout_pct": PEAK_DRAWDOWN_LOCKOUT_PCT,
    "stop_loss_pct": STOP_LOSS_PCT,
    "take_profit_pct": TAKE_PROFIT_PCT,
    "min_hold_days": MIN_HOLD_DAYS,
    "max_hold_days": MAX_HOLD_DAYS,
    "signal_interval_minutes": SIGNAL_INTERVAL_MINUTES,
    "extended_hours_enabled": EXTENDED_HOURS_ENABLED,
    "regime_allocation": {str(k): v for k, v in REGIME_ALLOCATION.items()},
    # Financial Datasets overlay — set to false to disable fundamental scoring
    "financial_datasets_enabled": True,
    # Trade-mode knobs (hot-reloadable)
    "stock_trading_enabled":   STOCK_TRADING_ENABLED,
    "options_trading_enabled": OPTIONS_TRADING_ENABLED,
    "intraday_enabled":        INTRADAY_ENABLED,
    "stock_max_daily_usd":       STOCK_MAX_DAILY_USD,
    "options_max_daily_usd":     OPTIONS_MAX_DAILY_USD,
    "options_max_deployed_usd":  OPTIONS_MAX_DEPLOYED_USD,   # deployed-capital cap (no daily reset)
    "stock_max_deployed_usd":    STOCK_MAX_DEPLOYED_USD,     # deployed-capital cap (no daily reset)
    "options_max_per_trade_pct": OPTIONS_MAX_PER_TRADE_PCT,  # per-trade fraction of deployed cap
    # Options behavior
    "options_take_profit_pct": OPTIONS_TAKE_PROFIT_PCT,
    "options_stop_loss_pct":   OPTIONS_STOP_LOSS_PCT,
    "options_min_dte_exit":    OPTIONS_MIN_DTE_EXIT,
    "options_target_dte_min":  OPTIONS_TARGET_DTE[0],
    "options_target_dte_max":  OPTIONS_TARGET_DTE[1],
    "options_target_delta":    OPTIONS_TARGET_DELTA,
    # Covered call
    "covered_call_enabled":           COVERED_CALL_ENABLED,
    "covered_call_target_delta":      COVERED_CALL_TARGET_DELTA,
    "covered_call_target_dte_min":    COVERED_CALL_TARGET_DTE[0],
    "covered_call_target_dte_max":    COVERED_CALL_TARGET_DTE[1],
    "covered_call_auto_acquire":      COVERED_CALL_AUTO_ACQUIRE,
    # Multi-leg spreads + iron condor
    "spreads_enabled":              SPREADS_ENABLED,
    "iron_condor_enabled":          IRON_CONDOR_ENABLED,
    "spread_target_short_delta":    SPREAD_TARGET_SHORT_DELTA,
    "spread_wing_width":            SPREAD_WING_WIDTH,
    "spread_take_profit_pct":       SPREAD_TAKE_PROFIT_PCT,
    "spread_stop_loss_pct":         SPREAD_STOP_LOSS_PCT,
    "spread_min_credit":            SPREAD_MIN_CREDIT,
    "iron_condor_short_delta":      IRON_CONDOR_SHORT_DELTA,
    "iron_condor_wing_width":       IRON_CONDOR_WING_WIDTH,
    # Dynamic daily watchlist
    "dynamic_watchlist_enabled":   DYNAMIC_WATCHLIST_ENABLED,
    "dynamic_watchlist_limit":     DYNAMIC_WATCHLIST_LIMIT,
    "dynamic_watchlist_min_price": DYNAMIC_WATCHLIST_MIN_PRICE,
    "dynamic_watchlist_min_oi":    DYNAMIC_WATCHLIST_MIN_OI,
    "dynamic_watchlist_refresh_hour_et": DYNAMIC_WATCHLIST_REFRESH_HOUR_ET,
    # Signal thresholds + paper safety valve
    "signal_score_threshold_long":  SIGNAL_SCORE_THRESHOLD_LONG,
    "signal_score_threshold_short": SIGNAL_SCORE_THRESHOLD_SHORT,
    "paper_force_top_score":        PAPER_FORCE_TOP_SCORE,
    "paper_force_after_hour_et":    PAPER_FORCE_AFTER_HOUR_ET,
    "paper_force_after_min_et":     PAPER_FORCE_AFTER_MIN_ET,
    # Intraday ORB
    "intraday_opening_range_min":   INTRADAY_OPENING_RANGE_MIN,
    "intraday_force_close_hour_et": INTRADAY_FORCE_CLOSE_HOUR_ET,
    "intraday_force_close_min_et":  INTRADAY_FORCE_CLOSE_MIN_ET,
    "intraday_scan_interval_min":   INTRADAY_SCAN_INTERVAL_MIN,
    "intraday_max_daily_usd":       INTRADAY_MAX_DAILY_USD,
    "intraday_target_dte_min":      INTRADAY_TARGET_DTE[0],
    "intraday_target_dte_max":      INTRADAY_TARGET_DTE[1],
    "intraday_target_delta":        INTRADAY_TARGET_DELTA,
    "intraday_stop_loss_pct":       INTRADAY_STOP_LOSS_PCT,
    "intraday_take_profit_pct":     INTRADAY_TAKE_PROFIT_PCT,
    "intraday_min_orb_width_pct":   INTRADAY_MIN_ORB_WIDTH_PCT,
}


def load() -> dict[str, Any]:
    try:
        with open(_RUNTIME_FILE) as f:
            data = json.load(f)
        return {**_DEFAULTS, **data}
    except Exception:
        return dict(_DEFAULTS)


def save(cfg: dict[str, Any]) -> None:
    with open(_RUNTIME_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_watchlist() -> list[str]:
    return load().get("watchlist", WATCHLIST)


def get_regime_allocation() -> dict[int, float]:
    raw = load().get("regime_allocation", _DEFAULTS["regime_allocation"])
    return {int(k): float(v) for k, v in raw.items()}
