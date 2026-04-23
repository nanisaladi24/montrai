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

    # ── Options ───────────────────────────────────────────────────────────────
    def supports_options(self) -> bool:
        return True

    def supports_multi_leg(self) -> bool:
        return True

    def buy_option(self, contract_symbol: str, qty: int, limit_price: float,
                   regime_name: str = "") -> Optional[str]:
        """Open a long option position via robin_stocks.options.order_buy_option_limit.

        robin_stocks identifies contracts by (symbol, expiry, strike, contract_type),
        not OCC ticker. We parse the OCC symbol (O:SPY260619C00720000 format).
        """
        spec = _parse_occ(contract_symbol)
        if spec is None:
            logger.error(f"buy_option: cannot parse {contract_symbol}")
            return None
        try:
            order = self._rh.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",
                price=round(limit_price, 2),
                symbol=spec["underlying"],
                quantity=qty,
                expirationDate=spec["expiry"],
                strike=spec["strike"],
                optionType=spec["contract_type"],
                timeInForce="gfd",
            )
            order_id = order.get("id", "unknown")
            log_trade(contract_symbol, "BUY_OPT", qty, limit_price, "open_long",
                      regime_name, order_id)
            logger.info(f"BUY_OPT {contract_symbol} x{qty} @ ${limit_price:.2f} → {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"buy_option({contract_symbol}): {e}")
            return None

    def sell_option(self, contract_symbol: str, qty: int, limit_price: float,
                    reason: str = "", regime_name: str = "") -> Optional[str]:
        spec = _parse_occ(contract_symbol)
        if spec is None:
            logger.error(f"sell_option: cannot parse {contract_symbol}")
            return None
        try:
            # `open` vs `close` matters — if the reason hints at a covered call we
            # SELL_TO_OPEN; otherwise we're closing a long leg.
            position_effect = "open" if "covered" in (reason or "").lower() else "close"
            credit_or_debit = "credit" if position_effect == "open" else "debit"
            order = self._rh.order_sell_option_limit(
                positionEffect=position_effect,
                creditOrDebit=credit_or_debit,
                price=round(limit_price, 2),
                symbol=spec["underlying"],
                quantity=qty,
                expirationDate=spec["expiry"],
                strike=spec["strike"],
                optionType=spec["contract_type"],
                timeInForce="gfd",
            )
            order_id = order.get("id", "unknown")
            log_trade(contract_symbol, "SELL_OPT", qty, limit_price,
                      reason or "close_long", regime_name, order_id)
            logger.info(f"SELL_OPT {contract_symbol} x{qty} @ ${limit_price:.2f} "
                        f"({position_effect}) reason={reason} → {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"sell_option({contract_symbol}): {e}")
            return None

    def get_option_positions(self) -> list[dict]:
        try:
            positions = self._rh.get_open_option_positions()
        except Exception as e:
            logger.error(f"get_option_positions: {e}")
            return []
        out = []
        for p in positions:
            qty = int(float(p.get("quantity") or 0))
            if qty == 0:
                continue
            out.append({
                "symbol": p.get("chain_symbol", ""),
                "qty": qty,
                "avg_entry_price": float(p.get("average_price") or 0),
                "current_price": float(p.get("current_price") or 0),
                "unrealized_pl": 0.0,
                "market_value": float(p.get("market_value") or 0),
            })
        return out

    def submit_multi_leg_order(
        self,
        legs: list[dict],
        qty: int,
        net_limit_price: float,
        order_side: str,
        strategy: str = "",
        regime_name: str = "",
    ) -> Optional[str]:
        """Multi-leg via robin_stocks.orders.order_option_spread.

        robin_stocks expects leg descriptors like:
            {"expirationDate": "YYYY-MM-DD", "strike": float, "optionType": "call"|"put",
             "effect": "open"|"close", "action": "buy"|"sell"}
        """
        rh_legs = []
        underlying = None
        for lg in legs:
            spec = _parse_occ(lg["contract_symbol"])
            if spec is None:
                logger.error(f"submit_multi_leg_order: cannot parse {lg['contract_symbol']}")
                return None
            underlying = underlying or spec["underlying"]
            rh_legs.append({
                "expirationDate": spec["expiry"],
                "strike": spec["strike"],
                "optionType": spec["contract_type"],
                "effect": lg["position_intent"],   # "open" or "close"
                "action": lg["side"],              # "buy" or "sell"
            })

        credit_or_debit = "debit" if order_side == "buy" else "credit"
        try:
            # robin_stocks v3+ exposes order_option_spread(); earlier versions may
            # differ. Wrap in getattr so we fail gracefully with a clear log.
            fn = getattr(self._rh, "order_option_spread", None)
            if fn is None:
                logger.error("robin_stocks lacks order_option_spread — upgrade the package")
                return None
            order = fn(
                direction=credit_or_debit,
                price=round(abs(net_limit_price), 2),
                symbol=underlying,
                quantity=qty,
                spread=rh_legs,
                timeInForce="gfd",
            )
            order_id = (order or {}).get("id", "unknown")
            leg_desc = " / ".join(f"{l['action']} {l['optionType']} {l['strike']}" for l in rh_legs)
            log_trade(underlying or (strategy or "MLEG"), "MLEG", qty, net_limit_price,
                      f"{order_side}_{strategy}", regime_name, order_id,
                      strategy=strategy or "")
            try:
                from core.orders_ledger import record_multi_leg_submission
                record_multi_leg_submission(
                    order_id=order_id, strategy=strategy or "",
                    underlying=underlying or "", legs=legs, qty=qty,
                    net_limit_price=net_limit_price, order_side=order_side,
                )
            except Exception as e:
                logger.warning(f"ledger record failed: {e}")
            logger.info(f"MLEG {strategy} x{qty} @ net ${net_limit_price:.2f} ({credit_or_debit}): "
                        f"{leg_desc} → {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"submit_multi_leg_order({strategy} x{qty}): {e}")
            return None


def _parse_occ(contract_symbol: str) -> Optional[dict]:
    """Parse OCC-format option symbols used by Alpaca (O:SYMYYMMDDCXXXXXXXX).

    Example: O:SPY260619C00720000
             └──┬──┘└─┬─┘└┬┘└───┬───┘
             underlying  exp  C/P  strike×1000

    Returns {"underlying", "expiry" (YYYY-MM-DD), "contract_type" ("call"|"put"), "strike"}.
    """
    s = contract_symbol
    if s.startswith("O:"):
        s = s[2:]
    # OCC: [root 1-6 chars][YYMMDD][C/P][strike×1000, 8 digits]
    if len(s) < 15:
        return None
    try:
        # Walk from the right: 8 digits strike, 1 char type, 6 digits date
        strike_str = s[-8:]
        type_char = s[-9]
        date_str = s[-15:-9]
        root = s[:-15]
        strike = int(strike_str) / 1000.0
        contract_type = "call" if type_char.upper() == "C" else "put"
        expiry = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}"
        return {"underlying": root, "expiry": expiry,
                "contract_type": contract_type, "strike": strike}
    except Exception:
        return None
