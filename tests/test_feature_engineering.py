import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest
from core.feature_engineering import add_indicators, swing_signal, build_hmm_features


def make_df(n=60) -> pd.DataFrame:
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(n) * 0.5)
    return pd.DataFrame({
        "open": prices * 0.999,
        "high": prices * 1.005,
        "low": prices * 0.995,
        "close": prices,
        "volume": np.random.randint(1_000_000, 5_000_000, n).astype(float),
    })


def test_add_indicators_columns():
    df = add_indicators(make_df())
    for col in ["rsi_14", "macd", "bb_position", "atr_14", "realised_vol"]:
        assert col in df.columns, f"Missing column: {col}"


def test_no_nan_after_indicators():
    df = add_indicators(make_df())
    assert not df[["rsi_14", "macd", "bb_position", "atr_pct"]].isnull().any().any()


def test_swing_signal_returns_score():
    result = swing_signal(make_df())
    assert "score" in result
    assert isinstance(result["score"], float)
    assert -1.1 <= result["score"] <= 1.1


def test_hmm_features_shape():
    df = add_indicators(make_df())
    features = build_hmm_features(df)
    assert features.ndim == 2
    assert features.shape[1] == 5
