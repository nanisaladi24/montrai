"""
Main Orchestrator — Train → Monitor → Execute loop.

Run modes:
  python main.py               # live trading loop
  python main.py --backtest    # run walk-forward backtest only
  python main.py --train       # retrain HMM only
"""
import argparse
import time
import sys
from datetime import datetime

from config.settings import (
    WATCHLIST, SIGNAL_INTERVAL_MINUTES, TRADING_MODE
)
from core.market_data import fetch_historical, latest_quote, is_market_open
from core.feature_engineering import build_hmm_features, swing_signal, add_indicators
from core.position_tracker import BotState, Position
from regime.hmm_engine import RegimeDetector
from regime.strategies import get_regime_watchlist, compute_position_size, can_open_new_position
from risk.risk_manager import RiskManager
from executor.order_executor import login, get_portfolio_value, get_cash, buy_fractional, sell_all
from monitoring.logger import get_logger

logger = get_logger("main")


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
    features = build_hmm_features(df)
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

        df = fetch_historical(symbol, days=60)
        if df.empty or len(df) < 30:
            continue

        signal = swing_signal(df)
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
                entry_date=datetime.utcnow().date().isoformat(),
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


def main_loop():
    logger.info(f"🚀 AI Swing Trader starting (mode={TRADING_MODE})")

    if TRADING_MODE == "live":
        login()

    state = BotState.load()
    detector = RegimeDetector.load()

    # Initial train if no saved model
    if detector.model is None:
        detector = train_phase(detector)

    # Retrain weekly (every 7 * 24 * 60 / SIGNAL_INTERVAL minutes ≈ 168 cycles)
    cycles = 0
    retrain_every = max(1, int(7 * 24 * 60 / SIGNAL_INTERVAL_MINUTES))

    while True:
        if RiskManager.check_lockout():
            logger.critical("Lockout file present. Sleeping 60s and re-checking.")
            time.sleep(60)
            continue

        if not is_market_open():
            logger.info("Market closed. Sleeping 5 minutes.")
            time.sleep(300)
            continue

        try:
            portfolio_value = get_portfolio_value() if TRADING_MODE == "live" else state.peak_equity or 10000.0
            regime = monitor_phase(detector, state, portfolio_value)
            execute_phase(detector, state, regime, portfolio_value)

            cycles += 1
            if cycles % retrain_every == 0:
                detector = train_phase(detector)

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Saving state.")
            state.save()
            break
        except Exception as e:
            logger.error(f"Unhandled error in main loop: {e}", exc_info=True)

        logger.info(f"Sleeping {SIGNAL_INTERVAL_MINUTES} minutes until next cycle...")
        time.sleep(SIGNAL_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Swing Trader")
    parser.add_argument("--backtest", action="store_true", help="Run walk-forward backtest")
    parser.add_argument("--train", action="store_true", help="Retrain HMM only")
    parser.add_argument("--symbol", default="AAPL", help="Symbol for backtest")
    args = parser.parse_args()

    if args.backtest:
        from backtester.walk_forward import run_walk_forward
        run_walk_forward(args.symbol)
    elif args.train:
        d = RegimeDetector.load()
        train_phase(d)
    else:
        main_loop()
