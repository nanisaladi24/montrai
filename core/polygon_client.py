"""
Polygon (massive.com) REST client — indices, intraday bars, options history.

Scope:
  • Indices: VIX / VVIX / VIX3M as `I:VIX`, `I:VVIX`, `I:VIX3M` — replaces
    yfinance for the last data category it was needed for.
  • Stocks + ETFs daily/minute aggregates.
  • Options contract aggregates + chain snapshots. Greeks may be null on
    lower tiers; callers fall back to Alpaca chain for Greeks/IV.

This module deliberately avoids a heavy SDK — thin urllib wrapper, same
pattern as core/financial_datasets.py. Env var: POLYGON_API_KEY.
"""
from __future__ import annotations
import os
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import pandas as pd

from monitoring.logger import get_logger

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = get_logger("polygon")

_BASE = "https://api.polygon.io"


def _api_key() -> str:
    return os.getenv("POLYGON_API_KEY", "") or ""


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    key = _api_key()
    if not key:
        return None
    query = dict(params or {})
    query["apiKey"] = key
    url = f"{_BASE}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "montrai/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug(f"polygon GET {path}: {e}")
        return None


# ── Aggregate bars ─────────────────────────────────────────────────────────────

def fetch_aggregates(
    ticker: str,
    multiplier: int = 1,
    timespan: str = "day",
    days: int = 504,
    end_date: Optional[date] = None,
) -> pd.DataFrame:
    """Return OHLCV for any Polygon ticker (stocks, ETFs, indices via I:*)."""
    if end_date is None:
        end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days + 30)
    path = (
        f"/v2/aggs/ticker/{urllib.parse.quote(ticker)}/range/"
        f"{multiplier}/{timespan}/{start_date.isoformat()}/{end_date.isoformat()}"
    )
    data = _get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
    if not data or data.get("status") not in ("OK", "DELAYED"):
        return pd.DataFrame()
    rows = data.get("results") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("time").sort_index()
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    return df.tail(days) if timespan == "day" else df


def fetch_index_daily(index_ticker: str, days: int = 504) -> pd.Series:
    """Daily close for a Polygon index. `index_ticker` is the short form
    ('VIX', 'VVIX', 'VIX3M'); the I: prefix is added automatically."""
    poly_ticker = index_ticker if index_ticker.startswith("I:") else f"I:{index_ticker}"
    df = fetch_aggregates(poly_ticker, 1, "day", days)
    if df.empty:
        return pd.Series(dtype=float)
    return df["close"].rename(index_ticker.lower())


def fetch_minute_bars(ticker: str, days: int = 5) -> pd.DataFrame:
    """Minute aggregates for intraday execution. Returns UTC-indexed OHLCV."""
    return fetch_aggregates(ticker, 1, "minute", days)


# ── Latest quote ───────────────────────────────────────────────────────────────

def latest_quote(ticker: str) -> Optional[float]:
    data = _get(f"/v2/last/trade/{urllib.parse.quote(ticker)}", {})
    if not data:
        return None
    price = (data.get("results") or {}).get("p")
    return float(price) if price is not None else None


# ── Options ────────────────────────────────────────────────────────────────────

def options_chain_snapshot(
    underlying: str,
    dte_min: int = 30,
    dte_max: int = 45,
    side: Optional[str] = None,
) -> list[dict]:
    """Chain snapshot in the given DTE window. Greeks + IV may be null on
    lower tiers — callers should fall back to broker data if so."""
    today = datetime.now(timezone.utc).date()
    params = {
        "expiration_date.gte": (today + timedelta(days=dte_min)).isoformat(),
        "expiration_date.lte": (today + timedelta(days=dte_max)).isoformat(),
        "limit": 250,
    }
    if side:
        params["contract_type"] = side  # "call" or "put"
    results: list[dict] = []
    next_url = None
    for _ in range(10):  # at most 10 pages
        if next_url is None:
            data = _get(f"/v3/snapshot/options/{underlying}", params)
        else:
            # next_url already includes query string — append the API key
            req_url = f"{next_url}&apiKey={_api_key()}"
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(req_url, headers={"User-Agent": "montrai/0.1"}),
                    timeout=10,
                ) as resp:
                    data = json.loads(resp.read())
            except Exception:
                break
        if not data:
            break
        results.extend(data.get("results") or [])
        next_url = data.get("next_url")
        if not next_url:
            break
    return results


# ── Health check ───────────────────────────────────────────────────────────────

def is_configured() -> bool:
    return bool(_api_key())
