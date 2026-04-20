"""
Robinhood broker — live trading only via robin_stocks.
Not suitable for paper trading; use Alpaca for that.
"""
from typing import Optional
from executor.base import BrokerBase
from monitoring.logger import get_logger, log_trade

logger = get_logger("robinhood_broker")


class RobinhoodBroker(BrokerBase):

    def __init__(self, username: str, password: str, mfa_code: str = ""):
        import robin_stocks.robinhood as rh
        self._rh = rh
        kwargs = dict(username=username, password=password, store_session=True)
        if mfa_code:
            kwargs["mfa_code"] = mfa_code
        rh.login(**kwargs)
        logger.info("Robinhood broker initialized (LIVE)")

    def get_portfolio_value(self) -> float:
        try:
            profile = self._rh.load_portfolio_profile()
            return float(profile.get("equity", 0))
        except Exception as e:
            logger.error(f"get_portfolio_value: {e}")
            return 0.0

    def get_cash(self) -> float:
        try:
            profile = self._rh.load_account_profile()
            return float(profile.get("cash", 0))
        except Exception as e:
            logger.error(f"get_cash: {e}")
            return 0.0

    def buy_notional(self, symbol: str, dollars: float, regime_name: str = "") -> Optional[str]:
        try:
            order = self._rh.order_buy_fractional_by_price(symbol, dollars)
            order_id = order.get("id", "unknown")
            price = float(order.get("average_price") or order.get("price") or 0)
            qty = float(order.get("quantity") or 0)
            log_trade(symbol, "BUY", qty, price, "market_order", regime_name, order_id)
            logger.info(f"BUY {symbol} ${dollars:.2f} → order {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"buy_notional({symbol}, ${dollars}): {e}")
            return None

    def sell_position(self, symbol: str, current_price: float, reason: str = "", regime_name: str = "") -> Optional[str]:
        try:
            positions = self._rh.get_open_stock_positions()
            qty = 0.0
            for p in positions:
                info = self._rh.get_instrument_by_url(p["instrument"])
                if info and info.get("symbol") == symbol:
                    qty = float(p.get("quantity", 0))
                    break
            if qty <= 0:
                logger.warning(f"sell_position: no open position for {symbol}")
                return None
            order = self._rh.order_sell_fractional_by_quantity(symbol, qty)
            order_id = order.get("id", "unknown")
            log_trade(symbol, "SELL", qty, current_price, reason, regime_name, order_id)
            logger.info(f"SELL {symbol} qty={qty} @ ${current_price:.2f} → order {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"sell_position({symbol}): {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._rh.cancel_stock_order(order_id)
            return True
        except Exception as e:
            logger.error(f"cancel_order({order_id}): {e}")
            return False

    def get_open_positions(self) -> dict[str, float]:
        try:
            positions = self._rh.get_open_stock_positions()
            result = {}
            for p in positions:
                info = self._rh.get_instrument_by_url(p["instrument"])
                if info and info.get("symbol"):
                    result[info["symbol"]] = float(p.get("quantity", 0))
            return result
        except Exception as e:
            logger.error(f"get_open_positions: {e}")
            return {}
