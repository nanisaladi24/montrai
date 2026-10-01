from typing import List, Dict
from config.settings import MAX_POSITION_SIZE_PCT, SECTOR_MAP
import config.runtime_config as rc


def symbol_sector(symbol: str) -> str:
    """Return canonical sector for a ticker; 'other' for anything not mapped."""
    return SECTOR_MAP.get(symbol.upper(), "other")


def count_sector_positions(state, sector: str) -> int:
    """Count open positions (stock + options + multi-leg) whose underlying maps
    to the given sector. Used to enforce max_positions_per_sector."""
    if sector == "other":
        return 0  # 'other' is a catch-all — never gate on it
    n = 0
    for sym in getattr(state, "positions", {}):
        if symbol_sector(sym) == sector:
            n += 1
    for pos in getattr(state, "options_positions", {}).values():
        if symbol_sector(getattr(pos, "underlying", "")) == sector:
            n += 1
    for pos in getattr(state, "multi_leg_positions", {}).values():
        if symbol_sector(getattr(pos, "underlying", "")) == sector:
            n += 1
    return n


def sector_cap_blocks(state, symbol: str) -> bool:
    """True if adding one more position on `symbol` would exceed the per-sector cap."""
    sector = symbol_sector(symbol)
    if sector == "other":
        return False
    cap = int(rc.load().get("max_positions_per_sector", 2))
    return count_sector_positions(state, sector) >= cap


def get_regime_watchlist(regime: int) -> List[str]:
    """Base watchlist merged with today's dynamic mover list, narrowed by regime.

    Crash regime → empty list (no trades).
    Bear + Euphoria → narrow to blue-chips / indices only (ignore dynamic list
    in these defensive regimes — we don't want to chase movers when risk is elevated).
    All other regimes → base ∪ dynamic.
    """
    watchlist = list(rc.get_watchlist())

    # Bear / Euphoria narrowing (defensive — stay with highly-liquid names only)
    if regime == 0:
        return []
    if regime == 1:
        candidates = ["SPY", "QQQ", "JPM", "BAC"]
        return [s for s in candidates if s in watchlist or s in candidates]
    if regime == 4:
        candidates = ["SPY", "QQQ", "MSFT", "AAPL", "GOOGL"]
        return [s for s in candidates if s in watchlist or s in candidates]

    # Bull / Neutral: merge in today's dynamic movers (from bot_state.json)
    try:
        from core.position_tracker import BotState
        state = BotState.load()
        from datetime import date as _d
        if state.dynamic_watchlist_date == _d.today().isoformat():
            dynamic = [entry["symbol"] for entry in state.dynamic_watchlist
                       if "symbol" in entry]
            # Dedup while preserving order: static first, then new dynamic additions
            seen = set(watchlist)
            for sym in dynamic:
                if sym not in seen:
                    watchlist.append(sym)
                    seen.add(sym)
    except Exception:
        # If state load fails, fall back to static watchlist silently
        pass

    return watchlist


def compute_position_size(
    regime: int,
    portfolio_value: float,
    signal_score: float,
    atr_pct: float,
    is_halved: bool = False,
) -> float:
    cfg = rc.load()
    allocation_mult = rc.get_regime_allocation().get(regime, 0.5)
    max_pos_pct = cfg.get("max_position_size_pct", MAX_POSITION_SIZE_PCT)

    if is_halved:
        allocation_mult *= 0.5

    base_dollars = portfolio_value * max_pos_pct * allocation_mult
    confidence = min(abs(signal_score) / 1.0, 1.0)
    base_dollars *= max(confidence, 0.5)

    if atr_pct > 0:
        target_risk_pct = 0.01
        vol_adjusted = (target_risk_pct * portfolio_value) / atr_pct
        base_dollars = min(base_dollars, vol_adjusted)

    return round(max(base_dollars, 0), 2)


def can_open_new_position(open_count: int, regime: int) -> bool:
    if regime == 0:
        return False
    return open_count < rc.load().get("max_open_positions", 8)
