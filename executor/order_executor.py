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
