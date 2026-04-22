"""
Options chain + GEX via Alpaca's options market-data API.

Alpaca returns contract snapshots with Greeks, IV, OI, bid/ask — so we get
real broker Greeks instead of computing them locally. If Alpaca isn't
configured or the call fails, callers fall back to yfinance in market_data.

Endpoints used (alpaca-py SDK):
  OptionHistoricalDataClient.get_option_chain(req)   — full chain snapshots
  TradingClient.get_option_contracts(req)            — tradable contract list
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Sequence

from monitoring.logger import get_logger

logger = get_logger("options_data")


@dataclass
class OptionContract:
    symbol: str             # OCC: SPY250620C00450000
    underlying: str
    expiry: date
    strike: float
    side: str               # "call" | "put"
    bid: float
    ask: float
    last: float
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    open_interest: int
    volume: int

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last or self.ask or self.bid

    @property
    def dte(self) -> int:
        return (self.expiry - date.today()).days


def _clients():
    """Build lazy Alpaca options clients. Raises if keys aren't set."""
    import config.settings as cfg
    if not cfg.ALPACA_API_KEY or not cfg.ALPACA_SECRET_KEY:
        raise RuntimeError("Alpaca API keys not configured")
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.trading.client import TradingClient
    return (
        OptionHistoricalDataClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY),
        TradingClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.ALPACA_PAPER),
    )


def get_option_chain(
    underlying: str,
    dte_min: int = 30,
    dte_max: int = 45,
    sides: Sequence[str] = ("call", "put"),
) -> list[OptionContract]:
    """Return tradable contracts in the DTE window with Greeks and quotes."""
    try:
        data_client, trading_client = _clients()
    except Exception as e:
        logger.warning(f"get_option_chain({underlying}): Alpaca not configured — {e}")
        return []

    today = date.today()
    exp_gte = today + timedelta(days=dte_min)
    exp_lte = today + timedelta(days=dte_max)

    try:
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import AssetStatus, ContractType
    except ImportError:
        logger.error("alpaca-py lacks options support — upgrade the package")
        return []

    contracts: list[OptionContract] = []
    for side in sides:
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                status=AssetStatus.ACTIVE,
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                type=ContractType.CALL if side == "call" else ContractType.PUT,
                limit=1000,
            )
            resp = trading_client.get_option_contracts(req)
        except Exception as e:
            logger.warning(f"get_option_contracts({underlying}, {side}): {e}")
            continue

        listing = getattr(resp, "option_contracts", None) or getattr(resp, "results", None) or []
        if not listing:
            continue

        # Snapshot batch — gets Greeks, IV, and quote for each contract symbol.
        symbols = [c.symbol for c in listing if getattr(c, "symbol", None)]
        snapshots = {}
        try:
            from alpaca.data.requests import OptionSnapshotRequest
            for i in range(0, len(symbols), 100):  # 100-per-request batch cap
                batch = symbols[i:i + 100]
                snap_req = OptionSnapshotRequest(symbol_or_symbols=batch)
                snapshots.update(data_client.get_option_snapshot(snap_req))
        except Exception as e:
            logger.warning(f"option snapshot fetch for {underlying}: {e}")

        for c in listing:
            sym = getattr(c, "symbol", "")
            snap = snapshots.get(sym)
            if not snap:
                continue
            greeks = getattr(snap, "greeks", None)
            quote = getattr(snap, "latest_quote", None)
            trade = getattr(snap, "latest_trade", None)
            bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
            ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
            last = float(getattr(trade, "price", 0) or 0) if trade else 0.0
            iv = float(getattr(snap, "implied_volatility", 0) or 0)
            delta = float(getattr(greeks, "delta", 0) or 0) if greeks else 0.0
            gamma = float(getattr(greeks, "gamma", 0) or 0) if greeks else 0.0
            theta = float(getattr(greeks, "theta", 0) or 0) if greeks else 0.0
            vega = float(getattr(greeks, "vega", 0) or 0) if greeks else 0.0
            exp = getattr(c, "expiration_date", None)
            if isinstance(exp, str):
                exp = datetime.strptime(exp, "%Y-%m-%d").date()
            contracts.append(OptionContract(
                symbol=sym,
                underlying=underlying,
                expiry=exp,
                strike=float(c.strike_price),
                side=side,
                bid=bid, ask=ask, last=last,
                iv=iv, delta=delta, gamma=gamma, theta=theta, vega=vega,
                open_interest=int(getattr(c, "open_interest", 0) or 0),
                volume=int(getattr(snap, "volume", 0) or 0),
            ))

    return contracts


def pick_contract(
    chain: list[OptionContract],
    target_delta: float = 0.40,
    side: str = "call",
) -> Optional[OptionContract]:
    """Pick the contract closest to the target absolute delta on the given side,
    with a liquidity filter to avoid thin strikes."""
    candidates = [c for c in chain if c.side == side and c.bid > 0 and c.ask > 0
                  and c.open_interest >= 50]
    if not candidates:
        candidates = [c for c in chain if c.side == side and (c.bid + c.ask) > 0]
    if not candidates:
        return None
    target = target_delta if side == "call" else -target_delta
    return min(candidates, key=lambda c: abs(c.delta - target))


def pick_vertical_spread(
    chain: list[OptionContract],
    side: str,                      # "call" | "put"
    short_delta: float = 0.30,
    wing_width: float = 5.0,
    direction: str = "credit",      # "credit" | "debit"
) -> Optional[tuple[OptionContract, OptionContract]]:
    """Return (short_leg, long_leg) for a vertical spread.

    Credit: short_leg is nearer-the-money (higher premium), long_leg is further OTM.
    Debit: long_leg is nearer-the-money, short_leg is further OTM (we sell the cheap OTM).

    Returns None if a matched strike at `wing_width` doesn't exist in the chain
    or liquidity is too thin.
    """
    same_side = [c for c in chain if c.side == side and c.bid > 0 and c.ask > 0
                 and c.open_interest >= 20]
    if not same_side:
        return None
    # All legs in the same expiry — pick the most-populated expiry to avoid
    # cross-expiry spread picks.
    from collections import Counter
    exp_counts = Counter(c.expiry for c in same_side)
    target_exp = exp_counts.most_common(1)[0][0]
    same_side = [c for c in same_side if c.expiry == target_exp]

    # Pick primary leg: credit → short at target_delta; debit → long at higher delta
    if direction == "credit":
        primary_target_delta = short_delta if side == "call" else -short_delta
        primary = min(same_side, key=lambda c: abs(c.delta - primary_target_delta))
    else:
        # Debit: long leg is more ITM (~40-50Δ); short leg is far OTM wing protection
        primary_target_delta = (short_delta + 0.20) if side == "call" else -(short_delta + 0.20)
        primary = min(same_side, key=lambda c: abs(c.delta - primary_target_delta))

    # Wing leg: for a call spread, the hedge strike is further above (credit) / below (debit)
    # For a put spread, mirror. Direction of wing:
    if side == "call":
        wing_strike_target = primary.strike + wing_width if direction == "credit" else primary.strike - wing_width
    else:  # put
        wing_strike_target = primary.strike - wing_width if direction == "credit" else primary.strike + wing_width

    wing_candidates = [c for c in same_side if c.strike != primary.strike]
    if not wing_candidates:
        return None
    wing = min(wing_candidates, key=lambda c: abs(c.strike - wing_strike_target))

    # Assign short/long based on direction
    if direction == "credit":
        return (primary, wing)  # short, long
    else:
        return (wing, primary)  # short (OTM hedge), long (ITM leg we bought)


def pick_iron_condor(
    chain: list[OptionContract],
    spot: float,
    short_delta: float = 0.15,
    wing_width: float = 5.0,
) -> Optional[tuple[OptionContract, OptionContract, OptionContract, OptionContract]]:
    """Return (put_short, put_long, call_short, call_long) for a balanced iron condor.

    Short strikes sit at ~short_delta OTM (lower-risk wings). Long strikes
    protect by wing_width further OTM. All four legs share expiry.
    """
    put_pair = pick_vertical_spread(chain, "put", short_delta, wing_width, direction="credit")
    call_pair = pick_vertical_spread(chain, "call", short_delta, wing_width, direction="credit")
    if not put_pair or not call_pair:
        return None
    put_short, put_long = put_pair
    call_short, call_long = call_pair
    # Sanity — shorts must straddle spot
    if put_short.strike >= spot or call_short.strike <= spot:
        return None
    return (put_short, put_long, call_short, call_long)


def net_premium(legs: list[tuple[OptionContract, str]]) -> float:
    """Compute per-unit net premium in dollars for a list of (contract, side) tuples.

    side = "long" means we buy (pay mid) → adds to debit.
    side = "short" means we sell (receive mid) → subtracts from debit.

    Positive return = net debit (we pay). Negative = net credit (we receive).
    """
    net = 0.0
    for contract, side in legs:
        mid = contract.mid
        if mid <= 0:
            return 0.0
        if side == "long":
            net += mid
        else:  # short
            net -= mid
    return net


def compute_gex_from_chain(chain: list[OptionContract], spot: float) -> dict:
    """Net GEX from a pre-fetched chain. Uses broker-quoted gamma rather than BS."""
    if not chain or spot <= 0:
        return {}
    call_gex = 0.0
    put_gex = 0.0
    strike_gex: dict[float, float] = {}
    for c in chain:
        if c.open_interest <= 0 or c.gamma <= 0:
            continue
        gex = c.gamma * c.open_interest * 100 * (spot ** 2) / 1e9
        if c.side == "call":
            call_gex += gex
            strike_gex[c.strike] = strike_gex.get(c.strike, 0) + gex
        else:
            put_gex += gex
            strike_gex[c.strike] = strike_gex.get(c.strike, 0) - gex

    net_gex = call_gex - put_gex
    sorted_strikes = sorted(strike_gex.keys())
    cumulative = 0.0
    gamma_flip = spot
    for k in sorted_strikes:
        prev = cumulative
        cumulative += strike_gex[k]
        if prev * cumulative < 0:
            gamma_flip = k
            break
    return {
        "gex_total": round(net_gex, 4),
        "gex_per_spot": round(net_gex / spot, 6),
        "gamma_flip": round(gamma_flip, 2),
        "gamma_flip_distance_pct": round((gamma_flip - spot) / spot, 4),
        "spot": spot,
    }


def fetch_gex_alpaca(underlying: str = "SPY") -> dict:
    """GEX from Alpaca options chain across the nearest four expiries."""
    try:
        chain = get_option_chain(underlying, dte_min=0, dte_max=60)
        if not chain:
            return {}
        from core.market_data import latest_quote
        spot = latest_quote(underlying) or 0.0
        return compute_gex_from_chain(chain, spot)
    except Exception as e:
        logger.warning(f"fetch_gex_alpaca({underlying}): {e}")
        return {}
