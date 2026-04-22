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

    # ── 1. Exits on existing multi-leg positions ───────────────────────────────
    for key, pos in list(state.multi_leg_positions.items()):
        current_value = _current_spread_value(pos)
        if current_value <= 0:
            continue
        should_exit, reason = RiskManager.should_exit_multi_leg(
            pos.net_entry, current_value, pos.qty, pos.dte()
        )
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
        # Pay/receive current net value as limit
        order_id = submit_multi_leg_order(
            legs=closing_legs, qty=pos.unit_count, net_limit_price=current_value,
            order_side=close_side, strategy=f"close_{pos.strategy}",
            regime_name=regime_name,
        )
        if order_id:
            pnl = state.close_multi_leg_position(key, current_value)
            logger.info(f"MLEG CLOSED {pos.strategy} {pos.underlying} x{pos.unit_count} "
                        f"@ net ${current_value:.2f} | P&L ${pnl:+.2f} | {reason}")

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

    # Budget per new trade: spread across up to 3 fresh picks
    open_count = len(state.multi_leg_positions)
    slots_left = max(1, 3 - open_count)
    per_trade_budget = min(remaining, remaining / slots_left)

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

    # Initial train if no saved model
    if detector.model is None:
        detector = train_phase(detector)

    while True:
        cfg = rc.load()
        signal_interval = cfg["signal_interval_minutes"]
        retrain_every = max(1, int(7 * 24 * 60 / signal_interval))
        stocks_on  = cfg.get("stock_trading_enabled", False)
        options_on = cfg.get("options_trading_enabled", True)

        if RiskManager.check_lockout():
            logger.critical("Lockout file present. Sleeping 60s and re-checking.")
            write_heartbeat(state, regime=-1, regime_name="lockout", mode="halted")
            time.sleep(60)
            continue

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
            regime = monitor_phase(detector, state, portfolio_value)

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
