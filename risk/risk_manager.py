"""
Risk Manager — all safety logic lives here and runs independently of the AI signals.
Circuit breakers are hard-coded and cannot be disabled by the trading logic.
"""
import os
from dataclasses import dataclass
from datetime import date
from typing import Tuple
import config.settings as _cfg
import config.runtime_config as rc
from core.position_tracker import BotState, Position
from monitoring.logger import get_logger

logger = get_logger("risk_manager")


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str
    adjusted_dollars: float = 0.0


class RiskManager:
    """
    Stateless rule engine — receives state and returns verdicts.
    Every check writes a log line so the audit trail is complete.
    """

    # ── Circuit Breaker 1: Lockout file ───────────────────────────────────────
    @staticmethod
    def check_lockout() -> bool:
        lockout_file = _cfg.LOCKOUT_FILE
        if os.path.exists(lockout_file):
            logger.critical(f"LOCKOUT FILE detected ({lockout_file}). Bot halted.")
            return True
        return False

    # ── Circuit Breaker 2a: Stock daily spend cap ─────────────────────────────
    @staticmethod
    def check_daily_spend(state: BotState, proposed_dollars: float) -> RiskVerdict:
        state.reset_daily_if_new_day()
        cfg = rc.load()
        max_daily = cfg.get("stock_max_daily_usd",
                            cfg.get("max_daily_spend_usd", _cfg.STOCK_MAX_DAILY_USD))
        remaining = max_daily - state.daily_spent
        if remaining <= 0:
            return RiskVerdict(False, f"Stock daily spend cap hit (${max_daily})", 0.0)
        allowed = min(proposed_dollars, remaining)
        if allowed < proposed_dollars:
            logger.warning(f"Stock trade size trimmed to ${allowed:.2f} (daily cap ${max_daily})")
        return RiskVerdict(True, "within stock daily cap", allowed)

    # ── Circuit Breaker 2b: Options daily premium cap ─────────────────────────
    @staticmethod
    def check_options_daily_spend(state: BotState, proposed_dollars: float) -> RiskVerdict:
        """Options premium cap is *separate* from the stock cap. Hitting the
        $1000/day options limit does not affect the stock budget and vice-versa."""
        state.reset_daily_if_new_day()
        max_daily = rc.load().get("options_max_daily_usd", _cfg.OPTIONS_MAX_DAILY_USD)
        remaining = max_daily - state.options_daily_spent
        if remaining <= 0:
            return RiskVerdict(False, f"Options daily cap hit (${max_daily})", 0.0)
        allowed = min(proposed_dollars, remaining)
        if allowed < proposed_dollars:
            logger.warning(f"Options premium trimmed to ${allowed:.2f} (daily cap ${max_daily})")
        return RiskVerdict(True, "within options daily cap", allowed)

    # ── Circuit Breaker 3: Daily loss trigger ─────────────────────────────────
    @staticmethod
    def check_daily_loss(portfolio_value: float, start_of_day_value: float, state: BotState) -> bool:
        halt_pct = rc.load().get("daily_loss_halt_pct", _cfg.DAILY_LOSS_HALT_PCT)
        loss_pct = (start_of_day_value - portfolio_value) / max(start_of_day_value, 1)
        if loss_pct >= halt_pct and not state.is_halved:
            state.is_halved = True
            state.save()
            logger.warning(f"Daily loss {loss_pct:.2%} >= {halt_pct:.2%}. Position sizes halved.")
            return True
        return False

    # ── Circuit Breaker 4: Peak drawdown lockout ──────────────────────────────
    @staticmethod
    def check_peak_drawdown(portfolio_value: float, state: BotState) -> bool:
        if portfolio_value > state.peak_equity:
            state.peak_equity = portfolio_value
            state.save()
        peak_dd_pct = rc.load().get("peak_drawdown_lockout_pct", _cfg.PEAK_DRAWDOWN_LOCKOUT_PCT)
        drawdown = (state.peak_equity - portfolio_value) / max(state.peak_equity, 1)
        if drawdown >= peak_dd_pct:
            logger.critical(
                f"PEAK DRAWDOWN {drawdown:.2%} >= {peak_dd_pct:.2%}. "
                f"Creating lockout file and halting bot."
            )
            with open(_cfg.LOCKOUT_FILE, "w") as f:
                f.write(f"Drawdown lockout triggered at {date.today().isoformat()}\n"
                        f"Peak: ${state.peak_equity:.2f} | Current: ${portfolio_value:.2f}\n"
                        f"Drawdown: {drawdown:.2%}\n"
                        f"Delete this file and restart the bot manually to resume.")
            return True
        return False

    # ── Position exit rules ───────────────────────────────────────────────────
    @staticmethod
    def should_exit_position(pos: Position, current_price: float) -> Tuple[bool, str]:
        pnl_pct = pos.unrealized_pnl_pct(current_price)

        # Hard stop-loss
        if current_price <= pos.stop_loss:
            return True, f"stop-loss hit ({pnl_pct:.2%})"

        # Take profit
        if current_price >= pos.take_profit:
            return True, f"take-profit hit ({pnl_pct:.2%})"

        return False, ""

    # ── Pre-trade approval ────────────────────────────────────────────────────
    @staticmethod
    def approve_trade(
        symbol: str,
        proposed_dollars: float,
        state: BotState,
        portfolio_value: float,
    ) -> RiskVerdict:
        if RiskManager.check_lockout():
            return RiskVerdict(False, "bot is locked out", 0.0)

        verdict = RiskManager.check_daily_spend(state, proposed_dollars)
        if not verdict.allowed:
            return verdict

        logger.info(f"Trade approved: {symbol} ${verdict.adjusted_dollars:.2f}")
        return verdict

    @staticmethod
    def compute_stops(entry_price: float) -> Tuple[float, float]:
        cfg = rc.load()
        sl_pct = cfg.get("stop_loss_pct", _cfg.STOP_LOSS_PCT)
        tp_pct = cfg.get("take_profit_pct", _cfg.TAKE_PROFIT_PCT)
        stop_loss = round(entry_price * (1 - sl_pct), 4)
        take_profit = round(entry_price * (1 + tp_pct), 4)
        return stop_loss, take_profit

    # ── Options pre-trade approval ────────────────────────────────────────────
    @staticmethod
    def approve_options_trade(
        contract_symbol: str,
        proposed_dollars: float,
        state: BotState,
    ) -> RiskVerdict:
        if RiskManager.check_lockout():
            return RiskVerdict(False, "bot is locked out", 0.0)
        verdict = RiskManager.check_options_daily_spend(state, proposed_dollars)
        if not verdict.allowed:
            return verdict
        logger.info(f"Options trade approved: {contract_symbol} ${verdict.adjusted_dollars:.2f}")
        return verdict

    # ── Options exit rules ────────────────────────────────────────────────────
    @staticmethod
    def should_exit_option(
        entry_premium: float,
        current_premium: float,
        dte: int,
        is_short: bool = False,
    ) -> Tuple[bool, str]:
        """Direction-aware exit check.

        Long: TP when current ≥ entry × (1+TP); SL when current ≤ entry × (1-SL).
        Short: TP when decay ≥ TP (current ≤ entry × (1-TP));
               SL when current ≥ entry × 2 (premium doubled against us).
        """
        cfg = rc.load()
        tp = cfg.get("options_take_profit_pct", _cfg.OPTIONS_TAKE_PROFIT_PCT)
        sl = cfg.get("options_stop_loss_pct", _cfg.OPTIONS_STOP_LOSS_PCT)
        min_dte = cfg.get("options_min_dte_exit", _cfg.OPTIONS_MIN_DTE_EXIT)
        if entry_premium <= 0:
            return False, ""
        if is_short:
            decay = (entry_premium - current_premium) / entry_premium  # +ve = profit
            if decay >= tp:
                return True, f"short TP decay {decay:.1%}"
            if current_premium >= entry_premium * 2:
                return True, f"short SL premium doubled ({current_premium:.2f} vs entry {entry_premium:.2f})"
            if dte <= min_dte:
                return True, f"DTE {dte} ≤ {min_dte}"
            return False, ""
        change = (current_premium - entry_premium) / entry_premium
        if change >= tp:
            return True, f"take-profit hit (+{change:.1%})"
        if change <= -sl:
            return True, f"stop-loss hit ({change:.1%})"
        if dte <= min_dte:
            return True, f"DTE {dte} ≤ {min_dte}"
        return False, ""
