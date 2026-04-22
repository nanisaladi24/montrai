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
from core.options_data import (
    OptionContract, get_option_chain, pick_contract,
    pick_vertical_spread, pick_iron_condor, net_premium,
)
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


# ── Multi-leg picks (verticals + iron condor) ──────────────────────────────────

@dataclass
class SpreadPick:
    underlying: str
    strategy: str                   # bull_put_credit | bear_call_credit | bull_call_debit | bear_put_debit | iron_condor
    legs: list                      # [(contract, side), ...] side = "long" | "short"
    qty: int                        # signed: +N = long spread (debit), -N = short spread (credit)
    net_limit: float                # absolute per-unit $ (always positive)
    capital_at_risk: float          # total $ that can be lost
    score: float
    reasoning: str


def _strategy_for_spread(score: float, regime_name: str) -> Optional[str]:
    """Decision tree for vertical spreads. Credit spreads harvest IV when
    directional bias is present; debit spreads define risk for pure direction."""
    regime_name = (regime_name or "").lower()
    # Credit structures preferred — defined risk AND IV harvest in one trade
    if score >= _MIN_SCORE_LONG and regime_name in _BULLISH_REGIMES:
        return "bull_put_credit"
    if score <= _MAX_SCORE_SHORT and regime_name in _BEARISH_REGIMES:
        return "bear_call_credit"
    return None


def select_spread_trade(
    underlying: str,
    score: float,
    regime_name: str,
    max_risk_usd: float,
) -> Optional[SpreadPick]:
    """Pick a vertical credit spread based on regime + directional score.

    max_risk_usd caps the capital-at-risk for the whole trade; size is derived
    from (width - credit) × 100 × qty.
    """
    strategy = _strategy_for_spread(score, regime_name)
    if strategy is None:
        return None
    if max_risk_usd < 50:
        return None

    cfg = rc.load()
    dte_min = int(cfg.get("options_target_dte_min", 30))
    dte_max = int(cfg.get("options_target_dte_max", 45))
    short_delta = float(cfg.get("spread_target_short_delta", 0.30))
    wing_width = float(cfg.get("spread_wing_width", 5.0))
    min_credit = float(cfg.get("spread_min_credit", 0.20))

    # Each vertical uses ONE chain side
    spread_side = "put" if strategy == "bull_put_credit" else "call"
    chain = get_option_chain(underlying, dte_min=dte_min, dte_max=dte_max, sides=[spread_side])
    if not chain:
        return None
    pair = pick_vertical_spread(chain, spread_side, short_delta, wing_width, direction="credit")
    if not pair:
        return None
    short_leg, long_leg = pair

    # Compute net credit
    net = net_premium([(short_leg, "short"), (long_leg, "long")])
    credit = abs(net) if net < 0 else 0.0
    if credit < min_credit:
        logger.info(f"{underlying} {strategy}: credit ${credit:.2f} < min ${min_credit}")
        return None

    width = abs(short_leg.strike - long_leg.strike)
    max_loss_per_unit = max(width - credit, 0.01) * 100  # dollars per 1 spread unit
    qty_units = int(max_risk_usd // max_loss_per_unit)
    if qty_units < 1:
        return None

    capital_at_risk = qty_units * max_loss_per_unit
    reasoning = (
        f"{regime_name.upper()} regime, score {score:+.2f}, "
        f"short {short_leg.strike}/{spread_side[0].upper()}@Δ{short_leg.delta:+.2f}, "
        f"long {long_leg.strike}/{spread_side[0].upper()}, "
        f"width ${width:.0f}, credit ${credit:.2f}"
    )
    return SpreadPick(
        underlying=underlying,
        strategy=strategy,
        legs=[(short_leg, "short"), (long_leg, "long")],
        qty=-qty_units,  # negative = credit (we sold the spread)
        net_limit=credit,
        capital_at_risk=capital_at_risk,
        score=score,
        reasoning=reasoning,
    )


def select_iron_condor(
    underlying: str,
    score: float,
    regime_name: str,
    spot: float,
    max_risk_usd: float,
) -> Optional[SpreadPick]:
    """Iron condor for neutral / range-bound setups. Fires when conviction is
    low (|score| < 0.3) — premium-harvest strategy that monetizes chop."""
    if abs(score) >= 0.30:
        return None  # Directional — let spread/long-option path handle it
    if max_risk_usd < 100:
        return None
    cfg = rc.load()
    dte_min = int(cfg.get("options_target_dte_min", 30))
    dte_max = int(cfg.get("options_target_dte_max", 45))
    short_delta = float(cfg.get("iron_condor_short_delta", 0.15))
    wing_width = float(cfg.get("iron_condor_wing_width", 5.0))
    min_credit = float(cfg.get("spread_min_credit", 0.20))

    chain = get_option_chain(underlying, dte_min=dte_min, dte_max=dte_max, sides=["call", "put"])
    if not chain:
        return None
    result = pick_iron_condor(chain, spot, short_delta, wing_width)
    if not result:
        return None
    put_short, put_long, call_short, call_long = result

    legs = [
        (put_short,  "short"),
        (put_long,   "long"),
        (call_short, "short"),
        (call_long,  "long"),
    ]
    net = net_premium(legs)
    credit = abs(net) if net < 0 else 0.0
    if credit < min_credit * 2:  # iron condor collects from two sides
        return None

    max_wing_width = max(
        abs(put_short.strike  - put_long.strike),
        abs(call_short.strike - call_long.strike),
    )
    max_loss_per_unit = max(max_wing_width - credit, 0.01) * 100
    qty_units = int(max_risk_usd // max_loss_per_unit)
    if qty_units < 1:
        return None
    capital_at_risk = qty_units * max_loss_per_unit
    reasoning = (
        f"{regime_name.upper()} neutral range (|score|={abs(score):.2f}), "
        f"put {put_short.strike}/{put_long.strike}, call {call_short.strike}/{call_long.strike}, "
        f"credit ${credit:.2f}, width ${max_wing_width:.0f}"
    )
    return SpreadPick(
        underlying=underlying,
        strategy="iron_condor",
        legs=legs,
        qty=-qty_units,
        net_limit=credit,
        capital_at_risk=capital_at_risk,
        score=score,
        reasoning=reasoning,
    )
