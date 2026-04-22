"""
Walk-Forward Backtester — no look-ahead bias.
Trains on a rolling window then tests on the next out-of-sample window.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import List, Dict
from config.settings import (
    BACKTEST_TRAIN_DAYS, BACKTEST_TEST_DAYS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, REGIME_ALLOCATION,
    LOG_DIR,
)
from core.market_data import fetch_historical
from core.feature_engineering import add_indicators, build_hmm_features, swing_signal
from regime.hmm_engine import RegimeDetector
from monitoring.logger import get_logger

logger = get_logger("backtester")


def run_walk_forward(symbol: str, total_days: int = 756) -> pd.DataFrame:
    """
    Run walk-forward backtest for a single symbol.
    Returns a DataFrame of fold-level performance metrics.
    Results are also saved to logs/backtest_{symbol}_{date}.csv.
    """
    logger.info(f"Walk-forward backtest: {symbol}, {total_days} days")
    df = fetch_historical(symbol, days=total_days)
    if df.empty or len(df) < BACKTEST_TRAIN_DAYS + BACKTEST_TEST_DAYS:
        logger.error(f"Not enough data for {symbol}")
        return pd.DataFrame()

    df = add_indicators(df)
    results = []
    all_trades = []
    start = 0

    while start + BACKTEST_TRAIN_DAYS + BACKTEST_TEST_DAYS <= len(df):
        train_df = df.iloc[start : start + BACKTEST_TRAIN_DAYS]
        test_df  = df.iloc[start + BACKTEST_TRAIN_DAYS : start + BACKTEST_TRAIN_DAYS + BACKTEST_TEST_DAYS]

        detector = RegimeDetector()
        train_features = build_hmm_features(train_df)
        if len(train_features) < 30:
            start += BACKTEST_TEST_DAYS
            continue
        try:
            detector.model = None
            detector._train_model_on_features(train_features)
        except Exception as e:
            logger.warning(f"HMM train failed at fold {start}: {e}")
            start += BACKTEST_TEST_DAYS
            continue

        fold_trades = _simulate_trades(test_df, detector, symbol)
        if fold_trades:
            fold_df = pd.DataFrame(fold_trades)
            fold_pnl = fold_df["pnl_pct"].sum()
            win_rate = (fold_df["pnl_pct"] > 0).mean()
            results.append({
                "symbol": symbol,
                "fold_start": train_df.index[0].date(),
                "fold_test_start": test_df.index[0].date(),
                "n_trades": len(fold_trades),
                "total_return_pct": round(fold_pnl * 100, 2),
                "win_rate": round(win_rate, 3),
                "avg_return_pct": round(fold_df["pnl_pct"].mean() * 100, 2),
                "max_loss": round(fold_df["pnl_pct"].min() * 100, 2),
                "max_gain": round(fold_df["pnl_pct"].max() * 100, 2),
            })
            all_trades.extend(fold_trades)

        start += BACKTEST_TEST_DAYS

    result_df = pd.DataFrame(results)
    if not result_df.empty:
        logger.info(f"\n{result_df.to_string()}")
        logger.info(f"Mean return per fold: {result_df['total_return_pct'].mean():.2f}%")
        logger.info(f"Mean win rate: {result_df['win_rate'].mean():.2%}")
        _save_backtest_results(symbol, result_df, pd.DataFrame(all_trades))

    return result_df


def _save_backtest_results(symbol: str, folds: pd.DataFrame, trades: pd.DataFrame):
    """Persist fold summary and individual trades to CSV under logs/."""
    Path(LOG_DIR).mkdir(exist_ok=True)
    date_str = datetime.today().strftime("%Y%m%d")
    folds_path = Path(LOG_DIR) / f"backtest_{symbol}_{date_str}.csv"
    trades_path = Path(LOG_DIR) / f"backtest_trades_{symbol}_{date_str}.csv"
    folds.to_csv(folds_path, index=False)
    if not trades.empty:
        trades.to_csv(trades_path, index=False)
    logger.info(f"Backtest saved → {folds_path}  |  trades → {trades_path}")


def _simulate_trades(df: pd.DataFrame, detector: RegimeDetector, symbol: str = "") -> List[Dict]:
    trades = []
    i = 1
    while i < len(df) - 1:
        slice_df = df.iloc[: i + 1]
        signal = swing_signal(slice_df)
        features = build_hmm_features(slice_df)
        if len(features) == 0:
            i += 1
            continue
        regime = detector.predict_regime(features)
        alloc = REGIME_ALLOCATION.get(regime, 0.5)

        entry_price = float(df.iloc[i]["close"])

        if signal["score"] >= 0.6 and alloc > 0:
            stop = entry_price * (1 - STOP_LOSS_PCT)
            target = entry_price * (1 + TAKE_PROFIT_PCT)
            exit_price, exit_reason = None, None

            for j in range(i + 1, min(i + 15, len(df))):
                row = df.iloc[j]
                if row["low"] <= stop:
                    exit_price, exit_reason = stop, "stop_loss"
                    i = j
                    break
                if row["high"] >= target:
                    exit_price, exit_reason = target, "take_profit"
                    i = j
                    break
            else:
                exit_price = float(df.iloc[min(i + 14, len(df) - 1)]["close"])
                exit_reason = "time_exit"
                i += 14

            pnl_pct = (exit_price - entry_price) / entry_price * alloc
            trades.append({
                "symbol": symbol,
                "entry_date": df.index[i].date() if hasattr(df.index[i], "date") else str(df.index[i])[:10],
                "entry": round(entry_price, 4),
                "exit": round(exit_price, 4),
                "pnl_pct": round(pnl_pct * 100, 3),
                "reason": exit_reason,
                "regime": regime,
                "signal_score": signal["score"],
            })
        else:
            i += 1

    return trades
