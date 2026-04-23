"""
Alpaca broker — paper and live trading via alpaca-py SDK.
Paper endpoint: https://paper-api.alpaca.markets

During regular hours:  market orders (notional dollar amount).
During extended hours: limit orders only (Alpaca requirement).
  - Buy  limit = last_price * (1 + EXTENDED_HOURS_LIMIT_OFFSET_PCT)
  - Sell limit = last_price * (1 - EXTENDED_HOURS_LIMIT_OFFSET_PCT)
Extended hours trading is gated by EXTENDED_HOURS_ENABLED in settings.
"""
from typing import Optional
from executor.base import BrokerBase
from monitoring.logger import get_logger, log_trade

logger = get_logger("alpaca_broker")


def _underlying_from_occ(contract_symbol: str) -> str:
    """Strip OCC suffix (YYMMDD + C/P + 8-digit strike) to recover the
    underlying ticker. Accepts 'O:' prefix."""
    s = contract_symbol[2:] if contract_symbol.startswith("O:") else contract_symbol
    if len(s) < 15:
        return ""
    return s[:-15]


class AlpacaBroker(BrokerBase):

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        mode = "PAPER" if paper else "LIVE"
        logger.info(f"Alpaca broker initialized ({mode})")

    # ── Account ───────────────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        try:
            return float(self._trading.get_account().equity)
        except Exception as e:
            logger.error(f"get_portfolio_value: {e}")
            return 0.0

    def get_cash(self) -> float:
        try:
            return float(self._trading.get_account().cash)
        except Exception as e:
            logger.error(f"get_cash: {e}")
            return 0.0

    # ── Orders ────────────────────────────────────────────────────────────────

    def buy_notional(self, symbol: str, dollars: float, regime_name: str = "") -> Optional[str]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from core.market_data import current_session
        import config.settings as cfg

        session = current_session()
        in_extended = session in ("pre_market", "after_hours")

        try:
            if in_extended:
                # Limit order required — convert dollars to qty + limit price
                from alpaca.trading.requests import LimitOrderRequest
                price = self._latest_price(symbol)
                if not price:
                    logger.error(f"buy_notional: could not get price for {symbol}")
                    return None
                limit_price = round(price * (1 + cfg.EXTENDED_HOURS_LIMIT_OFFSET_PCT), 2)
                qty = round(dollars / limit_price, 6)
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    limit_price=limit_price,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    extended_hours=True,
                )
                order_type = "limit_extended"
            else:
                from alpaca.trading.requests import MarketOrderRequest
                req = MarketOrderRequest(
                    symbol=symbol,
                    notional=round(dollars, 2),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                price = self._latest_price(symbol)
                qty = round(dollars / price, 6) if price else 0.0
                order_type = "market_order"

            order = self._trading.submit_order(req)
            order_id = str(order.id)
            log_trade(symbol, "BUY", qty, price, order_type, regime_name, order_id)
            logger.info(f"BUY {symbol} ${dollars:.2f} [{session}] → order {order_id}")
            return order_id

        except Exception as e:
            logger.error(f"buy_notional({symbol}, ${dollars}): {e}")
            return None

    def sell_position(self, symbol: str, current_price: float, reason: str = "", regime_name: str = "") -> Optional[str]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from core.market_data import current_session
        import config.settings as cfg

        session = current_session()
        in_extended = session in ("pre_market", "after_hours")

        try:
            if in_extended:
                from alpaca.trading.requests import LimitOrderRequest
                # Fetch current quantity from broker
                positions = {p.symbol: p for p in self._trading.get_all_positions()}
                if symbol not in positions:
                    logger.warning(f"sell_position: no open position for {symbol}")
                    return None
                qty = float(positions[symbol].qty)
                limit_price = round(current_price * (1 - cfg.EXTENDED_HOURS_LIMIT_OFFSET_PCT), 2)
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    limit_price=limit_price,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    extended_hours=True,
                )
                order = self._trading.submit_order(req)
                order_id = str(order.id)
                log_trade(symbol, "SELL", qty, current_price, reason, regime_name, order_id)
                logger.info(f"SELL {symbol} @ ${current_price:.2f} [limit, {session}] reason={reason} → {order_id}")
            else:
                # close_position submits a market sell for the full qty
                order = self._trading.close_position(symbol)
                order_id = str(order.id)
                qty = float(order.qty or 0)
                log_trade(symbol, "SELL", qty, current_price, reason, regime_name, order_id)
                logger.info(f"SELL {symbol} @ ${current_price:.2f} [market] reason={reason} → {order_id}")

            return order_id

        except Exception as e:
            logger.error(f"sell_position({symbol}): {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            from uuid import UUID
            self._trading.cancel_order_by_id(UUID(order_id))
            return True
        except Exception as e:
            logger.error(f"cancel_order({order_id}): {e}")
            return False

    def get_open_positions(self) -> dict[str, float]:
        try:
            return {p.symbol: float(p.qty) for p in self._trading.get_all_positions()}
        except Exception as e:
            logger.error(f"get_open_positions: {e}")
            return {}

    # ── Options ───────────────────────────────────────────────────────────────

    def supports_options(self) -> bool:
        return True

    def buy_option(self, contract_symbol: str, qty: int, limit_price: float,
                   regime_name: str = "",
                   protective_tp_pct: float = 0.0,
                   protective_sl_pct: float = 0.0) -> Optional[str]:
        """Open a long option position (BUY_TO_OPEN). Single-leg limit order.

        If both `protective_tp_pct` and `protective_sl_pct` > 0, waits for
        the buy to fill and then submits an **OCO sell** (one limit TP +
        one stop-limit SL). Whichever fills first auto-cancels the other —
        broker-side TP/SL management, no polling needed. The paired order
        id is recorded at `_last_stop_order_id` for the caller to persist
        (so we can cancel it if we manually exit via TP/DTE/regime change).
        """
        self._last_stop_order_id = None
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
            from alpaca.trading.requests import (
                LimitOrderRequest, StopOrderRequest, StopLossRequest, TakeProfitRequest,
            )
            req = LimitOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                limit_price=round(limit_price, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.SIMPLE,
            )
            order = self._trading.submit_order(req)
            order_id = str(order.id)
            log_trade(contract_symbol, "BUY_OPT", qty, limit_price,
                      "open_long", regime_name, order_id)
            logger.info(f"BUY_OPT {contract_symbol} x{qty} @ ${limit_price:.2f} → {order_id}")

            want_tp = protective_tp_pct and protective_tp_pct > 0
            want_sl = protective_sl_pct and protective_sl_pct > 0
            if want_tp or want_sl:
                status = self.wait_for_order_fill(order_id, timeout_sec=5.0)
                if status != "filled":
                    logger.warning(f"  skip protective order — buy {order_id} status={status}")
                    return order_id
                fill_price = limit_price
                try:
                    o = self._trading.get_order_by_id(order_id)
                    if getattr(o, "filled_avg_price", None):
                        fill_price = float(o.filled_avg_price)
                except Exception:
                    pass

                if want_tp and want_sl:
                    # OCO: limit-sell TP + stop-limit SL — broker cancels the
                    # other automatically on fill
                    tp_price = round(fill_price * (1 + protective_tp_pct), 2)
                    sl_trigger = round(max(fill_price * (1 - protective_sl_pct), 0.01), 2)
                    sl_limit   = round(max(sl_trigger * 0.95, 0.01), 2)
                    try:
                        oco_req = LimitOrderRequest(
                            symbol=contract_symbol,
                            qty=qty,
                            limit_price=tp_price,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC,
                            order_class=OrderClass.OCO,
                            stop_loss=StopLossRequest(stop_price=sl_trigger, limit_price=sl_limit),
                        )
                        oco = self._trading.submit_order(oco_req)
                        self._last_stop_order_id = str(oco.id)
                        logger.info(f"  OCO: TP ${tp_price:.2f} / SL trigger ${sl_trigger:.2f} → {self._last_stop_order_id}")
                    except Exception as e:
                        logger.error(f"  OCO submit failed for {contract_symbol}: {e} — falling back to bare stop")
                        want_tp = False  # fall through to SL-only path below
                if want_sl and not self._last_stop_order_id:
                    # SL-only fallback (OCO failed or TP not requested)
                    stop_price = round(max(fill_price * (1 - protective_sl_pct), 0.01), 2)
                    try:
                        stop_req = StopOrderRequest(
                            symbol=contract_symbol,
                            qty=qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                            stop_price=stop_price,
                        )
                        stop = self._trading.submit_order(stop_req)
                        self._last_stop_order_id = str(stop.id)
                        logger.info(f"  protective STOP @ ${stop_price:.2f} → {self._last_stop_order_id}")
                    except Exception as e:
                        logger.error(f"  protective stop submit failed for {contract_symbol}: {e}")
            return order_id
        except Exception as e:
            logger.error(f"buy_option({contract_symbol} x{qty}): {e}")
            return None

    def sell_option(self, contract_symbol: str, qty: int, limit_price: float,
                    reason: str = "", regime_name: str = "") -> Optional[str]:
        """Close a long option position (SELL_TO_CLOSE). Single-leg limit order."""
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
            from alpaca.trading.requests import LimitOrderRequest
            req = LimitOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                limit_price=round(limit_price, 2),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.SIMPLE,
            )
            order = self._trading.submit_order(req)
            order_id = str(order.id)
            log_trade(contract_symbol, "SELL_OPT", qty, limit_price,
                      reason or "close_long", regime_name, order_id)
            logger.info(f"SELL_OPT {contract_symbol} x{qty} @ ${limit_price:.2f} reason={reason} → {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"sell_option({contract_symbol}): {e}")
            return None

    # ── Multi-leg options (MLEG) ──────────────────────────────────────────────
    def supports_multi_leg(self) -> bool:
        return True

    def submit_multi_leg_order(
        self,
        legs: list[dict],
        qty: int,
        net_limit_price: float,
        order_side: str,
        strategy: str = "",
        regime_name: str = "",
        **kwargs,
    ) -> Optional[str]:
        """Submit a multi-leg (2-leg vertical or 4-leg iron condor) order to Alpaca.
        Pass use_market=True to submit a MarketOrderRequest instead of limit —
        for emergency SL exits where fill speed > price.

        legs item format: {"contract_symbol": "O:SPY...", "side": "buy"|"sell", "position_intent": "open"|"close"}

        order_side: "buy" → net debit (we pay); "sell" → net credit (we receive).
        Alpaca accepts the net limit price as a positive value regardless of direction;
        the order_side distinguishes debit vs credit at the parent-order level.
        """
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, PositionIntent
            from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        except ImportError:
            logger.error("alpaca-py SDK missing MLEG types — upgrade alpaca-py")
            return None

        intent_map = {
            ("buy", "open"):  PositionIntent.BUY_TO_OPEN,
            ("sell", "open"): PositionIntent.SELL_TO_OPEN,
            ("buy", "close"): PositionIntent.BUY_TO_CLOSE,
            ("sell", "close"): PositionIntent.SELL_TO_CLOSE,
        }
        side_map = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}

        try:
            leg_reqs = []
            for lg in legs:
                intent = intent_map[(lg["side"], lg["position_intent"])]
                leg_reqs.append(OptionLegRequest(
                    symbol=lg["contract_symbol"],
                    side=side_map[lg["side"]],
                    position_intent=intent,
                    ratio_qty=lg.get("ratio_qty", 1),
                ))
            use_market = kwargs.get("use_market", False)
            if use_market:
                from alpaca.trading.requests import MarketOrderRequest
                req = MarketOrderRequest(
                    qty=qty,
                    order_class=OrderClass.MLEG,
                    time_in_force=TimeInForce.DAY,
                    side=side_map[order_side],
                    legs=leg_reqs,
                )
            else:
                req = LimitOrderRequest(
                    qty=qty,
                    order_class=OrderClass.MLEG,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(abs(net_limit_price), 2),
                    side=side_map[order_side],
                    legs=leg_reqs,
                )
            order = self._trading.submit_order(req)
            order_id = str(order.id)
            leg_desc = " / ".join(f"{lg['side']} {lg['contract_symbol']}" for lg in legs)
            underlying = _underlying_from_occ(legs[0]["contract_symbol"]) if legs else ""
            log_trade(underlying or (strategy or "MLEG"), "MLEG", qty, net_limit_price,
                      f"{order_side}_{strategy}", regime_name, order_id,
                      strategy=strategy or "")
            # Durable ledger record — authoritative net_entry source for reconciliation
            try:
                from core.orders_ledger import record_multi_leg_submission
                record_multi_leg_submission(
                    order_id=order_id, strategy=strategy or "",
                    underlying=underlying, legs=legs, qty=qty,
                    net_limit_price=net_limit_price, order_side=order_side,
                )
            except Exception as e:
                logger.warning(f"ledger record failed: {e}")
            logger.info(f"MLEG {strategy} x{qty} @ net ${net_limit_price:.2f} ({order_side}): "
                        f"{leg_desc} → {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"submit_multi_leg_order({strategy} x{qty}): {e}")
            return None

    def get_stock_positions(self) -> dict[str, float]:
        """Only non-option positions keyed by symbol → qty."""
        try:
            positions = self._trading.get_all_positions()
        except Exception as e:
            logger.error(f"get_stock_positions: {e}")
            return {}
        out: dict[str, float] = {}
        for p in positions:
            asset_class = str(getattr(p, "asset_class", "")).lower()
            if asset_class and "option" in asset_class:
                continue
            out[p.symbol] = float(p.qty)
        return out

    def get_option_positions(self) -> list[dict]:
        """Return list of option positions held at the broker.

        Each item: {symbol, qty, avg_entry_price, current_price, unrealized_pl}.
        """
        try:
            positions = self._trading.get_all_positions()
        except Exception as e:
            logger.error(f"get_option_positions: {e}")
            return []
        out = []
        for p in positions:
            asset_class = str(getattr(p, "asset_class", "")).lower()
            if "option" not in asset_class:
                continue
            out.append({
                "symbol": p.symbol,
                "qty": int(float(p.qty)),
                "avg_entry_price": float(p.avg_entry_price or 0),
                "current_price": float(getattr(p, "current_price", 0) or 0),
                "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
            })
        return out

    # ── Order lifecycle helpers (fast fill / stale cleanup) ──────────────────

    def cancel_stale_orders(self, max_age_seconds: int = 180) -> int:
        """Cancel any open orders older than max_age_seconds. Prevents the
        'held_for_orders' lockout where a stale limit close blocks new
        MLEG submissions on the same legs. Returns count cancelled."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            from datetime import datetime, timezone
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            orders = self._trading.get_orders(filter=req)
        except Exception as e:
            logger.debug(f"cancel_stale_orders list failed: {e}")
            return 0
        now = datetime.now(timezone.utc)
        cancelled = 0
        for o in orders:
            submitted = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            if not submitted:
                continue
            age = (now - submitted).total_seconds()
            if age < max_age_seconds:
                continue
            try:
                self._trading.cancel_order_by_id(str(o.id))
                logger.warning(f"cancelled stale order {o.id} ({o.symbol or o.order_class}) age={age:.0f}s")
                cancelled += 1
            except Exception as e:
                logger.warning(f"cancel {o.id} failed: {e}")
        return cancelled

    def wait_for_order_fill(self, order_id: str, timeout_sec: float = 8.0, poll_sec: float = 0.5) -> str:
        """Poll an order until filled / cancelled / expired / timeout.
        Returns final status string. Used on SL exits to verify the fill
        landed fast; caller escalates to market if status != 'filled'."""
        import time as _time
        end = _time.time() + timeout_sec
        last_status = "unknown"
        while _time.time() < end:
            try:
                o = self._trading.get_order_by_id(order_id)
                last_status = str(o.status).split(".")[-1].lower()
                if last_status in ("filled", "canceled", "expired", "rejected", "done_for_day"):
                    return last_status
            except Exception as e:
                logger.debug(f"wait_for_order_fill poll failed: {e}")
            _time.sleep(poll_sec)
        return last_status

    # ── Account baseline (authoritative peak for drawdown) ────────────────────

    _baseline_cache: dict = {"value": None, "ts": None}

    def get_account_baseline(self) -> float:
        """Returns max(starting_capital, historical_peak_equity) from Alpaca's
        full portfolio history. Cached 1h — peak moves rarely enough, and
        the full-period history fetch is heavy."""
        import time as _time
        cache = AlpacaBroker._baseline_cache
        now = _time.time()
        if cache["value"] is not None and cache["ts"] and (now - cache["ts"]) < 3600:
            return cache["value"]
        try:
            h = self.get_portfolio_history(period="all", timeframe="1D")
            equity = [e for e in (h.get("equity") or []) if e and e > 0]
            base_value = float(h.get("base_value") or 0)
            historical_peak = max(equity) if equity else 0.0
            baseline = max(base_value, historical_peak)
        except Exception as e:
            logger.warning(f"get_account_baseline: falling back to get_account: {e}")
            try:
                acct = self._trading.get_account()
                baseline = float(getattr(acct, "equity", 0) or 0)
            except Exception:
                baseline = 0.0
        cache["value"] = baseline
        cache["ts"] = now
        return baseline

    # ── Portfolio history (for perf charts) ───────────────────────────────────

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict:
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            req = GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
            h = self._trading.get_portfolio_history(req)
        except Exception as e:
            logger.warning(f"get_portfolio_history({period}, {timeframe}): {e}")
            return {"timestamps": [], "equity": [], "profit_loss": [],
                    "profit_loss_pct": [], "base_value": 0.0}
        return {
            "timestamps":      [int(t) for t in (getattr(h, "timestamp", None) or [])],
            "equity":          [float(e or 0) for e in (getattr(h, "equity", None) or [])],
            "profit_loss":     [float(p or 0) for p in (getattr(h, "profit_loss", None) or [])],
            "profit_loss_pct": [float(p or 0) for p in (getattr(h, "profit_loss_pct", None) or [])],
            "base_value":      float(getattr(h, "base_value", 0) or 0),
        }

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _latest_price(self, symbol: str) -> float:
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quote = self._data.get_stock_latest_quote(req)
            return float(quote[symbol].ask_price or quote[symbol].bid_price)
        except Exception as e:
            logger.warning(f"_latest_price({symbol}): {e}")
            return 0.0
