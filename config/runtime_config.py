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
