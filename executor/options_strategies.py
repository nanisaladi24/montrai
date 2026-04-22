"""
Options strategy selector.

Maps (regime, technical score, IV context) → a concrete long-option trade
(long call or long put). Scaffolded for future expansion to spreads and
iron condors, but only long-options are enabled in Phase 1 because they
have the simplest, bounded risk profile (premium paid = max loss).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

import config.runtime_config as rc
from core.options_data import OptionContract, get_option_chain, pick_contract
from monitoring.logger import get_logger

logger = get_logger("options_strategies")

# Regimes where each directional side is allowed. Crash + bear lean bearish;
# bull + euphoria lean bullish; neutral lets the technical score decide.
_BULLISH_REGIMES = {"bull", "neutral", "euphoria"}
_BEARISH_REGIMES = {"crash", "bear", "neutral"}

_MIN_SCORE_LONG = 0.6
_MAX_SCORE_SHORT = -0.6


@dataclass
class OptionsPick:
    underlying: str
    strategy: str               # long_call | long_put
    contract: OptionContract
    qty: int                    # number of contracts
    limit_price: float          # per-contract limit in dollars
    total_cost: float           # qty * limit_price * 100
    score: float
    reasoning: str


def _strategy_for(score: float, regime_name: str) -> Optional[str]:
    regime_name = (regime_name or "").lower()
    if score >= _MIN_SCORE_LONG and regime_name in _BULLISH_REGIMES:
        return "long_call"
    if score <= _MAX_SCORE_SHORT and regime_name in _BEARISH_REGIMES:
        return "long_put"
    return None


def select_trade(
    underlying: str,
    score: float,
    regime_name: str,
    per_trade_budget_usd: float,
) -> Optional[OptionsPick]:
    """Return a concrete trade or None if the signal doesn't meet thresholds.

    per_trade_budget_usd caps the premium outlay for this one trade.
    """
    strategy = _strategy_for(score, regime_name)
    if strategy is None:
        return None
    if per_trade_budget_usd < 10:
        return None  # rounding noise — not enough budget for a single contract

    cfg = rc.load()
    dte_min = int(cfg.get("options_target_dte_min", 30))
    dte_max = int(cfg.get("options_target_dte_max", 45))
    target_delta = float(cfg.get("options_target_delta", 0.40))
    side = "call" if strategy == "long_call" else "put"

    chain = get_option_chain(underlying, dte_min=dte_min, dte_max=dte_max, sides=[side])
    if not chain:
        logger.info(f"{underlying}: no {side} chain in {dte_min}-{dte_max}DTE window")
        return None

    contract = pick_contract(chain, target_delta=target_delta, side=side)
    if not contract:
        logger.info(f"{underlying}: no {side} contract near {target_delta}Δ with liquidity")
        return None

    # Round limit to the contract's quote mid, clamped by the ask. A single
    # contract controls 100 shares so dollar-cost = mid * 100 * qty.
    mid = contract.mid
    if mid <= 0:
        return None
    limit_price = round(mid, 2)
    per_contract_cost = limit_price * 100
    qty = int(per_trade_budget_usd // per_contract_cost)
    if qty < 1:
        return None

    total_cost = qty * per_contract_cost
    reasoning = (
        f"{regime_name.upper()} regime, score {score:+.2f}, "
        f"Δ={contract.delta:+.2f}, IV={contract.iv:.2f}, "
        f"OI={contract.open_interest}, DTE={contract.dte}"
    )
    return OptionsPick(
        underlying=underlying,
        strategy=strategy,
        contract=contract,
        qty=qty,
        limit_price=limit_price,
        total_cost=total_cost,
        score=score,
        reasoning=reasoning,
    )
