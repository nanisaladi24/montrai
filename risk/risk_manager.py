"""
Risk Manager — all safety logic lives here and runs independently of the AI signals.
Circuit breakers are hard-coded and cannot be disabled by the trading logic.
"""
import os
from dataclasses import dataclass
from datetime import date
from typing import Tuple
import config.settings as _cfg
from config.settings import (
    MAX_DAILY_SPEND_USD, DAILY_LOSS_HALT_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
)
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

    # ── Circuit Breaker 2: Daily spend cap ───────────────────────────────────
    @staticmethod
    def check_daily_spend(state: BotState, proposed_dollars: float) -> RiskVerdict:
        state.reset_daily_if_new_day()
        remaining = MAX_DAILY_SPEND_USD - state.daily_spent
        if remaining <= 0:
            return RiskVerdict(False, f"Daily spend cap hit (${MAX_DAILY_SPEND_USD})", 0.0)
        allowed = min(proposed_dollars, remaining)
        if allowed < proposed_dollars:
            logger.warning(f"Trade size trimmed to ${allowed:.2f} (daily cap ${MAX_DAILY_SPEND_USD})")
        return RiskVerdict(True, "within daily cap", allowed)

    # ── Circuit Breaker 3: Daily loss trigger ─────────────────────────────────
    @staticmethod
    def check_daily_loss(portfolio_value: float, start_of_day_value: float, state: BotState) -> bool:
        loss_pct = (start_of_day_value - portfolio_value) / max(start_of_day_value, 1)
        if loss_pct >= DAILY_LOSS_HALT_PCT and not state.is_halved:
            state.is_halved = True
            state.save()
            logger.warning(f"Daily loss {loss_pct:.2%} >= {DAILY_LOSS_HALT_PCT:.2%}. Position sizes halved.")
            return True
        return False

    # ── Circuit Breaker 4: Peak drawdown lockout ──────────────────────────────
    @staticmethod
    def check_peak_drawdown(portfolio_value: float, state: BotState) -> bool:
        if portfolio_value > state.peak_equity:
            state.peak_equity = portfolio_value
            state.save()
        peak_dd_pct = _cfg.PEAK_DRAWDOWN_LOCKOUT_PCT
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
        stop_loss = round(entry_price * (1 - STOP_LOSS_PCT), 4)
        take_profit = round(entry_price * (1 + TAKE_PROFIT_PCT), 4)
        return stop_loss, take_profit
