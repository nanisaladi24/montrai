"""Durable MLEG order ledger.

Records each multi-leg submission (order_id + strategy + underlying + legs +
submitted net limit price) so that reconcile_from_broker can recover the
true entry credit/debit even after a state wipe. Broker avg_entry_price is
noisy on paper fills and can't be trusted for exit-threshold math.
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from config.settings import ORDERS_LEDGER_FILE
from monitoring.logger import get_logger

logger = get_logger("orders_ledger")


def record_multi_leg_submission(
    order_id: str,
    strategy: str,
    underlying: str,
    legs: list[dict],
    qty: int,
    net_limit_price: float,
    order_side: str,
) -> None:
    """Append a successful MLEG submission to the ledger.

    legs item format: {"contract_symbol", "side", "position_intent", "ratio_qty"}.
    Only records OPEN submissions (position_intent == "open") — close events
    are handled via state.close_multi_leg_position.
    """
    if not order_id:
        return
    # Closing orders don't need ledger entries — the original open lives there.
    if all(lg.get("position_intent") == "close" for lg in legs):
        return
    entry = {
        "order_id": order_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "underlying": underlying,
        "order_side": order_side,          # "buy" | "sell"
        "qty": qty,
        "net_limit_price": round(abs(net_limit_price), 4),
        "legs": [
            {
                "contract_symbol": lg["contract_symbol"],
                "side": lg["side"],            # "buy" | "sell" at submission
                "position_intent": lg.get("position_intent", "open"),
                "ratio_qty": lg.get("ratio_qty", 1),
            }
            for lg in legs
        ],
    }
    try:
        data = _load()
        data.append(entry)
        Path(ORDERS_LEDGER_FILE).write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"ledger write failed: {e}")


def find_matching_open(
    underlying: str,
    contract_symbols: set[str],
) -> Optional[dict]:
    """Return the most-recent ledger entry whose leg contract_symbols exactly
    match the given set (order-independent), or None."""
    data = _load()
    wanted = {s.lstrip("O:") for s in contract_symbols}
    best: Optional[dict] = None
    best_ts = ""
    for entry in data:
        if entry.get("underlying") != underlying:
            continue
        entry_syms = {lg["contract_symbol"].lstrip("O:") for lg in entry.get("legs", [])}
        if entry_syms != wanted:
            continue
        ts = entry.get("ts", "")
        if ts > best_ts:
            best_ts = ts
            best = entry
    return best


def _load() -> list[dict]:
    p = Path(ORDERS_LEDGER_FILE)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text() or "[]")
    except Exception as e:
        logger.warning(f"ledger load failed: {e}")
        return []
