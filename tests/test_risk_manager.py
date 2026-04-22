import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from core.position_tracker import BotState, Position
from risk.risk_manager import RiskManager
from config.settings import STOCK_MAX_DAILY_USD, STOP_LOSS_PCT, TAKE_PROFIT_PCT


def make_state(spent=0.0) -> BotState:
    s = BotState()
    s.daily_spent = spent
    from datetime import date
    s.daily_date = date.today().isoformat()
    return s


def test_daily_cap_blocks_when_full():
    # check_daily_spend now enforces the stock cap, not the legacy combined cap.
    state = make_state(spent=STOCK_MAX_DAILY_USD)
    verdict = RiskManager.check_daily_spend(state, 100.0)
    assert not verdict.allowed


def test_daily_cap_trims_excess():
    state = make_state(spent=STOCK_MAX_DAILY_USD - 50)
    verdict = RiskManager.check_daily_spend(state, 100.0)
    assert verdict.allowed
    assert verdict.adjusted_dollars == 50.0


def test_daily_cap_allows_within_limit():
    state = make_state(spent=0.0)
    verdict = RiskManager.check_daily_spend(state, 200.0)
    assert verdict.allowed
    assert verdict.adjusted_dollars == 200.0


def test_stop_loss_triggers():
    pos = Position("AAPL", 1.0, 100.0, "2026-01-01", 95.0, 112.0)
    should_exit, reason = RiskManager.should_exit_position(pos, 94.9)
    assert should_exit
    assert "stop-loss" in reason


def test_take_profit_triggers():
    pos = Position("AAPL", 1.0, 100.0, "2026-01-01", 95.0, 112.0)
    should_exit, reason = RiskManager.should_exit_position(pos, 112.1)
    assert should_exit
    assert "take-profit" in reason


def test_no_exit_within_range():
    pos = Position("AAPL", 1.0, 100.0, "2026-01-01", 95.0, 112.0)
    should_exit, _ = RiskManager.should_exit_position(pos, 105.0)
    assert not should_exit


def test_compute_stops():
    stop, target = RiskManager.compute_stops(100.0)
    assert abs(stop - 100.0 * (1 - STOP_LOSS_PCT)) < 0.01
    assert abs(target - 100.0 * (1 + TAKE_PROFIT_PCT)) < 0.01


def test_peak_drawdown_creates_lockout(tmp_path, monkeypatch):
    import config.settings as cfg
    monkeypatch.setattr(cfg, "LOCKOUT_FILE", str(tmp_path / "LOCKOUT"))
    monkeypatch.setattr(cfg, "PEAK_DRAWDOWN_LOCKOUT_PCT", 0.10)
    state = BotState()
    state.peak_equity = 10000.0
    result = RiskManager.check_peak_drawdown(8999.0, state)
    assert result
    assert (tmp_path / "LOCKOUT").exists()
