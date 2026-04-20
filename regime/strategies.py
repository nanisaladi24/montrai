from typing import List, Dict
from config.settings import MAX_POSITION_SIZE_PCT
import config.runtime_config as rc


def get_regime_watchlist(regime: int) -> List[str]:
    watchlist = rc.get_watchlist()
    # In crash regime trade nothing; in bear/euphoria trim to blue chips + indices
    overrides: Dict[int, List[str]] = {
        0: [],
        1: [s for s in ["SPY", "QQQ", "JPM", "BAC"] if s in watchlist or s in ["SPY", "QQQ", "JPM", "BAC"]],
        4: [s for s in ["SPY", "QQQ", "MSFT", "AAPL", "GOOGL"] if s in watchlist or s in ["SPY", "QQQ", "MSFT", "AAPL", "GOOGL"]],
    }
    return overrides.get(regime, watchlist)


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
