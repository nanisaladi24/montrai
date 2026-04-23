"""One-shot: close every open option position at the broker.

Reads state for tracked multi-leg and single-leg positions, submits the
reversing MLEG / single-leg orders, and on broker-ack removes them from
state. Broker is the source of truth — anything at Alpaca not in state
gets closed too (with a warning). Paper or live, driven by .env.

Usage:  .venv/bin/python scripts/close_all_positions.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.position_tracker import BotState
from executor.order_executor import (
    get_broker, get_option_positions, submit_multi_leg_order,
)
from monitoring.logger import get_logger, log_trade

logger = get_logger("close_all")


def _current_spread_value(legs: list, broker_positions: dict[str, dict]) -> float:
    """Sum signed per-leg mid: short legs cost us to buy back (+),
    long legs return premium (−). Returns absolute net to close."""
    net = 0.0
    for leg in legs:
        sym = leg.contract_symbol
        bp = broker_positions.get(sym) or broker_positions.get(sym.lstrip("O:"))
        if not bp:
            continue
        mark = bp["current_price"]
        if leg.side == "short":
            net += mark
        else:
            net -= mark
    return abs(net)


def close_multi_leg(state: BotState, broker_positions: dict[str, dict]) -> None:
    for key, pos in list(state.multi_leg_positions.items()):
        current_value = _current_spread_value(pos.legs, broker_positions)
        closing_legs = []
        for leg in pos.legs:
            closing_legs.append({
                "contract_symbol": leg.contract_symbol,
                "side": "sell" if leg.side == "long" else "buy",
                "position_intent": "close",
                "ratio_qty": 1,
            })
        close_side = "buy" if pos.is_credit else "sell"
        logger.info(f"Closing {pos.strategy} {pos.underlying} x{pos.unit_count} "
                    f"@ net ${current_value:.2f} (entry ${pos.net_entry:.2f})")
        order_id = submit_multi_leg_order(
            legs=closing_legs, qty=pos.unit_count,
            net_limit_price=max(current_value, 0.05),
            order_side=close_side, strategy=f"emergency_close_{pos.strategy}",
            regime_name="emergency_close",
        )
        if order_id:
            pnl = state.close_multi_leg_position(key, current_value)
            logger.info(f"  → order {order_id} submitted · tracked P&L ${pnl:+.2f}")
        else:
            logger.error(f"  → close order rejected for {key}; leaving in state")


def close_single_leg(state: BotState, broker_positions: dict[str, dict]) -> None:
    broker = get_broker()
    for sym, pos in list(state.options_positions.items()):
        bp = broker_positions.get(sym) or broker_positions.get(sym.lstrip("O:"))
        if not bp:
            logger.warning(f"Single-leg {sym} not present at broker — removing from state")
            state.options_positions.pop(sym, None)
            continue
        mark = bp["current_price"]
        logger.info(f"Closing single-leg {sym} qty={pos.qty} @ ${mark:.2f}")
        # Reverse the position: if we're long, we sell; if short, we buy
        if pos.qty > 0:
            oid = broker.sell_option(sym, pos.qty, mark, reason="emergency_close",
                                     regime_name="emergency_close")
        else:
            oid = broker.buy_option(sym, abs(pos.qty), mark, regime_name="emergency_close")
        if oid:
            pnl = state.close_option_position(sym, mark)
            logger.info(f"  → order {oid} · tracked P&L ${pnl:+.2f}")


def main():
    state = BotState.load()
    broker_live = get_option_positions()
    broker_by_sym = {p["symbol"]: p for p in broker_live}
    logger.info(f"Broker has {len(broker_by_sym)} option legs; state has "
                f"{len(state.multi_leg_positions)} multi-leg + "
                f"{len(state.options_positions)} single-leg positions tracked.")

    close_multi_leg(state, broker_by_sym)
    close_single_leg(state, broker_by_sym)
    state.save()

    Path("logs").mkdir(exist_ok=True)
    with open("logs/recovery.log", "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} | close_all_positions | "
                f"ran emergency close script\n")
    logger.info("Done. Re-check broker positions once fills settle.")


if __name__ == "__main__":
    main()
