"""
Public executor facade — delegates to the configured broker.

  BROKER=alpaca     → AlpacaBroker (paper or live)
  BROKER=robinhood  → RobinhoodBroker (live only)

All callers import from here; never import broker implementations directly.
"""
from typing import Optional
from executor.base import BrokerBase
from monitoring.logger import get_logger

logger = get_logger("executor")
_broker: Optional[BrokerBase] = None


def get_broker() -> BrokerBase:
    global _broker
    if _broker is not None:
        return _broker

    import config.settings as cfg

    if cfg.BROKER == "alpaca":
        if not cfg.ALPACA_API_KEY or not cfg.ALPACA_SECRET_KEY:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        from executor.alpaca_broker import AlpacaBroker
        _broker = AlpacaBroker(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.ALPACA_PAPER)

    elif cfg.BROKER == "robinhood":
        if not cfg.RH_USERNAME or not cfg.RH_PASSWORD:
            raise RuntimeError("ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD not set in .env")
        from executor.robinhood_broker import RobinhoodBroker
        _broker = RobinhoodBroker(cfg.RH_USERNAME, cfg.RH_PASSWORD, cfg.RH_MFA_CODE)

    else:
        raise ValueError(f"Unknown BROKER='{cfg.BROKER}'. Use 'alpaca' or 'robinhood'.")

    return _broker


# ── Convenience wrappers (keep existing call-sites unchanged) ──────────────────

def login():
    """Eagerly initialize the broker connection."""
    get_broker()


def get_portfolio_value() -> float:
    return get_broker().get_portfolio_value()


def get_cash() -> float:
    return get_broker().get_cash()


def buy_fractional(symbol: str, dollars: float, regime_name: str = "") -> Optional[str]:
    return get_broker().buy_notional(symbol, dollars, regime_name)


def sell_all(symbol: str, current_price: float, reason: str = "", regime_name: str = "") -> Optional[str]:
    return get_broker().sell_position(symbol, current_price, reason, regime_name)


def cancel_order(order_id: str) -> bool:
    return get_broker().cancel_order(order_id)


def get_open_positions() -> dict[str, float]:
    return get_broker().get_open_positions()


# ── Options wrappers (no-op for brokers that don't support them) ──────────────

def supports_options() -> bool:
    try:
        return get_broker().supports_options()
    except Exception:
        return False


def buy_option(contract_symbol: str, qty: int, limit_price: float,
               regime_name: str = "",
               protective_tp_pct: float = 0.0,
               protective_sl_pct: float = 0.0) -> Optional[str]:
    return get_broker().buy_option(
        contract_symbol, qty, limit_price, regime_name,
        protective_tp_pct=protective_tp_pct,
        protective_sl_pct=protective_sl_pct,
    )


def last_protection_order_id() -> str:
    """Returns the OCO (or bare stop) order id created by the most recent
    buy_option call, or '' if none. Persist this on the tracked position
    so a manual exit can cancel the protection order first."""
    try:
        return str(getattr(get_broker(), "_last_stop_order_id", "") or "")
    except Exception:
        return ""


def cancel_order(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        return bool(get_broker().cancel_order(order_id))
    except Exception as e:
        logger.debug(f"cancel_order({order_id}): {e}")
        return False


def sell_option(contract_symbol: str, qty: int, limit_price: float,
                reason: str = "", regime_name: str = "") -> Optional[str]:
    return get_broker().sell_option(contract_symbol, qty, limit_price, reason, regime_name)


def get_option_positions() -> list[dict]:
    try:
        return get_broker().get_option_positions()
    except Exception as e:
        logger.error(f"get_option_positions: {e}")
        return []


def get_stock_positions() -> dict[str, float]:
    try:
        return get_broker().get_stock_positions()
    except Exception as e:
        logger.error(f"get_stock_positions: {e}")
        return {}


def supports_multi_leg() -> bool:
    try:
        return get_broker().supports_multi_leg()
    except Exception:
        return False


def submit_multi_leg_order(legs: list[dict], qty: int, net_limit_price: float,
                           order_side: str, strategy: str = "",
                           regime_name: str = "", use_market: bool = False) -> Optional[str]:
    return get_broker().submit_multi_leg_order(
        legs=legs, qty=qty, net_limit_price=net_limit_price,
        order_side=order_side, strategy=strategy, regime_name=regime_name,
        use_market=use_market,
    )


def cancel_stale_orders(max_age_seconds: int = 180) -> int:
    try:
        return int(get_broker().cancel_stale_orders(max_age_seconds=max_age_seconds))
    except Exception as e:
        logger.debug(f"cancel_stale_orders: {e}")
        return 0


def wait_for_order_fill(order_id: str, timeout_sec: float = 8.0, poll_sec: float = 0.5) -> str:
    try:
        return get_broker().wait_for_order_fill(order_id, timeout_sec=timeout_sec, poll_sec=poll_sec)
    except Exception as e:
        logger.debug(f"wait_for_order_fill: {e}")
        return "unknown"


def get_portfolio_history(period: str = "1M", timeframe: str = "1D") -> dict:
    try:
        return get_broker().get_portfolio_history(period=period, timeframe=timeframe)
    except NotImplementedError:
        return {"timestamps": [], "equity": [], "profit_loss": [],
                "profit_loss_pct": [], "base_value": 0.0}
    except Exception as e:
        logger.error(f"get_portfolio_history({period},{timeframe}): {e}")
        return {"timestamps": [], "equity": [], "profit_loss": [],
                "profit_loss_pct": [], "base_value": 0.0}


def get_account_baseline() -> float:
    """max(starting_capital, historical_peak_equity) from the broker —
    authoritative floor for peak_equity / drawdown calculations."""
    try:
        return float(get_broker().get_account_baseline() or 0)
    except NotImplementedError:
        return 0.0
    except Exception as e:
        logger.error(f"get_account_baseline: {e}")
        return 0.0
