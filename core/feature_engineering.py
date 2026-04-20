import numpy as np
import pandas as pd
from monitoring.logger import get_logger

logger = get_logger("feature_eng")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI, MACD, Bollinger Bands, ATR, and volume ratio to OHLCV df."""
    df = df.copy()

    # Returns
    df["ret_1d"] = df["close"].pct_change()
    df["ret_5d"] = df["close"].pct_change(5)

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


def build_hmm_features(df: pd.DataFrame) -> np.ndarray:
    """Return feature matrix suitable for HMM training."""
    cols = ["ret_1d", "realised_vol", "atr_pct", "bb_position", "vol_ratio"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        df = add_indicators(df)
    features = df[cols].replace([np.inf, -np.inf], np.nan).dropna()
    return features.values


def swing_signal(df: pd.DataFrame) -> dict:
    """
    Return a dict with buy/sell signal strength for swing entry/exit.
    score > 0.6  → long candidate
    score < -0.6 → exit / short candidate
    """
    df = add_indicators(df)
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

    return {"score": round(score, 3), "reasons": reasons, "last": last.to_dict()}
