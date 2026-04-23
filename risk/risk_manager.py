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
        """Stock spend check — must pass BOTH caps:
          • daily flow cap (resets each day) — how much fresh notional to deploy today
          • deployed cap — max total notional in play at any time
        Proposed size is trimmed to whichever headroom is smaller."""
        state.reset_daily_if_new_day()
        cfg = rc.load()
        max_daily = cfg.get("stock_max_daily_usd",
                            cfg.get("max_daily_spend_usd", _cfg.STOCK_MAX_DAILY_USD))
        max_deployed = cfg.get("stock_max_deployed_usd", _cfg.STOCK_MAX_DEPLOYED_USD)
        daily_remain    = max_daily - state.daily_spent
        deployed_remain = max_deployed - state.stock_capital_deployed()
        if daily_remain <= 0:
            return RiskVerdict(False, f"Stock daily cap hit (${max_daily:.0f})", 0.0)
        if deployed_remain <= 0:
            return RiskVerdict(False, f"Stock deployed cap hit (${state.stock_capital_deployed():.0f}/${max_deployed:.0f})", 0.0)
        allowed = min(proposed_dollars, daily_remain, deployed_remain)
        if allowed < proposed_dollars:
            logger.warning(
                f"Stock trade trimmed to ${allowed:.2f} · "
                f"daily ${state.daily_spent:.0f}/${max_daily:.0f} · "
                f"deployed ${state.stock_capital_deployed():.0f}/${max_deployed:.0f}"
            )
        return RiskVerdict(True, "within stock caps", allowed)

    # ── Circuit Breaker 2b: Options daily + deployed caps ─────────────────────
    @staticmethod
    def check_options_daily_spend(state: BotState, proposed_dollars: float) -> RiskVerdict:
        """Options spend — must pass BOTH caps:
          • daily premium cap (flow, resets each day)
          • deployed cap — total cost-basis + capital-at-risk currently in play
        """
        state.reset_daily_if_new_day()
        cfg = rc.load()
        max_daily = cfg.get("options_max_daily_usd", _cfg.OPTIONS_MAX_DAILY_USD)
        max_deployed = cfg.get("options_max_deployed_usd", _cfg.OPTIONS_MAX_DEPLOYED_USD)
        daily_remain    = max_daily - state.options_daily_spent
        deployed_remain = max_deployed - state.options_capital_deployed()
        if daily_remain <= 0:
            return RiskVerdict(False, f"Options daily cap hit (${max_daily:.0f})", 0.0)
        if deployed_remain <= 0:
            return RiskVerdict(False, f"Options deployed cap hit (${state.options_capital_deployed():.0f}/${max_deployed:.0f})", 0.0)
        allowed = min(proposed_dollars, daily_remain, deployed_remain)
        if allowed < proposed_dollars:
            logger.warning(
                f"Options premium trimmed to ${allowed:.2f} · "
                f"daily ${state.options_daily_spent:.0f}/${max_daily:.0f} · "
                f"deployed ${state.options_capital_deployed():.0f}/${max_deployed:.0f}"
            )
        return RiskVerdict(True, "within options caps", allowed)

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
        # Floor peak with broker's authoritative baseline (starting capital +
        # historical peak from Alpaca's portfolio history) so a bot restart
        # during a drawdown can't seed peak from a drawdown-era snapshot.
        try:
            from executor.order_executor import get_account_baseline
            broker_baseline = get_account_baseline()
            if broker_baseline > state.peak_equity:
                state.peak_equity = broker_baseline
                state.save()
        except Exception as e:
            logger.debug(f"get_account_baseline failed: {e}")
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

    # ── Multi-leg spread exit rules ───────────────────────────────────────────
    @staticmethod
    def should_exit_multi_leg(
        net_entry: float,          # absolute per-unit dollars at entry
        current_net_value: float,  # absolute per-unit dollars to close now
        qty: int,                  # signed: +N = long/debit, -N = short/credit
        dte: int,
        skip_sl: bool = False,     # True for reconciled orphans — entry can't be trusted
    ) -> Tuple[bool, str]:
        """Direction-aware exit for vertical spreads + iron condors.

        Long spread (debit): TP when current ≥ entry × (1+tp); SL when current ≤ entry × (1-sl).
        Short spread (credit): TP when current ≤ entry × (1-tp); SL when current ≥ entry × 2.

        When skip_sl=True (orphan reconciliation fallback), stop-loss paths
        are suppressed because we don't have an authoritative entry price —
        we rely on TP + DTE to exit.
        """
        cfg = rc.load()
        tp = cfg.get("spread_take_profit_pct", _cfg.SPREAD_TAKE_PROFIT_PCT)
        sl = cfg.get("spread_stop_loss_pct",   _cfg.SPREAD_STOP_LOSS_PCT)
        min_dte = cfg.get("options_min_dte_exit", _cfg.OPTIONS_MIN_DTE_EXIT)
        if net_entry <= 0 or qty == 0:
            return False, ""
        if qty > 0:   # debit — we own it
            pct = (current_net_value - net_entry) / net_entry
            if pct >= tp:
                return True, f"debit TP +{pct:.1%}"
            if pct <= -sl and not skip_sl:
                return True, f"debit SL {pct:.1%}"
        else:         # credit — we sold it
            decay = (net_entry - current_net_value) / net_entry
            if decay >= tp:
                return True, f"credit TP decay {decay:.1%}"
            if current_net_value >= net_entry * 2 and not skip_sl:
                return True, f"credit SL ({current_net_value:.2f} vs entry {net_entry:.2f})"
        if dte <= min_dte:
            return True, f"DTE {dte} ≤ {min_dte}"
        return False, ""

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
