"""
Dynamic daily watchlist discovery.

Pulls top movers (gainers + losers + most-actives) from Alpaca's screener,
filters out penny stocks and non-optionable tickers, verifies options
liquidity at the ATM strike, and returns a ranked list that merges with
the static base watchlist.

Refresh cadence: once per trading day, before market open (~09:00 ET).
Results are cached in BotState so the 30-50 API calls per refresh only
happen once daily.

Alpaca ScreenerClient endpoints (primary source — available on current tier):
  - get_most_actives(top=N)     — top by volume
  - get_market_movers(top=N)    — gainers + losers (no volume field)

Polygon equivalents (`/v2/snapshot/locale/us/markets/stocks/gainers` + `/losers`
+ `/tickers`) are currently tier-gated on the user's plan and return
`NOT_AUTHORIZED`. If the plan upgrades, a second fetch path can be added to
`fetch_candidates()` to prefer Polygon data (it includes raw dollar volume).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

import config.settings as _cfg
import config.runtime_config as rc
from monitoring.logger import get_logger

logger = get_logger("dynamic_watchlist")


@dataclass
class Candidate:
    symbol: str
    price: float
    percent_change: float
    source: str          # "gainer" | "loser" | "active"
    volume: Optional[float] = None


def _screener_client():
    from alpaca.data.historical.screener import ScreenerClient
    return ScreenerClient(_cfg.ALPACA_API_KEY, _cfg.ALPACA_SECRET_KEY)


# Symbol-exclusion patterns: warrants, rights, preferred shares, units, SPACs
# and any ticker with a suffix segment (.WS, .U, etc.) — these almost never
# have liquid options on the retail side.
_BAD_SUFFIXES = (".WS", ".W", ".U", ".R", "-W", "-R")
_BAD_ENDS     = ("W", "R")          # AMPGR, IVDAW, USGOW → penny warrants
_BAD_PATTERNS = ("WS",)


def _is_optionable(symbol: str) -> bool:
    """Reject tickers that are obviously not optionable (warrants, rights, units).
    Not perfect — a definitive check requires an options chain query, which is
    expensive and handled separately in check_options_liquidity()."""
    s = symbol.upper()
    if any(s.endswith(suf) for suf in _BAD_SUFFIXES):
        return False
    # Heuristic: 5+ char tickers ending in W (warrants) or R (rights)
    if len(s) >= 5 and s[-1] in _BAD_ENDS:
        # Allow well-known tickers that happen to end in these letters
        allowlist = {"AMZR", "ANIPR", "LEW", "NEXR", "NVTR", "SLAB"}
        if s not in allowlist:
            return False
    return True


def fetch_candidates(
    gainers_top: int = 20,
    losers_top: int = 20,
    actives_top: int = 20,
) -> list[Candidate]:
    """Return combined gainers + losers + most-actives from Alpaca.

    The counts here are *pre-filter* — real counts after price / optionable
    filters will be smaller.
    """
    from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

    candidates: list[Candidate] = []
    client = _screener_client()

    try:
        movers = client.get_market_movers(MarketMoversRequest(top=max(gainers_top, losers_top)))
        for m in (movers.gainers or [])[:gainers_top]:
            candidates.append(Candidate(
                symbol=m.symbol, price=float(m.price),
                percent_change=float(m.percent_change), source="gainer",
            ))
        for m in (movers.losers or [])[:losers_top]:
            candidates.append(Candidate(
                symbol=m.symbol, price=float(m.price),
                percent_change=float(m.percent_change), source="loser",
            ))
    except Exception as e:
        logger.warning(f"fetch_candidates: movers failed: {e}")

    try:
        actives = client.get_most_actives(MostActivesRequest(top=actives_top))
        for a in (actives.most_actives or [])[:actives_top]:
            candidates.append(Candidate(
                symbol=a.symbol, price=0.0,  # most_actives doesn't expose price
                percent_change=0.0, source="active",
                volume=float(getattr(a, "volume", 0) or 0),
            ))
    except Exception as e:
        logger.warning(f"fetch_candidates: most_actives failed: {e}")

    return candidates


def filter_tradeable(
    candidates: list[Candidate],
    min_price: float = 5.0,
    max_price: float = 2000.0,
) -> list[Candidate]:
    """Remove penny stocks, warrants/rights, and tickers already in the static base."""
    seen: dict[str, Candidate] = {}
    for c in candidates:
        if c.symbol in seen:
            # Prefer gainer/loser record over the most-actives one (has price info)
            if seen[c.symbol].source == "active" and c.source in ("gainer", "loser"):
                seen[c.symbol] = c
            continue
        if not _is_optionable(c.symbol):
            continue
        # For movers we have price directly; for active-only we'll validate via chain later
        if c.price > 0 and (c.price < min_price or c.price > max_price):
            continue
        seen[c.symbol] = c
    return list(seen.values())


def check_options_liquidity(symbol: str, min_oi: int = 500) -> bool:
    """Verify at least one ATM contract in 30-45 DTE has OI ≥ min_oi."""
    try:
        from core.options_data import get_option_chain
        chain = get_option_chain(symbol, dte_min=30, dte_max=45, sides=["call"])
    except Exception as e:
        logger.debug(f"chain fetch failed for {symbol}: {e}")
        return False
    if not chain:
        return False
    liquid = any(c.open_interest >= min_oi for c in chain)
    return liquid


def build_daily_watchlist(
    base_watchlist: list[str],
    limit: int = 20,
    min_price: float = 5.0,
    min_oi: int = 500,
) -> list[dict]:
    """Discover up to `limit` liquid names to trade today.

    Returns a list of dicts with keys {symbol, source, price, percent_change}.
    Excludes symbols already in base_watchlist so we don't duplicate.
    """
    raw = fetch_candidates(gainers_top=20, losers_top=20, actives_top=20)
    tradeable = filter_tradeable(raw, min_price=min_price)
    # Skip anything already on the base
    base_upper = {s.upper() for s in base_watchlist}
    tradeable = [c for c in tradeable if c.symbol.upper() not in base_upper]
    # Rank: % change magnitude (gainers + losers surface movement); actives
    # come in with percent_change=0 so they rank last unless we explicitly boost.
    tradeable.sort(key=lambda c: abs(c.percent_change), reverse=True)

    # Liquidity check is expensive — only run it on the top candidates
    results: list[dict] = []
    for cand in tradeable:
        if len(results) >= limit:
            break
        if not check_options_liquidity(cand.symbol, min_oi=min_oi):
            logger.debug(f"{cand.symbol}: no liquid ATM options (OI < {min_oi})")
            continue
        results.append({
            "symbol": cand.symbol,
            "source": cand.source,
            "price": cand.price,
            "percent_change": round(cand.percent_change, 2),
        })
    _hydrate_prices(results)
    return results


def _hydrate_prices(results: list[dict]) -> None:
    """Fill `price` for picks that came in without it (most_actives rows —
    the SDK response omits price). Uses latest_quote only; percent_change
    is left as-is to avoid piling daily-history fetches onto the already
    chain-heavy refresh path."""
    if not results:
        return
    try:
        from core.market_data import latest_quote
    except Exception as e:
        logger.debug(f"price hydrate import failed: {e}")
        return
    for r in results:
        if r.get("price"):
            continue
        try:
            quote = latest_quote(r["symbol"])
            if quote:
                r["price"] = round(float(quote), 2)
        except Exception as e:
            logger.debug(f"latest_quote({r['symbol']}): {e}")
