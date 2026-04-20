from typing import List, Dict
from config.settings import (
    REGIME_ALLOCATION, WATCHLIST, MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS
)


# Per-regime preferred sector/symbol subsets (swing trading focus)
REGIME_WATCHLISTS: Dict[int, List[str]] = {
    0: [],                                          # crash: no trades
    1: ["SPY", "QQQ", "JPM", "BAC"],                # bear: only defensives/index
    2: ["SPY", "QQQ", "AAPL", "MSFT", "JPM"],       # neutral: quality large-caps
    3: WATCHLIST,                                    # bull: full universe
    4: ["SPY", "QQQ", "MSFT", "AAPL", "GOOGL"],     # euphoria: trim to blue chips
}


def get_regime_watchlist(regime: int) -> List[str]:
    return REGIME_WATCHLISTS.get(regime, WATCHLIST)


def compute_position_size(
    regime: int,
    portfolio_value: float,
    signal_score: float,
    atr_pct: float,
    is_halved: bool = False,
) -> float:
    """
    Kelly-inspired position sizing capped by regime allocation and risk limits.
    Returns dollar amount to allocate to the trade.
    """
    allocation_mult = REGIME_ALLOCATION.get(regime, 0.5)
    if is_halved:
        allocation_mult *= 0.5

    # Base position: max allowed % of portfolio
    base_dollars = portfolio_value * MAX_POSITION_SIZE_PCT * allocation_mult

    # Scale by signal confidence (0.6–1.0 → 60–100%)
    confidence = min(abs(signal_score) / 1.0, 1.0)
    base_dollars *= max(confidence, 0.5)

    # ATR-based volatility scaling: reduce size when vol is high
    if atr_pct > 0:
        target_risk_pct = 0.01   # risk 1% of portfolio per trade
        vol_adjusted = (target_risk_pct * portfolio_value) / atr_pct
        base_dollars = min(base_dollars, vol_adjusted)

    return round(max(base_dollars, 0), 2)


def can_open_new_position(open_count: int, regime: int) -> bool:
    if regime == 0:
        return False
    return open_count < MAX_OPEN_POSITIONS
