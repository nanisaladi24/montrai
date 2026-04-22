"""
Readers for locally cached Polygon flat files (data/polygon_s3/).

Pair this with scripts/polygon_s3_sync.py, which downloads the gzipped CSVs.
Everything here is pandas-first and avoids hitting the network — keep the
ingest and the reader paths decoupled.
"""
from __future__ import annotations
import gzip
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


_CACHE = Path(__file__).resolve().parent.parent / "data" / "polygon_s3"

_DATASETS = {
    "stocks":  "us_stocks_sip/day_aggs_v1",
    "options": "us_options_opra/day_aggs_v1",
    "indices": "us_indices/day_aggs_v1",
    "crypto":  "global_crypto/day_aggs_v1",
    "forex":   "global_forex/day_aggs_v1",
}


def _path(dataset: str, d: date) -> Path:
    prefix = _DATASETS[dataset]
    return _CACHE / prefix / f"{d.year}" / f"{d.month:02d}" / f"{d.isoformat()}.csv.gz"


def _read_day(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f)
    if "window_start" in df.columns:
        df["ts"] = pd.to_datetime(df["window_start"], unit="ns", utc=True)
    return df


def _iter_trading_days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def load_daily_bars(
    dataset: str,
    ticker: Optional[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Return per-date rows for a single ticker (or all tickers if None)."""
    frames = []
    for d in _iter_trading_days(start, end):
        df = _read_day(_path(dataset, d))
        if df.empty:
            continue
        if ticker:
            df = df[df["ticker"] == ticker]
        if not df.empty:
            df = df.copy()
            df["date"] = d
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_index_series(ticker: str, start: date, end: date) -> pd.Series:
    """Daily close series for a Polygon index (e.g. 'I:VIX', 'I:VVIX')."""
    df = load_daily_bars("indices", ticker, start, end)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].rename(ticker.lower())


def load_stock_bars(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Daily OHLCV for a single stock/ETF."""
    df = load_daily_bars("stocks", ticker, start, end)
    if df.empty:
        return pd.DataFrame()
    keep = ["date", "open", "high", "low", "close", "volume"]
    return df[[c for c in keep if c in df.columns]].set_index("date").sort_index()


def load_options_for_underlying(
    underlying: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    """All option-contract daily bars whose OCC ticker starts with `O:{underlying}`.

    Useful for backtesting the long-call / long-put strategy selector and
    reconstructing historical GEX (OI-weighted gamma, locally computed).
    """
    prefix = f"O:{underlying}"
    frames = []
    for d in _iter_trading_days(start, end):
        df = _read_day(_path("options", d))
        if df.empty or "ticker" not in df.columns:
            continue
        df = df[df["ticker"].astype(str).str.startswith(prefix)]
        if df.empty:
            continue
        df = df.copy()
        df["date"] = d
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def cache_summary() -> pd.DataFrame:
    """Quick inventory of what's on disk — useful before kicking off a backtest."""
    rows = []
    for ds, prefix in _DATASETS.items():
        root = _CACHE / prefix
        files = list(root.glob("*/*/*.csv.gz")) if root.exists() else []
        if not files:
            rows.append({"dataset": ds, "files": 0, "start": None, "end": None, "mb": 0})
            continue
        dates = sorted(f.stem.replace(".csv", "") for f in files)
        total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
        rows.append({
            "dataset": ds,
            "files": len(files),
            "start": dates[0],
            "end": dates[-1],
            "mb": round(total_mb, 1),
        })
    return pd.DataFrame(rows)
