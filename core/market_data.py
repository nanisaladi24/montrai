import pandas as pd
import numpy as np
import yfinance as yf
import pytz
from datetime import datetime, timedelta
from typing import List, Optional
from monitoring.logger import get_logger

logger = get_logger("market_data")

_ET = pytz.timezone("America/New_York")

# NYSE session boundaries (ET)
_PRE_MARKET_OPEN  = (4,  0)
_REGULAR_OPEN     = (9, 30)
_REGULAR_CLOSE    = (16,  0)
_AFTER_HOURS_CLOSE = (20,  0)


def current_session() -> str:
    """
    Return the current NYSE trading session name:
      'pre_market'   04:00–09:30 ET  (Mon–Fri)
      'regular'      09:30–16:00 ET  (Mon–Fri)
      'after_hours'  16:00–20:00 ET  (Mon–Fri)
      'closed'       all other times
    """
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return "closed"

    t = (now.hour, now.minute)

    if _PRE_MARKET_OPEN <= t < _REGULAR_OPEN:
        return "pre_market"
    if _REGULAR_OPEN <= t < _REGULAR_CLOSE:
        return "regular"
    if _REGULAR_CLOSE <= t < _AFTER_HOURS_CLOSE:
        return "after_hours"
    return "closed"


def is_market_open() -> bool:
    """
    True when the bot should be running.
    Respects EXTENDED_HOURS_ENABLED from settings:
      - False (default): regular session only (09:30–16:00 ET)
      - True:            regular + pre-market + after-hours
    """
    import config.settings as cfg
    session = current_session()
    if session == "closed":
        return False
    if session == "regular":
        return True
    # extended sessions only allowed when the feature flag is on
    return cfg.EXTENDED_HOURS_ENABLED and session in ("pre_market", "after_hours")


def fetch_historical(symbol: str, days: int = 504, interval: str = "1d") -> pd.DataFrame:
    """Fetch OHLCV history from Yahoo Finance."""
    end = datetime.today()
    start = end - timedelta(days=days + 30)
    try:
        df = yf.download(symbol, start=start, end=end, interval=interval, auto_adjust=True, progress=False)
        if df.empty:
            logger.warning(f"No data returned for {symbol}")
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.columns = ["open", "high", "low", "close", "volume"]
        return df.tail(days)
    except Exception as e:
        logger.error(f"fetch_historical({symbol}): {e}")
        return pd.DataFrame()


def fetch_multi(symbols: List[str], days: int = 504) -> dict[str, pd.DataFrame]:
    return {sym: fetch_historical(sym, days) for sym in symbols}


def fetch_market_data_bulk(symbols: List[str], days: int = 252) -> pd.DataFrame:
    closes = {}
    for sym in symbols:
        df = fetch_historical(sym, days)
        if not df.empty:
            closes[sym] = df["close"]
    if not closes:
        return pd.DataFrame()
    return pd.DataFrame(closes).dropna()


def latest_quote(symbol: str) -> Optional[float]:
    try:
        ticker = yf.Ticker(symbol)
        return float(ticker.fast_info.last_price)
    except Exception as e:
        logger.warning(f"latest_quote({symbol}): {e}")
        return None


def fetch_fred_features(api_key: str, days: int = 504) -> pd.DataFrame:
    """
    Fetch macro features from FRED (Federal Reserve Economic Data).
    Free API key: https://fred.stlouisfed.org/docs/api/api_key.html
    Returns: yield_curve_spread (10Y-2Y), fed_funds_rate, hy_credit_spread
    """
    series = {
        "T10Y2Y":  "yield_curve_spread",  # 10Y–2Y treasury spread (recession signal)
        "FEDFUNDS": "fed_funds_rate",      # Effective federal funds rate
        "BAMLH0A0HYM2": "hy_credit_spread", # High-yield OAS credit spread
    }
    end = datetime.today()
    start = end - timedelta(days=days + 60)
    raw = {}
    for fred_id, col_name in series.items():
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={fred_id}&api_key={api_key}&file_type=json"
            f"&observation_start={start.strftime('%Y-%m-%d')}"
            f"&observation_end={end.strftime('%Y-%m-%d')}"
        )
        try:
            import urllib.request, json as _json
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = _json.loads(resp.read())
            obs = data.get("observations", [])
            s = pd.Series(
                {o["date"]: float(o["value"]) for o in obs if o["value"] != "."},
                dtype=float,
            )
            s.index = pd.to_datetime(s.index)
            raw[col_name] = s
        except Exception as e:
            logger.warning(f"fetch_fred_features: could not fetch {fred_id}: {e}")

    if not raw:
        return pd.DataFrame()

    df = pd.DataFrame(raw).ffill().tail(days)
    return df


def fetch_macro_features(days: int = 504) -> pd.DataFrame:
    """
    Fetch macro/cross-asset features aligned to SPY trading days.
    Returns a DataFrame indexed by date with columns:
      vix, vix3m, vix_term_ratio, tlt_ret, dxy_ret
    Falls back gracefully — missing series are filled with 0.
    """
    symbols = {
        "vix":   "^VIX",
        "vix3m": "^VIX3M",
        "vvix":  "^VVIX",   # vol-of-vol: early warning before VIX spikes
        "tlt":   "TLT",
        "dxy":   "DX-Y.NYB",
        "hyg":   "HYG",     # high-yield credit: risk-on/off confirmation
        "smh":   "SMH",     # semiconductor ETF: sector leadership proxy
        "spy":   "SPY",     # needed for SMH/SPY relative strength
    }
    end = datetime.today()
    start = end - timedelta(days=days + 60)
    raw = {}
    for key, ticker in symbols.items():
        try:
            df = yf.download(ticker, start=start, end=end, interval="1d",
                             auto_adjust=True, progress=False)
            if not df.empty:
                close_col = "Close" if "Close" in df.columns else df.columns[0]
                raw[key] = df[close_col].squeeze()
        except Exception as e:
            logger.warning(f"fetch_macro_features: could not fetch {ticker}: {e}")

    result = pd.DataFrame(raw)

    if "vix" in result.columns and "vix3m" in result.columns:
        result["vix_term_ratio"] = result["vix"] / result["vix3m"].replace(0, float("nan"))
    else:
        result["vix_term_ratio"] = float("nan")

    # VVIX/VIX ratio: rising VVIX before VIX moves = early vol expansion warning
    if "vvix" in result.columns and "vix" in result.columns:
        result["vvix_vix_ratio"] = result["vvix"] / result["vix"].replace(0, float("nan"))
    else:
        result["vvix_vix_ratio"] = float("nan")

    if "tlt" in result.columns:
        result["tlt_ret"] = result["tlt"].pct_change()
    else:
        result["tlt_ret"] = 0.0

    if "dxy" in result.columns:
        result["dxy_ret"] = result["dxy"].pct_change()
    else:
        result["dxy_ret"] = 0.0

    if "hyg" in result.columns:
        result["hyg_ret"] = result["hyg"].pct_change()
    else:
        result["hyg_ret"] = 0.0

    # SMH relative strength vs SPY: positive = semis leading = risk-on tech regime
    if "smh" in result.columns and "spy" in result.columns:
        result["smh_spy_rs"] = result["smh"].pct_change() - result["spy"].pct_change()
    else:
        result["smh_spy_rs"] = 0.0

    keep = ["vix", "vix3m", "vix_term_ratio", "vvix", "vvix_vix_ratio",
            "tlt_ret", "dxy_ret", "hyg_ret", "smh_spy_rs"]
    for col in keep:
        if col not in result.columns:
            result[col] = 0.0

    return result[keep].tail(days)


def _bs_gamma(S: float, K: float, T: float, sigma: float, r: float = 0.04) -> float:
    """Black-Scholes gamma for a single option."""
    if T <= 0 or sigma <= 0:
        return 0.0
    from math import log, sqrt, exp, pi
    d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
    return exp(-0.5 * d1 ** 2) / (sqrt(2 * pi) * S * sigma * sqrt(T))


def fetch_gex(symbol: str = "SPY", max_expiries: int = 4) -> dict:
    """
    Calculate net Gamma Exposure (GEX) from the options chain.
    Returns:
      gex_total    — net GEX in $billions (positive = dealers long gamma)
      gex_per_spot — GEX normalised by spot price (scale-invariant)
      gamma_flip   — strike closest to zero net gamma (price tends to gravitate here)
    """
    try:
        ticker = yf.Ticker(symbol)
        spot = ticker.fast_info.last_price
        if not spot:
            return {}

        expiries = ticker.options[:max_expiries]
        today = datetime.today().date()

        total_call_gex = 0.0
        total_put_gex = 0.0
        strike_gex: dict[float, float] = {}

        for exp_str in expiries:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            T = max((exp_date - today).days / 365.0, 1 / 365)

            chain = ticker.option_chain(exp_str)

            for _, row in chain.calls.iterrows():
                iv = row.get("impliedVolatility", 0)
                oi = row.get("openInterest", 0) or 0
                K = row["strike"]
                if iv <= 0 or oi <= 0:
                    continue
                g = _bs_gamma(spot, K, T, iv)
                # Dealers short calls → negative delta → long gamma on calls
                gex = g * oi * 100 * spot ** 2 / 1e9
                total_call_gex += gex
                strike_gex[K] = strike_gex.get(K, 0) + gex

            for _, row in chain.puts.iterrows():
                iv = row.get("impliedVolatility", 0)
                oi = row.get("openInterest", 0) or 0
                K = row["strike"]
                if iv <= 0 or oi <= 0:
                    continue
                g = _bs_gamma(spot, K, T, iv)
                # Dealers long puts → negative gamma on puts
                gex = g * oi * 100 * spot ** 2 / 1e9
                total_put_gex += gex
                strike_gex[K] = strike_gex.get(K, 0) - gex

        net_gex = total_call_gex - total_put_gex

        # Gamma flip: strike where cumulative GEX crosses zero
        sorted_strikes = sorted(strike_gex.keys())
        cumulative = 0.0
        gamma_flip = spot
        for k in sorted_strikes:
            prev = cumulative
            cumulative += strike_gex[k]
            if prev * cumulative < 0:  # sign change
                gamma_flip = k
                break

        return {
            "gex_total": round(net_gex, 4),
            "gex_per_spot": round(net_gex / spot, 6),
            "gamma_flip": round(gamma_flip, 2),
            "gamma_flip_distance_pct": round((gamma_flip - spot) / spot, 4),
            "spot": spot,
        }
    except Exception as e:
        logger.warning(f"fetch_gex({symbol}): {e}")
        return {}
