import numpy as np
import pandas as pd
from monitoring.logger import get_logger
from core.market_data import fetch_macro_features, fetch_fred_features, fetch_gex
from core.financial_datasets import fundamental_score
import config.runtime_config as _rc

logger = get_logger("feature_eng")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI, MACD, Bollinger Bands, ATR, and volume ratio to OHLCV df."""
    df = df.copy()

    # Returns (multi-timeframe)
    df["ret_1d"]  = df["close"].pct_change()
    df["ret_5d"]  = df["close"].pct_change(5)
    df["ret_20d"] = df["close"].pct_change(20)
    df["ret_60d"] = df["close"].pct_change(60)

    # RSI (14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD (12, 26, 9)
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands (20, 2σ)
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_lower"] = sma20 - 2 * std20
    df["bb_position"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-9)

    # ATR (14) — volatility proxy
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()
    df["atr_pct"] = df["atr_14"] / df["close"]

    # Volume ratio (today vs 20-day avg)
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    # Realised volatility (20-day)
    df["realised_vol"] = df["ret_1d"].rolling(20).std() * np.sqrt(252)

    return df.dropna()


# Canonical HMM feature schema. Every feature column is always present —
# data-source gaps (FRED outage, GEX chain unavailable) zero-fill instead of
# changing the column count. A stable schema means the pickled HMM keeps
# matching across cycles, so we don't force-retrain on transient API blips.
HMM_FEATURE_COLUMNS = [
    # SPY price/technical
    "ret_1d", "ret_5d", "ret_20d", "ret_60d",
    "realised_vol", "atr_pct", "bb_position", "vol_ratio",
    # Macro / cross-asset (yfinance indices + ETFs)
    "vix_rank", "vix_term_ratio",
    "vvix_rank", "vvix_vix_ratio",
    "tlt_ret", "dxy_ret", "hyg_ret", "smh_spy_rs",
    # FRED (fills to 0 when key unset or series drops out)
    "yield_curve_spread", "fed_funds_rate", "hy_credit_spread",
    # GEX (Alpaca options chain → Black-Scholes)
    "gex_per_spot", "gamma_flip_dist",
]
_BASE_COLS = HMM_FEATURE_COLUMNS[:8]


def build_hmm_features(df: pd.DataFrame) -> np.ndarray:
    """Return a stable-schema feature matrix for the HMM.

    Always emits `HMM_FEATURE_COLUMNS` in order; missing sources fill with 0.0
    so the column count never drifts between cycles.
    """
    if df.empty:
        return np.empty((0, len(HMM_FEATURE_COLUMNS)))

    if any(c not in df.columns for c in _BASE_COLS):
        df = add_indicators(df)
    if df.empty:
        return np.empty((0, len(HMM_FEATURE_COLUMNS)))

    # Align macro features to the same date index
    macro = fetch_macro_features(days=len(df) + 120)
    if not macro.empty:
        macro.index = pd.to_datetime(macro.index)
        if isinstance(macro.index, pd.MultiIndex):
            macro.index = macro.index.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if isinstance(df.index, pd.MultiIndex):
        df.index = df.index.get_level_values(0)

    merged = df[_BASE_COLS].join(macro, how="left") if not macro.empty else df[_BASE_COLS].copy()

    # Percentile-rank VIX and VVIX so scale is invariant across history
    for raw_col, rank_col in [("vix", "vix_rank"), ("vvix", "vvix_rank")]:
        if raw_col in merged.columns:
            merged[rank_col] = merged[raw_col].rank(pct=True)
        else:
            merged[rank_col] = 0.5

    # FRED — join if configured; canonical columns are added unconditionally below
    fred_key = _rc.load().get("data_sources", {}).get("fred_api_key", "")
    if fred_key:
        try:
            fred = fetch_fred_features(fred_key, days=len(df) + 120)
            if not fred.empty:
                fred.index = pd.to_datetime(fred.index)
                merged = merged.join(fred, how="left")
                active = [c for c in ["yield_curve_spread", "fed_funds_rate", "hy_credit_spread"]
                          if c in merged.columns]
                logger.info(f"FRED features active: {active}")
        except Exception as e:
            logger.warning(f"FRED feature fetch failed, skipping: {e}")

    # GEX — Alpaca options chain preferred, yfinance fallback. Both may return
    # nothing out of hours or on API hiccups; schema stays stable via fill.
    gex = fetch_gex("SPY")
    if gex:
        gex_per_spot = gex.get("gex_per_spot", 0.0)
        gamma_flip_dist = gex.get("gamma_flip_distance_pct", 0.0)
        if pd.isna(gex_per_spot):
            gex_per_spot = 0.0
        if pd.isna(gamma_flip_dist):
            gamma_flip_dist = 0.0
        merged["gex_per_spot"] = gex_per_spot
        merged["gamma_flip_dist"] = gamma_flip_dist
        gex_total = gex.get("gex_total", float("nan"))
        flip_pct = gex.get("gamma_flip_distance_pct", float("nan"))
        gex_total_str = f"{gex_total:+.2f}B" if not pd.isna(gex_total) else "n/a"
        flip_pct_str = f"{flip_pct:+.1%}" if not pd.isna(flip_pct) else "n/a"
        logger.info(f"GEX: {gex_total_str} | flip @ ${gex.get('gamma_flip', 'n/a')} "
                    f"({flip_pct_str} from spot)")

    # Reindex to the canonical column set — any missing column becomes 0.0.
    merged = merged.reindex(columns=HMM_FEATURE_COLUMNS)
    merged = merged.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)
    return merged.values


def swing_signal(df: pd.DataFrame, symbol: str = "") -> dict:
    """
    Return a dict with buy/sell signal strength for swing entry/exit.
    score > 0.6  → long candidate
    score < -0.6 → exit / short candidate

    When symbol is provided and FINANCIAL_DATASETS_API_KEY is set, a
    fundamental overlay (valuation, earnings quality, insider activity,
    analyst revisions) is blended in at 30% weight.
    """
    if df is None or df.empty:
        return {"score": 0.0, "reasons": [], "last": {}}
    df = add_indicators(df)
    # add_indicators drops NaN warm-up rows; with a tight input it can leave
    # too few bars for the lookback below (df.iloc[-2] on macd crossover).
    if df.empty or len(df) < 2:
        return {"score": 0.0, "reasons": [], "last": {}}
    last = df.iloc[-1]

    score = 0.0
    reasons = []

    # RSI momentum
    if last["rsi_14"] < 35:
        score += 0.3
        reasons.append("RSI oversold")
    elif last["rsi_14"] > 65:
        score -= 0.3
        reasons.append("RSI overbought")

    # MACD crossover
    if last["macd_hist"] > 0 and df.iloc[-2]["macd_hist"] < 0:
        score += 0.35
        reasons.append("MACD bullish cross")
    elif last["macd_hist"] < 0 and df.iloc[-2]["macd_hist"] > 0:
        score -= 0.35
        reasons.append("MACD bearish cross")

    # Bollinger mean-reversion
    if last["bb_position"] < 0.15:
        score += 0.25
        reasons.append("Near lower BB")
    elif last["bb_position"] > 0.85:
        score -= 0.25
        reasons.append("Near upper BB")

    # Volume confirmation
    if last["vol_ratio"] > 1.5 and score > 0:
        score += 0.1
        reasons.append("Volume spike confirms")

    # ── Fundamental overlay (financial-datasets.ai) ────────────────────────────
    fund_result: dict = {}
    if symbol and _rc.load().get("financial_datasets_enabled", True):
        try:
            fund_result = fundamental_score(symbol)
            fscore = fund_result.get("score", 0.0)
            if fscore != 0.0:
                # Blend: 70% technical + 30% fundamental
                score = score * 0.70 + fscore * 0.30
                det = fund_result.get("details", {})
                if det.get("valuation_score", 0) > 0:
                    reasons.append("Cheap valuation")
                elif det.get("valuation_score", 0) < -0.05:
                    reasons.append("Rich valuation")
                if det.get("quality_score", 0) > 0:
                    reasons.append("Strong fundamentals")
                if det.get("surprise_score", 0) > 0:
                    reasons.append("Earnings beat streak")
                elif det.get("surprise_score", 0) < 0:
                    reasons.append("Earnings miss streak")
                if det.get("insider_score", 0) > 0.03:
                    reasons.append("Insider buying")
                elif det.get("insider_score", 0) < -0.03:
                    reasons.append("Insider selling")
                if det.get("revision_score", 0) > 0.05:
                    reasons.append("EPS estimates rising")
        except Exception as e:
            logger.warning(f"fundamental_score({symbol}): {e}")

    result = {"score": round(score, 3), "reasons": reasons, "last": last.to_dict()}
    if fund_result:
        result["fundamentals"] = fund_result.get("details", {})
    return result
