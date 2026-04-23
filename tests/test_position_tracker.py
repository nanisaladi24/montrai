import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from core import position_tracker as pt
from core.position_tracker import BotState, Position


@pytest.fixture(autouse=True)
def _isolate_state_file(tmp_path, monkeypatch):
    """Redirect BotState.save() writes to tmp_path so tests never clobber
    the live bot_state.json. Without this, close_position()'s save() call
    corrupts real state every test run."""
    monkeypatch.setattr(pt, "STATE_FILE", str(tmp_path / "bot_state.json"))


def test_position_pnl():
    pos = Position("TSLA", 2.0, 200.0, "2026-01-01", 190.0, 224.0)
    assert abs(pos.unrealized_pnl(220.0) - 40.0) < 0.01
    assert abs(pos.unrealized_pnl_pct(220.0) - 0.10) < 0.001


def test_daily_reset():
    state = BotState()
    state.daily_spent = 400.0
    state.daily_date = "2020-01-01"
    state.reset_daily_if_new_day()
    assert state.daily_spent == 0.0


def test_close_position_updates_pnl():
    state = BotState()
    state.positions["AAPL"] = Position("AAPL", 1.0, 100.0, "2026-01-01", 95.0, 112.0)
    pnl = state.close_position("AAPL", 110.0)
    assert abs(pnl - 10.0) < 0.01
    assert "AAPL" not in state.positions
    assert abs(state.total_realized_pnl - 10.0) < 0.01
