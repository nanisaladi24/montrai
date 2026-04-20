import pandas as pd
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


def fetch_macro_features(days: int = 504) -> pd.DataFrame:
    """
    Fetch macro/cross-asset features aligned to SPY trading days.
    Returns a DataFrame indexed by date with columns:
      vix, vix3m, vix_term_ratio, tlt_ret, dxy_ret
    Falls back gracefully — missing series are filled with 0.
    """
    symbols = {
        "vix":  "^VIX",
        "vix3m": "^VIX3M",
        "tlt":  "TLT",
        "dxy":  "DX-Y.NYB",
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

    if "tlt" in result.columns:
        result["tlt_ret"] = result["tlt"].pct_change()
    else:
        result["tlt_ret"] = 0.0

    if "dxy" in result.columns:
        result["dxy_ret"] = result["dxy"].pct_change()
    else:
        result["dxy_ret"] = 0.0

    keep = ["vix", "vix3m", "vix_term_ratio", "tlt_ret", "dxy_ret"]
    for col in keep:
        if col not in result.columns:
            result[col] = 0.0

    return result[keep].tail(days)
