"""
Main Orchestrator — Train → Monitor → Execute loop.

Run modes:
  python main.py               # live trading loop
  python main.py --backtest    # run walk-forward backtest for one symbol
  python main.py --backtest-all # walk-forward backtest for all watchlist symbols
  python main.py --scan        # score all watchlist symbols now — ranked buy candidates
  python main.py --train       # retrain HMM only
"""
import argparse
import json
import os
import time
import sys
import pandas as pd
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

from config.settings import TRADING_MODE
import config.runtime_config as rc
from core.market_data import fetch_historical, latest_quote, is_market_open, current_session
from core.feature_engineering import build_hmm_features, swing_signal, add_indicators
from core.position_tracker import BotState, Position, OptionsPosition, OptionLeg, MultiLegPosition
from regime.hmm_engine import RegimeDetector
from regime.strategies import get_regime_watchlist, compute_position_size, can_open_new_position
from risk.risk_manager import RiskManager
from executor.order_executor import (
    login, get_portfolio_value, get_cash,
    buy_fractional, sell_all,
    supports_options, buy_option, sell_option, get_option_positions, get_stock_positions,
    supports_multi_leg, submit_multi_leg_order,
)
from monitoring.logger import get_logger

logger = get_logger("main")

HEARTBEAT_FILE = "bot_heartbeat.json"


def _parse_occ_symbol(occ: str) -> Optional[dict]:
    """Parse Alpaca/Polygon OCC option symbol → {underlying, expiry, type, strike}."""
    s = occ[2:] if occ.startswith("O:") else occ
    if len(s) < 15:
        return None
    try:
        strike = int(s[-8:]) / 1000.0
        type_char = s[-9]
        date_str = s[-15:-9]
        underlying = s[:-15]
        return {
            "contract_symbol": occ,
            "underlying": underlying,
            "expiry": f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}",
            "contract_type": "call" if type_char.upper() == "C" else "put",
            "strike": strike,
        }
    except Exception:
        return None


def reconcile_from_broker(state: BotState) -> None:
    """Two-way sync with the broker — Alpaca is the source of truth.

    Adopt: positions at broker that state doesn't know about get synthesized
    into tracked entries so exit logic can manage them. Uses the order ledger
    for authoritative net_entry when available; falls back to broker cost
    basis + skip_sl flag for truly orphan positions.

    Prune: tracked positions whose legs are no longer present at the broker
    (user closed manually, or filled via another channel) get removed from
    state. Partial mismatches (some legs missing) get a warning but are
    left alone to avoid accidental data loss.
    """
    try:
        live = get_option_positions()
    except Exception as e:
        logger.warning(f"reconcile_from_broker: could not fetch broker positions: {e}")
        return

    broker_symbols = {p["symbol"] for p in live}
    # Normalize: symbols may or may not have the "O:" prefix between broker + state
    broker_syms_norm = {s.lstrip("O:") for s in broker_symbols} | broker_symbols

    # ── Prune tracked multi-leg positions whose legs disappeared at broker ──
    dirty = False
    for key in list(state.multi_leg_positions.keys()):
        mlp = state.multi_leg_positions[key]
        leg_syms = [leg.contract_symbol for leg in mlp.legs]
        present = [s for s in leg_syms if s in broker_syms_norm or s.lstrip("O:") in broker_syms_norm]
        if len(present) == 0:
            logger.info(f"reconcile: pruning {mlp.strategy} {mlp.underlying} x{abs(mlp.qty)} — all legs closed at broker")
            del state.multi_leg_positions[key]
            dirty = True
        elif len(present) < len(leg_syms):
            logger.warning(f"reconcile: {mlp.strategy} {mlp.underlying} partial mismatch "
                           f"({len(present)}/{len(leg_syms)} legs at broker) — left in state for manual review")

    # ── Prune tracked single-leg options that are no longer at broker ──
    for sym in list(state.options_positions.keys()):
        if sym not in broker_syms_norm and sym.lstrip("O:") not in broker_syms_norm:
            logger.info(f"reconcile: pruning single-leg {sym} — no longer at broker")
            del state.options_positions[sym]
            dirty = True

    # Stamp sync time regardless of whether changes were made
    state.last_broker_sync_at = datetime.now(timezone.utc).isoformat()

    if not live:
        if dirty:
            state.save()
        else:
            state.save()  # still persist the sync timestamp
        return

    tracked_contracts: set[str] = set()
    for mlp in state.multi_leg_positions.values():
        for leg in mlp.legs:
            tracked_contracts.add(leg.contract_symbol)
            tracked_contracts.add(f"O:{leg.contract_symbol}" if not leg.contract_symbol.startswith("O:") else leg.contract_symbol[2:])
    for sym in state.options_positions.keys():
        tracked_contracts.add(sym)
        tracked_contracts.add(f"O:{sym}" if not sym.startswith("O:") else sym[2:])

    orphans = [p for p in live if p["symbol"] not in tracked_contracts and p["symbol"].lstrip("O:") not in tracked_contracts]
    if not orphans:
        if dirty:
            state.save()
        else:
            state.save()
        return
    logger.info(f"reconcile_from_broker: {len(orphans)} orphan leg(s) found at broker — adopting into state")

    # Group by underlying + expiry so multi-leg structures stay together
    groups: dict[tuple, list[dict]] = {}
    singletons: list[dict] = []
    parsed_cache: dict[str, dict] = {}
    for p in orphans:
        parsed = _parse_occ_symbol(p["symbol"])
        if not parsed:
            singletons.append(p)
            continue
        parsed_cache[p["symbol"]] = parsed
        groups.setdefault((parsed["underlying"], parsed["expiry"]), []).append(p)

    today = datetime.now(timezone.utc).date().isoformat()

    for (underlying, expiry), group in groups.items():
        if len(group) == 1:
            singletons.append(group[0])
            continue
        if len(group) not in (2, 4):
            logger.warning(f"reconcile: {underlying} {expiry} has {len(group)} orphan legs — skipping (not a vertical/condor)")
            for p in group:
                singletons.append(p)
            continue
        legs: list[OptionLeg] = []
        for p in group:
            parsed = parsed_cache[p["symbol"]]
            leg_qty = p["qty"]                       # signed int from broker
            side = "short" if leg_qty < 0 else "long"
            legs.append(OptionLeg(
                contract_symbol=p["symbol"],
                side=side,
                contract_type=parsed["contract_type"],
                strike=parsed["strike"],
                expiry=parsed["expiry"],
                entry_price=abs(p["avg_entry_price"]),
                ratio_qty=1,
            ))

        # Classify strategy + credit/debit from STRIKE STRUCTURE (reliable) —
        # broker-reported avg_entry_price can be noisy or signed unpredictably.
        calls = [l for l in legs if l.contract_type == "call"]
        puts  = [l for l in legs if l.contract_type == "put"]
        strategy: Optional[str] = None
        is_credit = False
        if len(group) == 4 and len(calls) == 2 and len(puts) == 2:
            strategy = "iron_condor"
            is_credit = True   # standard iron condor is always credit
        elif len(calls) == 2 and len(puts) == 0:
            short = next((l for l in calls if l.side == "short"), None)
            long_ = next((l for l in calls if l.side == "long"), None)
            if short and long_:
                if short.strike < long_.strike:
                    strategy, is_credit = "bear_call_credit", True
                else:
                    strategy, is_credit = "bull_call_debit", False
        elif len(puts) == 2 and len(calls) == 0:
            short = next((l for l in puts if l.side == "short"), None)
            long_ = next((l for l in puts if l.side == "long"), None)
            if short and long_:
                if short.strike > long_.strike:
                    strategy, is_credit = "bull_put_credit", True
                else:
                    strategy, is_credit = "bear_put_debit", False
        if strategy is None:
            logger.warning(f"reconcile: {underlying} {expiry} — unrecognized leg mix, skipping")
            continue

        # Ledger-first: look up the submitted net limit price (authoritative)
        from core.orders_ledger import find_matching_open
        leg_syms = {l.contract_symbol for l in legs}
        ledger_entry = find_matching_open(underlying, leg_syms)

        unit_count = abs(group[0]["qty"])
        pos_qty = -unit_count if is_credit else unit_count

        if ledger_entry:
            net_entry = float(ledger_entry["net_limit_price"])
            origin = "reconciled_ledger"
        else:
            # No ledger trace — broker avg_entry_price is noisy on paper fills
            # and can't be trusted for SL math. Fall back to it for visibility,
            # but flag the position so exit logic skips the stop-loss path.
            short_sum = sum(l.entry_price for l in legs if l.side == "short")
            long_sum  = sum(l.entry_price for l in legs if l.side == "long")
            net_entry = round(abs(short_sum - long_sum), 4)
            origin = "reconciled_orphan"

        key = f"{underlying}_{strategy}_reconciled_{today.replace('-', '')}_{len(state.multi_leg_positions)}"
        state.multi_leg_positions[key] = MultiLegPosition(
            key=key, strategy=strategy, underlying=underlying,
            legs=legs, qty=pos_qty, net_entry=net_entry,
            entry_date=today, regime_at_entry="reconciled",
            origin=origin,
        )
        logger.info(f"reconcile: adopted {strategy} {underlying} {expiry} x{unit_count} "
                    f"net ${net_entry:.2f} ({'credit' if is_credit else 'debit'}) [{origin}]")

    for p in singletons:
        parsed = _parse_occ_symbol(p["symbol"]) or {}
        qty = p["qty"]
        state.options_positions[p["symbol"]] = OptionsPosition(
            contract_symbol=p["symbol"],
            underlying=parsed.get("underlying", ""),
            side=parsed.get("contract_type", "call"),
            strike=parsed.get("strike", 0.0),
            expiry=parsed.get("expiry", ""),
            qty=qty,
            entry_premium=p["avg_entry_price"],
            entry_date=today,
            regime_at_entry="reconciled",
            strategy="reconciled",
        )
        logger.info(f"reconcile: adopted singleton {p['symbol']} qty={qty} @ ${p['avg_entry_price']:.2f}")

    state.save()


def write_heartbeat(state: BotState, regime: int, regime_name: str, mode: str):
    """Dashboard reads this to show Running/Sleeping/Stopped + paper vs live.
    Written each cycle."""
    try:
        import config.settings as _cfg
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump({
                "ts": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "regime": regime,
                "regime_name": regime_name,
                "session": current_session(),
                "cycles": state.cycles,
                "mode": mode,
                "broker": _cfg.BROKER,
                "trading_mode": _cfg.TRADING_MODE,
                "alpaca_paper": getattr(_cfg, "ALPACA_PAPER", True),
                "stock_trading_enabled":   rc.load().get("stock_trading_enabled", False),
                "options_trading_enabled": rc.load().get("options_trading_enabled", True),
                "intraday_enabled":        rc.load().get("intraday_enabled", False),
                "spreads_enabled":         rc.load().get("spreads_enabled", False),
                "iron_condor_enabled":     rc.load().get("iron_condor_enabled", False),
                "covered_call_enabled":    rc.load().get("covered_call_enabled", False),
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"write_heartbeat: {e}")


def train_phase(detector: RegimeDetector) -> RegimeDetector:
    logger.info("═══ TRAINING PHASE ═══")
    success = detector.train("SPY")
    if not success:
        logger.error("HMM training failed. Using previous model if available.")
    return detector


def monitor_phase(detector: RegimeDetector, state: BotState, portfolio_value: float) -> int:
    """Detect current market regime. Returns regime integer."""
    logger.info("═══ MONITORING PHASE ═══")
    df = fetch_historical("SPY", days=120)
    if df.empty:
        logger.warning("Could not fetch SPY data for regime detection.")
        return 2  # neutral fallback
    df = add_indicators(df)
    if df.empty:
        logger.warning("SPY indicators dropped to empty frame — skipping regime detect.")
        return 2
    features = build_hmm_features(df)
    if features is None or getattr(features, "size", 0) == 0:
        logger.warning("HMM feature matrix empty — skipping regime detect.")
        return 2
    regime = detector.predict_regime(features)
    regime_name = detector.regime_name(regime)
    logger.info(f"Current regime: {regime} ({regime_name.upper()})")

    # Check peak drawdown circuit breaker
    if RiskManager.check_peak_drawdown(portfolio_value, state):
        logger.critical("Peak drawdown lockout triggered. Exiting.")
        sys.exit(1)

    return regime


def execute_phase(detector: RegimeDetector, state: BotState, regime: int, portfolio_value: float):
    """Scan watchlist for exits then entries."""
    logger.info("═══ EXECUTION PHASE ═══")
    state.reset_daily_if_new_day()
    regime_name = detector.regime_name(regime)

    # ── 1. Check exits first ────────────────────────────────────────────────
    for symbol, pos in list(state.positions.items()):
        price = latest_quote(symbol)
        if not price:
            continue
        should_exit, reason = RiskManager.should_exit_position(pos, price)
        if should_exit:
            order_id = sell_all(symbol, price, reason, regime_name)
            if order_id:
                pnl = state.close_position(symbol, price)
                logger.info(f"CLOSED {symbol} @ ${price:.2f} | P&L ${pnl:+.2f} | reason: {reason}")

    # ── 2. Daily loss check ─────────────────────────────────────────────────
    # Use today's starting value approximation from state
    start_val = state.peak_equity or portfolio_value
    RiskManager.check_daily_loss(portfolio_value, start_val, state)

    # ── 3. Scan for entries ─────────────────────────────────────────────────
    if not can_open_new_position(len(state.positions), regime):
        logger.info(f"No new entries allowed in {regime_name} regime or max positions reached.")
        return

    watchlist = get_regime_watchlist(regime)
    for symbol in watchlist:
        if symbol in state.positions:
            continue  # already holding

        df = fetch_historical(symbol, days=120)
        if df.empty or len(df) < 65:
            continue

        signal = swing_signal(df, symbol=symbol)
        if signal["score"] < 0.6:
            continue

        price = latest_quote(symbol)
        if not price or price <= 0:
            continue

        atr_pct = signal["last"].get("atr_pct", 0.02)
        dollars = compute_position_size(
            regime, portfolio_value, signal["score"], atr_pct, state.is_halved
        )
        if dollars < 1.0:
            logger.info(f"Skip {symbol}: position size too small (${dollars:.2f})")
            continue

        verdict = RiskManager.approve_trade(symbol, dollars, state, portfolio_value)
        if not verdict.allowed:
            logger.warning(f"Trade blocked for {symbol}: {verdict.reason}")
            continue

        order_id = buy_fractional(symbol, verdict.adjusted_dollars, regime_name)
        if order_id:
            state.daily_spent += verdict.adjusted_dollars
            stop_loss, take_profit = RiskManager.compute_stops(price)
            qty = verdict.adjusted_dollars / price
            state.positions[symbol] = Position(
                symbol=symbol,
                quantity=round(qty, 6),
                entry_price=price,
                entry_date=datetime.now(timezone.utc).date().isoformat(),
                stop_loss=stop_loss,
                take_profit=take_profit,
                regime_at_entry=regime_name,
            )
            state.save()
            logger.info(
                f"OPENED {symbol} ${verdict.adjusted_dollars:.2f} @ ${price:.2f} | "
                f"SL=${stop_loss:.2f} TP=${take_profit:.2f} | reasons: {signal['reasons']}"
            )

        if not can_open_new_position(len(state.positions), regime):
            break   # max positions reached


def options_execute_phase(detector: RegimeDetector, state: BotState, regime: int, portfolio_value: float):
    """Options-native execution path — exits first, then new directional entries."""
    from executor.options_strategies import select_trade
    from core.options_data import get_option_chain

    logger.info("═══ OPTIONS EXECUTION PHASE ═══")
    state.reset_daily_if_new_day()
    regime_name = detector.regime_name(regime)

    if not supports_options():
        logger.warning("Current broker does not support options — skipping options phase.")
        return

    # ── 1. Exits on existing option positions ──────────────────────────────────
    broker_positions = {p["symbol"]: p for p in get_option_positions()}
    for contract_symbol, pos in list(state.options_positions.items()):
        if pos.is_short:
            continue  # short legs (covered calls, spreads) are owned by covered_call_phase
        broker_pos = broker_positions.get(contract_symbol)
        current_premium = (broker_pos or {}).get("current_price", 0.0) or 0.0
        if current_premium <= 0:
            # Fallback: pull a fresh snapshot for this single contract
            try:
                from core.options_data import _clients
                data_client, _ = _clients()
                from alpaca.data.requests import OptionSnapshotRequest
                req = OptionSnapshotRequest(symbol_or_symbols=[contract_symbol])
                snaps = data_client.get_option_snapshot(req)
                snap = snaps.get(contract_symbol)
                quote = getattr(snap, "latest_quote", None) if snap else None
                if quote:
                    bid = float(getattr(quote, "bid_price", 0) or 0)
                    ask = float(getattr(quote, "ask_price", 0) or 0)
                    if bid > 0 and ask > 0:
                        current_premium = (bid + ask) / 2
            except Exception as e:
                logger.warning(f"Could not snapshot {contract_symbol} for exit check: {e}")
                continue

        should_exit, reason = RiskManager.should_exit_option(pos.entry_premium, current_premium, pos.dte())
        if not should_exit:
            continue
        qty = pos.qty
        # Sell slightly below mid to cross the spread — DAY limit order
        limit = round(max(current_premium * 0.98, 0.05), 2)
        order_id = sell_option(contract_symbol, qty, limit, reason, regime_name)
        if order_id:
            pnl = state.close_option_position(contract_symbol, current_premium)
            logger.info(f"CLOSED {contract_symbol} x{qty} @ ${current_premium:.2f} | "
                        f"P&L ${pnl:+.2f} | reason: {reason}")

    # ── 2. Daily-loss / drawdown-halving still applies (shared counter) ────────
    start_val = state.peak_equity or portfolio_value
    RiskManager.check_daily_loss(portfolio_value, start_val, state)

    # ── 3. Entry scan — directional calls/puts only in Phase 1 ─────────────────
    cfg = rc.load()
    max_daily = cfg.get("options_max_daily_usd", 1000.0)
    remaining = max_daily - state.options_daily_spent
    if remaining <= 10:
        logger.info(f"Options daily cap ${max_daily} consumed (${state.options_daily_spent:.2f}).")
        return

    watchlist = get_regime_watchlist(regime)
    # Per-trade budget: spread remaining budget across up to 4 fresh entries
    slots_left = max(1, 4 - len(state.options_positions))
    per_trade_budget = min(remaining, remaining / slots_left)

    # Score every candidate first so we allocate budget to the strongest signals
    picks = []
    for symbol in watchlist:
        if any(op.underlying == symbol for op in state.options_positions.values()):
            continue  # already have exposure on this underlying
        df = fetch_historical(symbol, days=120)
        if df.empty or len(df) < 65:
            continue
        sig = swing_signal(df, symbol=symbol)
        score = sig.get("score", 0.0)
        if abs(score) < 0.6:
            continue
        pick = select_trade(symbol, score, regime_name, per_trade_budget)
        if pick is None:
            continue
        picks.append(pick)

    picks.sort(key=lambda p: abs(p.score), reverse=True)

    for pick in picks:
        if state.options_daily_spent + pick.total_cost > max_daily:
            logger.info(f"Skipping {pick.underlying} {pick.strategy}: would exceed daily cap.")
            continue
        verdict = RiskManager.approve_options_trade(pick.contract.symbol, pick.total_cost, state)
        if not verdict.allowed:
            logger.warning(f"Options trade blocked: {pick.contract.symbol} — {verdict.reason}")
            continue
        # If risk trimmed the budget, recompute qty against the same per-contract cost
        per_contract = pick.limit_price * 100
        affordable_qty = int(verdict.adjusted_dollars // per_contract)
        if affordable_qty < 1:
            continue

        order_id = buy_option(pick.contract.symbol, affordable_qty, pick.limit_price, regime_name)
        if not order_id:
            continue
        cost = affordable_qty * per_contract
        state.options_daily_spent += cost
        state.options_positions[pick.contract.symbol] = OptionsPosition(
            contract_symbol=pick.contract.symbol,
            underlying=pick.underlying,
            side=pick.contract.side,
            strike=pick.contract.strike,
            expiry=pick.contract.expiry.isoformat() if hasattr(pick.contract.expiry, "isoformat") else str(pick.contract.expiry),
            qty=affordable_qty,
            entry_premium=pick.limit_price,
            entry_date=datetime.now(timezone.utc).date().isoformat(),
            regime_at_entry=regime_name,
            strategy=pick.strategy,
        )
        state.save()
        logger.info(f"OPENED {pick.strategy} {pick.contract.symbol} x{affordable_qty} "
                    f"@ ${pick.limit_price:.2f} (cost ${cost:.2f}) — {pick.reasoning}")


def _fetch_option_mid(contract_symbol: str) -> float:
    """Fetch current mid for a single option contract via Alpaca snapshot. 0 on failure."""
    try:
        from core.options_data import _clients
        from alpaca.data.requests import OptionSnapshotRequest
        data_client, _ = _clients()
        snaps = data_client.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=[contract_symbol])
        )
        snap = snaps.get(contract_symbol)
        quote = getattr(snap, "latest_quote", None) if snap else None
        if quote:
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
    except Exception as e:
        logger.warning(f"_fetch_option_mid({contract_symbol}): {e}")
    return 0.0


def _current_spread_value(pos: MultiLegPosition) -> float:
    """Per-unit absolute dollars to close the spread right now."""
    total = 0.0
    for leg in pos.legs:
        mid = _fetch_option_mid(leg.contract_symbol)
        if mid <= 0:
            return 0.0
        if leg.side == "long":
            total += mid   # we'd sell = give up this value
        else:  # short — to close we buy back
            total -= mid
    return abs(total)


def multi_leg_execute_phase(detector: RegimeDetector, state: BotState, regime: int, portfolio_value: float):
    """Vertical spreads + iron condor execution."""
    from executor.options_strategies import select_spread_trade, select_iron_condor
    from core.market_data import latest_quote

    logger.info("═══ MULTI-LEG EXECUTION PHASE ═══")
    if not supports_multi_leg():
        logger.warning("Broker does not support multi-leg orders — skipping.")
        return

    cfg = rc.load()
    regime_name = detector.regime_name(regime)

    # Cancel stale limit orders before trying to close/open — prevents the
    # "held_for_orders" lockout where stale orders block new MLEG submits.
    try:
        from executor.order_executor import cancel_stale_orders, wait_for_order_fill
        cancelled = cancel_stale_orders(max_age_seconds=180)
        if cancelled:
            logger.warning(f"cancelled {cancelled} stale order(s) before multi-leg phase")
    except Exception as e:
        logger.debug(f"cancel_stale_orders failed: {e}")

    # ── 1. Exits on existing multi-leg positions ───────────────────────────────
    for key, pos in list(state.multi_leg_positions.items()):
        current_value = _current_spread_value(pos)
        if current_value <= 0:
            continue
        is_orphan = getattr(pos, "origin", "") == "reconciled_orphan"
        should_exit, reason = RiskManager.should_exit_multi_leg(
            pos.net_entry, current_value, pos.qty, pos.dte(),
            skip_sl=is_orphan,
        )
        # Orphan emergency SL: when the bot can't trust the entry price, fall
        # back to proximity-to-max-loss as the safety gauge. For credit
        # spreads, max loss ≈ wing_width − credit (per unit). If current
        # close value is ≥ 75% of the wing width, the position is within
        # striking distance of max loss → force close regardless of entry.
        emergency = False
        if is_orphan and not should_exit and pos.is_credit:
            w = pos.width
            if w > 0 and current_value >= w * 0.75:
                should_exit, reason, emergency = True, f"orphan emergency SL ({current_value:.2f} vs width {w:.1f})", True
        if not should_exit:
            continue
        # Build closing legs: reverse each leg's side/intent
        closing_legs = []
        for leg in pos.legs:
            closing_legs.append({
                "contract_symbol": leg.contract_symbol,
                "side": "sell" if leg.side == "long" else "buy",
                "position_intent": "close",
                "ratio_qty": 1,
            })
        # Closing a credit spread = BUY to close (net debit paid); closing a debit = SELL (net credit)
        close_side = "buy" if pos.is_credit else "sell"
        # SL / emergency exits use market orders for guaranteed fast fill;
        # TP and DTE exits use limit orders to capture a good price.
        is_sl_exit = emergency or ("SL" in reason) or ("emergency" in reason)
        order_id = submit_multi_leg_order(
            legs=closing_legs, qty=pos.unit_count, net_limit_price=current_value,
            order_side=close_side, strategy=f"close_{pos.strategy}",
            regime_name=regime_name, use_market=is_sl_exit,
        )
        if not order_id:
            logger.error(f"MLEG close rejected for {key} ({reason})")
            continue
        # Fast fill verification — poll up to 8s. If a limit-order TP/DTE
        # exit doesn't fill that fast, leave it (not urgent); it'll get
        # picked up on the next cycle. If an SL market order doesn't fill
        # fast, there's an upstream broker issue — log loudly.
        try:
            status = wait_for_order_fill(order_id, timeout_sec=8.0)
        except Exception:
            status = "unknown"
        if is_sl_exit and status != "filled":
            logger.error(f"SL market close {order_id} status={status} — broker issue, position may still be open")
        pnl = state.close_multi_leg_position(key, current_value)
        logger.info(f"MLEG CLOSED {pos.strategy} {pos.underlying} x{pos.unit_count} "
                    f"@ net ${current_value:.2f} | P&L ${pnl:+.2f} | {reason} | order={order_id} status={status}")

    # ── 2. Entry scan ──────────────────────────────────────────────────────────
    spreads_on = cfg.get("spreads_enabled", False)
    ic_on      = cfg.get("iron_condor_enabled", False)
    if not (spreads_on or ic_on):
        logger.info("Spreads + iron condor disabled — skipping entries.")
        return

    max_daily = cfg.get("options_max_daily_usd", 1000.0)
    remaining = max_daily - state.options_daily_spent
    if remaining <= 50:
        logger.info(f"Options daily cap ${max_daily} consumed — no new multi-leg entries.")
        return

    # Budget per new trade: spread across up to 3 fresh picks, AND cap any
    # single trade to a fraction of the deployed-capital limit so a single
    # bad entry can't wipe the options book (JPM-x5 lesson — one trade at
    # 18% of deployed cap is too concentrated).
    open_count = len(state.multi_leg_positions)
    slots_left = max(1, 3 - open_count)
    max_deployed = float(cfg.get("options_max_deployed_usd", 10000.0))
    per_trade_pct = float(cfg.get("options_max_per_trade_pct", 0.15))
    per_trade_hard_cap = max_deployed * per_trade_pct
    per_trade_budget = min(remaining, remaining / slots_left, per_trade_hard_cap)
    logger.info(f"Multi-leg per-trade budget: ${per_trade_budget:.0f} "
                f"(remaining ${remaining:.0f} · slots {slots_left} · hard-cap ${per_trade_hard_cap:.0f})")

    watchlist = get_regime_watchlist(regime)
    open_underlyings = {p.underlying for p in state.multi_leg_positions.values()}

    picks: list = []
    for symbol in watchlist:
        if symbol in open_underlyings:
            continue
        df = fetch_historical(symbol, days=120)
        if df.empty or len(df) < 65:
            continue
        sig = swing_signal(df, symbol=symbol)
        score = sig.get("score", 0.0)

        if spreads_on and abs(score) >= 0.6:
            pick = select_spread_trade(symbol, score, regime_name, per_trade_budget)
            if pick:
                picks.append(pick)
                continue
        if ic_on and abs(score) < 0.30:
            spot = latest_quote(symbol)
            if not spot:
                continue
            pick = select_iron_condor(symbol, score, regime_name, spot, per_trade_budget)
            if pick:
                picks.append(pick)

    picks.sort(key=lambda p: abs(p.score), reverse=True)

    for pick in picks:
        if state.options_daily_spent + pick.capital_at_risk > max_daily:
            logger.info(f"Skipping {pick.underlying} {pick.strategy}: cap-at-risk would exceed daily limit.")
            continue
        # Build leg requests for the broker
        legs_req = []
        for contract, side in pick.legs:
            action = "buy" if side == "long" else "sell"
            legs_req.append({
                "contract_symbol": contract.symbol,
                "side": action,
                "position_intent": "open",
                "ratio_qty": 1,
            })
        # Credit structures: order_side = "sell" (we receive). Debit: "buy".
        order_side = "sell" if pick.qty < 0 else "buy"
        qty = pick.qty if pick.qty > 0 else -pick.qty
        order_id = submit_multi_leg_order(
            legs=legs_req, qty=qty, net_limit_price=pick.net_limit,
            order_side=order_side, strategy=pick.strategy, regime_name=regime_name,
        )
        if not order_id:
            continue
        # Track position
        key = f"{pick.underlying}_{pick.strategy}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        tracker_legs = []
        for contract, side in pick.legs:
            tracker_legs.append(OptionLeg(
                contract_symbol=contract.symbol,
                side=side,
                contract_type=contract.side,   # "call" | "put"
                strike=contract.strike,
                expiry=contract.expiry.isoformat() if hasattr(contract.expiry, "isoformat") else str(contract.expiry),
                entry_price=contract.mid,
                ratio_qty=1,
            ))
        state.multi_leg_positions[key] = MultiLegPosition(
            key=key, strategy=pick.strategy, underlying=pick.underlying,
            legs=tracker_legs, qty=pick.qty, net_entry=pick.net_limit,
            entry_date=datetime.now(timezone.utc).date().isoformat(),
            regime_at_entry=regime_name,
        )
        # Credit trades consume capital-at-risk against the daily cap (not premium)
        state.options_daily_spent += pick.capital_at_risk
        state.save()
        logger.info(f"MLEG OPENED {pick.strategy} {pick.underlying} x{qty} "
                    f"@ net ${pick.net_limit:.2f} — risk ${pick.capital_at_risk:.0f} — {pick.reasoning}")


def refresh_dynamic_watchlist(state: BotState, force: bool = False) -> bool:
    """Refresh today's dynamic watchlist once per day (pre-market).

    Returns True if a refresh was performed. Skips when:
      - already refreshed today (unless force=True)
      - dynamic_watchlist_enabled is False
    """
    cfg = rc.load()
    if not cfg.get("dynamic_watchlist_enabled", True):
        return False

    from datetime import date as _d
    today = _d.today().isoformat()
    if state.dynamic_watchlist_date == today and not force:
        return False

    try:
        from discovery.dynamic_watchlist import build_daily_watchlist
    except Exception as e:
        logger.warning(f"dynamic watchlist import failed: {e}")
        return False

    base = rc.get_watchlist()
    limit     = int(cfg.get("dynamic_watchlist_limit", 20))
    min_price = float(cfg.get("dynamic_watchlist_min_price", 5.0))
    min_oi    = int(cfg.get("dynamic_watchlist_min_oi", 500))

    logger.info(f"═══ DYNAMIC WATCHLIST REFRESH ═══ (limit={limit}, min_price=${min_price}, min_oi={min_oi})")
    try:
        picks = build_daily_watchlist(base, limit=limit, min_price=min_price, min_oi=min_oi)
    except Exception as e:
        logger.warning(f"build_daily_watchlist failed: {e}")
        return False

    from datetime import datetime as _dt, timezone as _tz
    state.dynamic_watchlist = picks
    state.dynamic_watchlist_date = today
    state.dynamic_watchlist_refreshed_at = _dt.now(_tz.utc).isoformat()
    state.save()
    if picks:
        sources = {}
        for p in picks:
            sources.setdefault(p["source"], []).append(p["symbol"])
        summary = " | ".join(f"{src}: {','.join(syms)}" for src, syms in sources.items())
        logger.info(f"Dynamic watchlist ({len(picks)}): {summary}")
    else:
        logger.info("Dynamic watchlist: no eligible movers passed filters today.")
    return True


def _force_top_score_paper(detector: RegimeDetector, state: BotState, regime: int,
                            portfolio_value: float) -> bool:
    """Paper-only safety valve. After `paper_force_after_HH:MM` ET, if no
    option trade has fired today, open a minimum-size position on the symbol
    with the highest |score|. Returns True if a trade was placed.

    Caller is responsible for the time + enabled gate — this function just executes.
    """
    from executor.options_strategies import select_trade
    from datetime import datetime as _dt
    import pytz

    regime_name = detector.regime_name(regime)
    watchlist = get_regime_watchlist(regime)

    # Find best symbol by |score|
    best = None
    for symbol in watchlist:
        df = fetch_historical(symbol, days=120)
        if df.empty or len(df) < 65:
            continue
        sig = swing_signal(df, symbol=symbol)
        score = sig.get("score", 0.0)
        if best is None or abs(score) > abs(best[1]):
            best = (symbol, score)
    if not best:
        return False
    symbol, score = best

    # Force a directional pick based on score sign, bypassing normal threshold
    # Use a tiny per-trade budget — this is observability, not conviction
    forced_budget = min(100.0, rc.load().get("options_max_daily_usd", 1000.0))
    # Mutate to clear threshold: the selector does its own regime filter, so
    # we synthesize a score that clears the gate, keeping the sign of the real score.
    signed_score = 0.99 if score >= 0 else -0.99
    # But keep the regime filter honest — don't fire long-put in BULL etc.
    from executor.options_strategies import _BULLISH_REGIMES, _BEARISH_REGIMES
    if signed_score > 0 and regime_name not in _BULLISH_REGIMES:
        return False
    if signed_score < 0 and regime_name not in _BEARISH_REGIMES:
        return False

    pick = select_trade(symbol, signed_score, regime_name, forced_budget)
    if pick is None:
        logger.info(f"paper_force: no chain/strike for {symbol} (real score {score:+.2f})")
        return False

    per_contract = pick.limit_price * 100
    qty = 1  # minimum — this is observability fire, not conviction
    if per_contract > forced_budget:
        logger.info(f"paper_force: {pick.contract.symbol} ${per_contract:.2f} exceeds ${forced_budget} — skip")
        return False

    order_id = buy_option(pick.contract.symbol, qty, pick.limit_price,
                          regime_name=f"{regime_name} FORCED_PAPER")
    if not order_id:
        return False
    cost = per_contract
    state.options_daily_spent += cost
    state.options_positions[pick.contract.symbol] = OptionsPosition(
        contract_symbol=pick.contract.symbol,
        underlying=pick.underlying,
        side=pick.contract.side,
        strike=pick.contract.strike,
        expiry=pick.contract.expiry.isoformat() if hasattr(pick.contract.expiry, "isoformat") else str(pick.contract.expiry),
        qty=qty, entry_premium=pick.limit_price,
        entry_date=datetime.now(timezone.utc).date().isoformat(),
        regime_at_entry=regime_name,
        strategy="forced_paper",
    )
    state.save()
    logger.info(f"⚠ FORCED_PAPER {pick.strategy} {pick.contract.symbol} x{qty} "
                f"@ ${pick.limit_price:.2f} — real score {score:+.2f} on {symbol} — "
                f"observability trade")
    return True


def intraday_execute_phase(detector: RegimeDetector, state: BotState, regime: int,
                           portfolio_value: float):
    """Opening Range Breakout strategy, filtered by HMM regime.

    - Builds opening range for each watchlist symbol (first 15 min of session).
    - After range, scans for breakouts and fires short-dated options.
    - Flattens all intraday positions at 15:55 ET regardless of P&L.
    """
    from intraday.orb import (
        compute_opening_range, detect_breakout, select_orb_trade,
        is_range_building_window, is_range_tradeable_window, is_force_close_window,
    )
    from core.market_data import latest_quote

    cfg = rc.load()
    regime_name = detector.regime_name(regime)
    opening_range_min = int(cfg.get("intraday_opening_range_min", 15))
    fc_hour = int(cfg.get("intraday_force_close_hour_et", 15))
    fc_min  = int(cfg.get("intraday_force_close_min_et", 55))

    # ── 1. EOD force-flatten all intraday positions ────────────────────────────
    if is_force_close_window(fc_hour, fc_min):
        from executor.order_executor import cancel_order as _cancel_order
        flatten_count = 0
        for key, pos in list(state.options_positions.items()):
            if not pos.intraday:
                continue
            # Cancel the broker-side OCO first so it doesn't fire on an
            # already-closed position (would become an orphan naked sell).
            if getattr(pos, "stop_order_id", ""):
                _cancel_order(pos.stop_order_id)
            current_premium = _fetch_option_mid(pos.contract_symbol)
            if current_premium <= 0:
                current_premium = max(pos.entry_premium * 0.5, 0.05)
            limit = round(max(current_premium * 0.98, 0.05), 2)
            order_id = sell_option(pos.contract_symbol, abs(pos.qty), limit,
                                   reason="eod_flatten_intraday", regime_name=regime_name)
            if order_id:
                pnl = state.close_option_position(pos.contract_symbol, current_premium)
                logger.info(f"INTRADAY EOD CLOSE {pos.contract_symbol} x{abs(pos.qty)} "
                            f"@ ${current_premium:.2f} | P&L ${pnl:+.2f}")
                flatten_count += 1
        if flatten_count:
            logger.info(f"Intraday EOD flatten: {flatten_count} positions closed.")
        return

    # ── 2. Skip if it's not the ORB-tradeable window ───────────────────────────
    if is_range_building_window(opening_range_min):
        logger.info(f"Intraday: still building opening range ({opening_range_min}min). No entries.")
        return
    if not is_range_tradeable_window(opening_range_min, fc_hour, fc_min):
        return  # Outside trade window entirely

    # ── 3. Exit existing intraday positions on TP/SL ───────────────────────────
    tp = float(cfg.get("intraday_take_profit_pct", 0.40))
    sl = float(cfg.get("intraday_stop_loss_pct", 0.30))
    for key, pos in list(state.options_positions.items()):
        if not pos.intraday:
            continue
        current_premium = _fetch_option_mid(pos.contract_symbol)
        if current_premium <= 0:
            continue
        change = (current_premium - pos.entry_premium) / pos.entry_premium
        reason = None
        if change >= tp:
            reason = f"intraday TP +{change:.1%}"
        elif change <= -sl:
            reason = f"intraday SL {change:.1%}"
        if reason:
            limit = round(max(current_premium * 0.98, 0.05), 2)
            order_id = sell_option(pos.contract_symbol, abs(pos.qty), limit,
                                   reason=reason, regime_name=regime_name)
            if order_id:
                pnl = state.close_option_position(pos.contract_symbol, current_premium)
                logger.info(f"INTRADAY CLOSE {pos.contract_symbol} @ ${current_premium:.2f} | "
                            f"P&L ${pnl:+.2f} | {reason}")

    # ── 4. Scan for breakouts ──────────────────────────────────────────────────
    max_daily = float(cfg.get("intraday_max_daily_usd", 500.0))
    remaining = max_daily - state.intraday_daily_spent
    if remaining <= 20:
        return

    watchlist = get_regime_watchlist(regime)
    open_underlyings = {p.underlying for p in state.options_positions.values() if p.intraday}
    per_trade_budget = min(remaining, remaining / max(1, 3 - len(open_underlyings)))

    for symbol in watchlist:
        if symbol in open_underlyings:
            continue
        or_range = compute_opening_range(symbol, opening_range_min)
        if not or_range:
            continue
        spot = latest_quote(symbol)
        if not spot:
            continue
        direction = detect_breakout(symbol, or_range, spot)
        if not direction:
            continue
        pick = select_orb_trade(symbol, direction, regime_name, or_range, spot, per_trade_budget)
        if not pick:
            continue
        if state.intraday_daily_spent + pick.total_cost > max_daily:
            continue

        # Intraday positions are sensitive — attach a broker-side OCO
        # (TP + SL) so protection doesn't depend on our 5-min polling cycle.
        order_id = buy_option(
            pick.contract_symbol, pick.qty, pick.limit_price,
            regime_name=f"{regime_name} INTRADAY_ORB",
            protective_tp_pct=tp, protective_sl_pct=sl,
        )
        if not order_id:
            continue
        from executor.order_executor import last_protection_order_id
        stop_oid = last_protection_order_id()
        state.intraday_daily_spent += pick.total_cost
        state.options_positions[pick.contract_symbol] = OptionsPosition(
            contract_symbol=pick.contract_symbol,
            underlying=pick.underlying,
            side="call" if pick.direction == "bullish" else "put",
            strike=0.0,  # selector has it — not critical for intraday tracking
            expiry="",   # filled opportunistically
            qty=pick.qty, entry_premium=pick.limit_price,
            entry_date=datetime.now(timezone.utc).date().isoformat(),
            regime_at_entry=regime_name,
            strategy="intraday_orb",
            intraday=True,
            stop_order_id=stop_oid,
        )
        state.save()
        logger.info(f"INTRADAY OPENED {pick.contract_symbol} x{pick.qty} "
                    f"@ ${pick.limit_price:.2f} (cost ${pick.total_cost:.0f}) — {pick.reasoning}")
        open_underlyings.add(pick.underlying)
        if len(open_underlyings) >= 3:
            break


def covered_call_phase(detector: RegimeDetector, state: BotState, regime: int, portfolio_value: float):
    """Write short calls against 100-share stock lots. Optional auto-acquire
    gated by stock_max_daily_usd cap — skips cleanly if the cap is too low."""
    from core.options_data import get_option_chain, pick_contract, _clients

    if not supports_options():
        logger.warning("CC: broker does not support options — skipping covered-call phase.")
        return

    cfg = rc.load()
    regime_name = detector.regime_name(regime)

    # ── 1. Close existing short calls at TP / DTE ──────────────────────────────
    for key, pos in list(state.options_positions.items()):
        if not pos.is_short or pos.strategy != "short_call_covered":
            continue
        current_premium = 0.0
        try:
            data_client, _ = _clients()
            from alpaca.data.requests import OptionSnapshotRequest
            snaps = data_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=[pos.contract_symbol])
            )
            snap = snaps.get(pos.contract_symbol)
            quote = getattr(snap, "latest_quote", None) if snap else None
            if quote:
                bid = float(getattr(quote, "bid_price", 0) or 0)
                ask = float(getattr(quote, "ask_price", 0) or 0)
                if bid > 0 and ask > 0:
                    current_premium = (bid + ask) / 2
        except Exception as e:
            logger.warning(f"CC snapshot {pos.contract_symbol}: {e}")
            continue
        if current_premium <= 0:
            continue

        should_exit, reason = RiskManager.should_exit_option(
            pos.entry_premium, current_premium, pos.dte(), is_short=True
        )
        if not should_exit:
            continue
        # Buy-to-close slightly above mid
        limit = round(current_premium * 1.02, 2)
        order_id = buy_option(pos.contract_symbol, abs(pos.qty), limit,
                              regime_name=f"{regime_name} CC_close")
        if order_id:
            pnl = state.close_option_position(pos.contract_symbol, current_premium)
            logger.info(f"CC CLOSED {pos.contract_symbol} x{abs(pos.qty)} @ ${current_premium:.2f} | "
                        f"P&L ${pnl:+.2f} | {reason}")

    # Write calls only in non-bearish regimes — don't cap upside into a rebound.
    if regime_name in ("crash", "bear"):
        logger.info(f"CC: skipping writes in {regime_name} regime")
        return

    try:
        stock_holdings = get_stock_positions()
    except Exception as e:
        logger.warning(f"CC get_stock_positions: {e}")
        stock_holdings = {}

    watchlist = rc.get_watchlist()
    already_short = {p.underlying for p in state.options_positions.values() if p.is_short}

    target_delta = cfg.get("covered_call_target_delta", 0.25)
    dte_min = int(cfg.get("covered_call_target_dte_min", 30))
    dte_max = int(cfg.get("covered_call_target_dte_max", 45))

    # ── 2. Write calls against eligible holdings ───────────────────────────────
    wrote_any = False
    for sym, qty in stock_holdings.items():
        if sym not in watchlist or qty < 100 or sym in already_short:
            continue
        lots = int(qty // 100)
        chain = get_option_chain(sym, dte_min=dte_min, dte_max=dte_max, sides=["call"])
        if not chain:
            logger.info(f"CC {sym}: no call chain in {dte_min}-{dte_max} DTE")
            continue
        target = pick_contract(chain, target_delta=target_delta, side="call")
        if not target or target.mid <= 0.05:
            logger.info(f"CC {sym}: no suitable OTM call near {target_delta}Δ")
            continue
        limit = round(max(target.mid * 0.98, 0.05), 2)
        order_id = sell_option(target.symbol, lots, limit,
                               reason="open_covered_call", regime_name=regime_name)
        if not order_id:
            continue
        state.options_positions[target.symbol] = OptionsPosition(
            contract_symbol=target.symbol,
            underlying=sym,
            side="call",
            strike=target.strike,
            expiry=target.expiry.isoformat() if hasattr(target.expiry, "isoformat") else str(target.expiry),
            qty=-lots,  # negative = short
            entry_premium=limit,
            entry_date=datetime.now(timezone.utc).date().isoformat(),
            regime_at_entry=regime_name,
            strategy="short_call_covered",
        )
        state.save()
        credit = limit * lots * 100
        logger.info(f"CC OPENED {target.symbol} x{lots} @ ${limit:.2f} (credit ${credit:.2f}) "
                    f"— {sym} held {qty}, Δ={target.delta:.2f}, DTE={target.dte}")
        wrote_any = True

    # ── 3. Optional auto-acquire — strict cap enforcement ──────────────────────
    if not wrote_any and cfg.get("covered_call_auto_acquire", False):
        stock_cap = cfg.get("stock_max_daily_usd", 5000.0)
        remaining = stock_cap - state.daily_spent
        candidates = []
        for sym in watchlist:
            if sym in stock_holdings and stock_holdings[sym] >= 100:
                continue
            price = latest_quote(sym)
            if not price or price <= 0:
                continue
            cost = price * 100
            candidates.append((cost, sym, price))
        candidates.sort()
        affordable = [c for c in candidates if c[0] <= remaining]
        if not affordable:
            cheapest = candidates[0] if candidates else None
            if cheapest:
                logger.info(
                    f"CC auto-acquire: no symbol fits ${remaining:.0f} remaining cap "
                    f"(cheapest: {cheapest[1]} @ ${cheapest[2]:.2f} × 100 = ${cheapest[0]:.0f}). "
                    f"Raise stock_max_daily_usd to enable."
                )
            return
        cost, sym, price = affordable[0]
        verdict = RiskManager.check_daily_spend(state, cost)
        if not verdict.allowed:
            logger.warning(f"CC auto-acquire {sym}: {verdict.reason}")
            return
        order_id = buy_fractional(sym, verdict.adjusted_dollars,
                                  regime_name=f"{regime_name} CC_acquire")
        if order_id:
            state.daily_spent += verdict.adjusted_dollars
            state.save()
            logger.info(f"CC ACQUIRED {sym}: ${verdict.adjusted_dollars:.2f} for ~100 shares @ "
                        f"${price:.2f}. Writing call on next cycle after fill.")


def main_loop():
    logger.info(f"🚀 Montrai starting (mode={TRADING_MODE})")

    if TRADING_MODE == "live":
        login()

    state = BotState.load()
    detector = RegimeDetector.load()

    # Seed peak_equity from broker baseline so the drawdown circuit breaker
    # never starts measuring against a snapshot we happened to take while
    # already drawn-down. Ratcheting continues as usual from here.
    try:
        from executor.order_executor import get_account_baseline
        broker_baseline = get_account_baseline()
        if broker_baseline > state.peak_equity:
            logger.info(f"peak_equity seeded from broker: ${state.peak_equity:.2f} → ${broker_baseline:.2f}")
            state.peak_equity = broker_baseline
            state.save()
    except Exception as e:
        logger.warning(f"broker baseline seed failed: {e}")

    # Initial train if no saved model
    if detector.model is None:
        detector = train_phase(detector)

    # Regime cache — during intraday mode we skip the HMM recompute on most
    # cycles since regime shifts rarely occur within a session. Peak-drawdown
    # check still fires every cycle (it's cheap). Default 30 min between HMM
    # recomputes; override via runtime.hmm_cache_min_intraday.
    last_regime: Optional[int] = None
    last_regime_ts: Optional[datetime] = None

    while True:
        cfg = rc.load()
        # Intraday override — ORB strategy wants 5-min cycles during the regular
        # session. Outside market hours the base swing interval still applies.
        if cfg.get("intraday_enabled", False) and is_market_open():
            signal_interval = int(cfg.get("intraday_scan_interval_min",
                                          cfg["signal_interval_minutes"]))
        else:
            signal_interval = cfg["signal_interval_minutes"]
        retrain_every = max(1, int(7 * 24 * 60 / signal_interval))
        stocks_on  = cfg.get("stock_trading_enabled", False)
        options_on = cfg.get("options_trading_enabled", True)

        if RiskManager.check_lockout():
            logger.critical("Lockout file present. Sleeping 60s and re-checking.")
            write_heartbeat(state, regime=-1, regime_name="lockout", mode="halted")
            time.sleep(60)
            continue

        # On the very first cycle, watchlist refresh + broker reconcile can
        # take a few minutes; write an "initializing" heartbeat so the
        # dashboard doesn't show the prior run's stale timestamp. Later
        # cycles already have a fresh end-of-cycle heartbeat to carry.
        if state.cycles == 0:
            write_heartbeat(state, regime=-1, regime_name="initializing", mode="initializing")

        # Per-cycle broker reconcile — Alpaca is source of truth. Adopts
        # orphans, prunes stale entries, stamps last_broker_sync_at.
        try:
            reconcile_from_broker(state)
        except Exception as e:
            logger.warning(f"reconcile_from_broker failed: {e}")

        # Pre-market daily hook: refresh dynamic watchlist once per day.
        # Runs regardless of market-open gate since the screener works pre-market.
        try:
            refresh_dynamic_watchlist(state)
        except Exception as e:
            logger.warning(f"refresh_dynamic_watchlist raised: {e}")

        if not is_market_open():
            logger.info("Market closed. Sleeping 5 minutes.")
            write_heartbeat(state, regime=-1, regime_name="market_closed", mode="sleeping")
            time.sleep(300)
            continue

        try:
            # Paper mode against Alpaca still has a real simulated equity balance —
            # pull it from the broker so drawdown tracking uses the actual $100k
            # starting equity, not a hard-coded $10k fallback.
            try:
                portfolio_value = get_portfolio_value()
                if not portfolio_value or portfolio_value <= 0:
                    portfolio_value = state.peak_equity or 100000.0
            except Exception:
                portfolio_value = state.peak_equity or 100000.0

            # HMM monitoring: skip during intraday mode if the last run is
            # fresh enough. Drawdown check still runs every cycle below.
            intraday_active = cfg.get("intraday_enabled", False) and is_market_open()
            hmm_cache_min = int(cfg.get("hmm_cache_min_intraday", 30))
            now_utc = datetime.now(timezone.utc)
            cache_fresh = (
                intraday_active
                and last_regime is not None
                and last_regime_ts is not None
                and (now_utc - last_regime_ts).total_seconds() / 60 < hmm_cache_min
            )
            if cache_fresh:
                regime = last_regime
                age_min = (now_utc - last_regime_ts).total_seconds() / 60
                logger.info(f"HMM cached: regime={regime} ({detector.regime_name(regime)}) "
                            f"· last run {age_min:.1f}m ago · next in {hmm_cache_min - age_min:.1f}m")
                # Keep the cheap safety check running every cycle
                if RiskManager.check_peak_drawdown(portfolio_value, state):
                    logger.critical("Peak drawdown lockout triggered. Exiting.")
                    sys.exit(1)
            else:
                regime = monitor_phase(detector, state, portfolio_value)
                last_regime = regime
                last_regime_ts = now_utc

            if stocks_on:
                execute_phase(detector, state, regime, portfolio_value)
            else:
                logger.info("Stock trading disabled — skipping stock execute phase.")
            if options_on:
                options_execute_phase(detector, state, regime, portfolio_value)
            else:
                logger.info("Options trading disabled — skipping options execute phase.")
            if options_on and (cfg.get("spreads_enabled", False) or cfg.get("iron_condor_enabled", False)):
                multi_leg_execute_phase(detector, state, regime, portfolio_value)
            if cfg.get("covered_call_enabled", False):
                covered_call_phase(detector, state, regime, portfolio_value)
            if cfg.get("intraday_enabled", False) and options_on:
                intraday_execute_phase(detector, state, regime, portfolio_value)

            # Paper-only safety valve — fires at end-of-session if nothing else filled
            if (
                cfg.get("paper_force_top_score", False)
                and TRADING_MODE == "paper"
                and state.options_daily_spent == 0.0
                and state.daily_spent == 0.0
            ):
                import pytz as _tz
                _et_now = datetime.now(_tz.timezone("America/New_York"))
                force_h = int(cfg.get("paper_force_after_hour_et", 15))
                force_m = int(cfg.get("paper_force_after_min_et", 30))
                if (_et_now.hour, _et_now.minute) >= (force_h, force_m) and _et_now.weekday() < 5:
                    _force_top_score_paper(detector, state, regime, portfolio_value)

            state.cycles += 1
            state.save()
            write_heartbeat(state, regime, detector.regime_name(regime),
                            mode=("options" if options_on else "idle") if not stocks_on
                                 else ("both" if options_on else "stocks"))
            if state.cycles % retrain_every == 0:
                detector = train_phase(detector)

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Saving state.")
            state.save()
            try:
                os.remove(HEARTBEAT_FILE)
            except Exception:
                pass
            break
        except Exception as e:
            logger.error(f"Unhandled error in main loop: {e}", exc_info=True)

        logger.info(f"Sleeping {signal_interval} minutes until next cycle...")
        time.sleep(signal_interval * 60)


def backtest_all():
    """Run walk-forward backtest on every symbol in the watchlist. Save combined CSV."""
    from backtester.walk_forward import run_walk_forward
    from pathlib import Path
    from config.settings import LOG_DIR

    watchlist = rc.get_watchlist()
    logger.info(f"Running full-watchlist backtest: {watchlist}")

    all_folds = []
    for sym in watchlist:
        fold_df = run_walk_forward(sym)
        if not fold_df.empty:
            all_folds.append(fold_df)

    if not all_folds:
        logger.warning("No backtest results produced.")
        return

    combined = (
        pd.concat(all_folds)
        .groupby("symbol", as_index=False)
        .agg(
            folds=("n_trades", "count"),
            total_trades=("n_trades", "sum"),
            mean_return_pct=("total_return_pct", "mean"),
            mean_win_rate=("win_rate", "mean"),
            best_fold_pct=("total_return_pct", "max"),
            worst_fold_pct=("total_return_pct", "min"),
        )
        .sort_values("mean_return_pct", ascending=False)
        .round(3)
    )

    from datetime import datetime as _dt
    Path(LOG_DIR).mkdir(exist_ok=True)
    out = Path(LOG_DIR) / f"backtest_all_{_dt.today().strftime('%Y%m%d')}.csv"
    combined.to_csv(out, index=False)

    print("\n═══ BACKTEST SUMMARY (all symbols) ═══")
    print(combined.to_string(index=False))
    print(f"\nFull results saved → {out}")
    print("Per-symbol fold detail + trades in logs/backtest_<SYMBOL>_*.csv")


def scan_watchlist():
    """Score every watchlist symbol right now. Print ranked table. Save to CSV."""
    from pathlib import Path
    from datetime import datetime as _dt
    from config.settings import LOG_DIR

    watchlist = rc.get_watchlist()
    detector = RegimeDetector.load()
    if detector.model is None:
        logger.info("No saved HMM model — training now...")
        detector = train_phase(detector)

    spy_df = fetch_historical("SPY", days=120)
    spy_features = build_hmm_features(add_indicators(spy_df)) if not spy_df.empty else None
    regime = detector.predict_regime(spy_features) if spy_features is not None and len(spy_features) else 2
    regime_name = detector.regime_name(regime)
    logger.info(f"Current regime: {regime} ({regime_name.upper()})")

    rows = []
    for sym in watchlist:
        df = fetch_historical(sym, days=120)
        if df.empty or len(df) < 65:
            continue
        sig = swing_signal(df, symbol=sym)
        price = latest_quote(sym) or 0.0
        row = {
            "symbol": sym,
            "score": sig["score"],
            "price": round(price, 2),
            "rsi": round(sig["last"].get("rsi_14", 0), 1),
            "macd_hist": round(sig["last"].get("macd_hist", 0), 4),
            "bb_pos": round(sig["last"].get("bb_position", 0.5), 2),
            "vol_ratio": round(sig["last"].get("vol_ratio", 1.0), 2),
            "reasons": " | ".join(sig["reasons"]),
        }
        fund = sig.get("fundamentals", {})
        if fund:
            row["pe"] = fund.get("pe", "")
            row["margin"] = fund.get("margin", "")
            row["insider"] = fund.get("insider_score", "")
            row["eps_revision"] = fund.get("eps_revision_pct", "")
        rows.append(row)

    if not rows:
        print("No signals generated.")
        return

    result = (
        pd.DataFrame(rows)
        .sort_values("score", ascending=False)
        .reset_index(drop=True)
    )
    result.insert(0, "rank", result.index + 1)

    # Mark top candidates clearly
    top = result[result["score"] >= 0.6]
    watchable = result[(result["score"] >= 0.4) & (result["score"] < 0.6)]

    print(f"\n═══ WATCHLIST SCAN — {_dt.today().strftime('%Y-%m-%d')} | Regime: {regime_name.upper()} ═══")
    if not top.empty:
        print(f"\n🟢 BUY CANDIDATES (score ≥ 0.6) — {len(top)} symbol(s):")
        print(top.to_string(index=False))
    if not watchable.empty:
        print(f"\n🟡 WATCHING (0.4 ≤ score < 0.6) — {len(watchable)} symbol(s):")
        print(watchable[["rank", "symbol", "score", "price", "rsi", "reasons"]].to_string(index=False))
    print(f"\n⬜ Full ranked table ({len(result)} symbols):")
    print(result[["rank", "symbol", "score", "price", "rsi", "reasons"]].to_string(index=False))

    Path(LOG_DIR).mkdir(exist_ok=True)
    out = Path(LOG_DIR) / f"scan_{_dt.today().strftime('%Y%m%d_%H%M')}.csv"
    result.to_csv(out, index=False)
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Swing Trader")
    parser.add_argument("--backtest", action="store_true", help="Walk-forward backtest for one symbol")
    parser.add_argument("--backtest-all", action="store_true", help="Walk-forward backtest for all watchlist symbols")
    parser.add_argument("--scan", action="store_true", help="Score all watchlist symbols now — ranked buy candidates")
    parser.add_argument("--train", action="store_true", help="Retrain HMM only")
    parser.add_argument("--symbol", default="AAPL", help="Symbol for --backtest")
    args = parser.parse_args()

    if args.backtest:
        from backtester.walk_forward import run_walk_forward
        run_walk_forward(args.symbol)
    elif args.backtest_all:
        backtest_all()
    elif args.scan:
        scan_watchlist()
    elif args.train:
        d = RegimeDetector.load()
        train_phase(d)
    else:
        main_loop()
