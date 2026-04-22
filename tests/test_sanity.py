"""
Sanity tests for the options-primary build.

Scope: behaviors that fail silently or regress across refactors — signed-qty
option math, direction-aware exits, separate stock/options daily caps, options
strategy selector, covered-call config surface, stable HMM schema.

Run: .venv/bin/python -m pytest tests/test_sanity.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ── Config surface ────────────────────────────────────────────────────────────

def test_runtime_config_exposes_new_keys():
    import config.runtime_config as rc
    cfg = rc.load()
    required = [
        "stock_trading_enabled", "options_trading_enabled", "intraday_enabled",
        "stock_max_daily_usd", "options_max_daily_usd",
        "options_take_profit_pct", "options_stop_loss_pct", "options_min_dte_exit",
        "options_target_dte_min", "options_target_dte_max", "options_target_delta",
        "covered_call_enabled", "covered_call_target_delta",
        "covered_call_target_dte_min", "covered_call_target_dte_max",
        "covered_call_auto_acquire",
    ]
    for k in required:
        assert k in cfg, f"runtime config missing {k}"


def test_default_trade_mode_is_options_only():
    import config.runtime_config as rc
    cfg = rc.load()
    assert cfg["options_trading_enabled"] is True
    assert cfg["stock_trading_enabled"] is False
    assert cfg["options_max_daily_usd"] == 1000.0


# ── OptionsPosition signed-qty math ───────────────────────────────────────────

def test_options_position_long_pnl():
    from core.position_tracker import OptionsPosition
    p = OptionsPosition(
        contract_symbol="O:SPY260620C00720000", underlying="SPY", side="call",
        strike=720, expiry="2026-06-20", qty=3, entry_premium=5.00,
        entry_date="2026-04-21", strategy="long_call",
    )
    assert not p.is_short
    assert p.cost_basis == 1500.0
    # +50% — qty=3, 3 × 100 × 2.50 = +$750
    assert p.unrealized_pnl(7.50) == pytest.approx(750.0)
    assert p.unrealized_pnl_pct(7.50) == pytest.approx(0.50)


def test_options_position_short_pnl():
    from core.position_tracker import OptionsPosition
    p = OptionsPosition(
        contract_symbol="O:SPY260620C00730000", underlying="SPY", side="call",
        strike=730, expiry="2026-06-20", qty=-2, entry_premium=5.00,
        entry_date="2026-04-21", strategy="short_call_covered",
    )
    assert p.is_short
    # For short, direction flips: current $2.50 = 50% decay = profit
    assert p.unrealized_pnl(2.50) == pytest.approx(500.0)
    assert p.unrealized_pnl_pct(2.50) == pytest.approx(0.50)
    # Premium doubled against us = SL
    assert p.unrealized_pnl(10.00) == pytest.approx(-1000.0)
    assert p.unrealized_pnl_pct(10.00) == pytest.approx(-1.0)


# ── Direction-aware exit logic ────────────────────────────────────────────────

def test_long_option_exits():
    from risk.risk_manager import RiskManager
    entry = 5.00
    # +50% hit → TP
    assert RiskManager.should_exit_option(entry, 7.50, 30)[0]
    # -50% hit → SL
    assert RiskManager.should_exit_option(entry, 2.50, 30)[0]
    # Mid-range, plenty of DTE → hold
    assert not RiskManager.should_exit_option(entry, 6.00, 30)[0]
    # DTE ≤ 7 → force close regardless of price
    assert RiskManager.should_exit_option(entry, 5.00, 5)[0]


def test_short_option_exits():
    from risk.risk_manager import RiskManager
    entry = 5.00
    # 50% decay = profit → TP
    assert RiskManager.should_exit_option(entry, 2.50, 30, is_short=True)[0]
    # Mid-range, plenty of DTE → hold
    assert not RiskManager.should_exit_option(entry, 4.00, 30, is_short=True)[0]
    # Premium doubled → SL
    assert RiskManager.should_exit_option(entry, 10.00, 30, is_short=True)[0]
    # DTE near expiry → force close
    assert RiskManager.should_exit_option(entry, 5.00, 5, is_short=True)[0]


# ── Daily-cap independence ────────────────────────────────────────────────────

def test_separate_stock_and_options_caps():
    """Hitting the options cap must NOT block stock trades, and vice-versa."""
    from core.position_tracker import BotState
    from risk.risk_manager import RiskManager
    from datetime import date

    state = BotState()
    state.daily_date = date.today().isoformat()
    state.options_daily_spent = 1000.0  # options cap fully consumed
    state.daily_spent = 0.0              # stock cap clean

    opt_verdict = RiskManager.check_options_daily_spend(state, 100.0)
    stk_verdict = RiskManager.check_daily_spend(state, 100.0)
    assert not opt_verdict.allowed, "Options cap should be full"
    assert stk_verdict.allowed, "Stock cap independence"


def test_options_cap_trims_partial():
    from core.position_tracker import BotState
    from risk.risk_manager import RiskManager
    from datetime import date
    state = BotState()
    state.daily_date = date.today().isoformat()
    state.options_daily_spent = 900.0   # $100 remaining
    verdict = RiskManager.check_options_daily_spend(state, 250.0)
    assert verdict.allowed
    assert verdict.adjusted_dollars == pytest.approx(100.0)


# ── Strategy selector decision tree ───────────────────────────────────────────

def test_strategy_selector_long_call_on_bullish():
    from executor.options_strategies import _strategy_for
    assert _strategy_for(0.75, "bull") == "long_call"
    assert _strategy_for(0.60, "neutral") == "long_call"
    assert _strategy_for(0.70, "euphoria") == "long_call"


def test_strategy_selector_long_put_on_bearish():
    from executor.options_strategies import _strategy_for
    assert _strategy_for(-0.70, "bear") == "long_put"
    assert _strategy_for(-0.90, "crash") == "long_put"


def test_strategy_selector_skips_weak_signals():
    from executor.options_strategies import _strategy_for
    # Score under threshold — no trade
    assert _strategy_for(0.40, "bull") is None
    assert _strategy_for(-0.50, "bear") is None
    # Conflicting direction (bullish score in bear regime) — no trade
    assert _strategy_for(0.80, "crash") is None


# ── Stable HMM schema ─────────────────────────────────────────────────────────

def test_hmm_schema_constant():
    from core.feature_engineering import HMM_FEATURE_COLUMNS
    assert len(HMM_FEATURE_COLUMNS) == 21
    # Guarantees no dupes
    assert len(set(HMM_FEATURE_COLUMNS)) == len(HMM_FEATURE_COLUMNS)


# ── BotState serialization round-trip with options ────────────────────────────

def test_bot_state_roundtrip_with_options(tmp_path, monkeypatch):
    import config.settings as cfg
    state_file = tmp_path / "bot_state.json"
    monkeypatch.setattr(cfg, "STATE_FILE", str(state_file))

    from core.position_tracker import BotState, OptionsPosition
    # re-import the module-level STATE_FILE binding
    import core.position_tracker as pt
    monkeypatch.setattr(pt, "STATE_FILE", str(state_file))

    state = BotState()
    state.options_positions["O:SPY260620C00720000"] = OptionsPosition(
        contract_symbol="O:SPY260620C00720000", underlying="SPY", side="call",
        strike=720, expiry="2026-06-20", qty=2, entry_premium=5.0,
        entry_date="2026-04-21", strategy="long_call",
    )
    state.options_daily_spent = 1000.0
    state.save()

    restored = BotState.load()
    assert "O:SPY260620C00720000" in restored.options_positions
    assert restored.options_positions["O:SPY260620C00720000"].qty == 2
    assert restored.options_daily_spent == 1000.0


# ── Polygon client configuration ──────────────────────────────────────────────

def test_polygon_key_configured():
    from core.polygon_client import is_configured
    assert is_configured(), "POLYGON_API_KEY must be in .env for live data"


# ── Multi-leg spread math ─────────────────────────────────────────────────────

def test_multi_leg_credit_spread_pnl():
    """Bull put credit spread: sell $700p / buy $695p for $1.00 credit on $5 wide.
    Max profit $1.00 (keep all credit), max loss $4.00 (width - credit)."""
    from core.position_tracker import OptionLeg, MultiLegPosition
    pos = MultiLegPosition(
        key="SPY_bull_put_credit_20260422",
        strategy="bull_put_credit",
        underlying="SPY",
        legs=[
            OptionLeg("O:SPY260620P00700000", "short", "put", 700, "2026-06-20", 2.00),
            OptionLeg("O:SPY260620P00695000", "long",  "put", 695, "2026-06-20", 1.00),
        ],
        qty=-2,          # short 2 spread units (credit received)
        net_entry=1.00,  # $1 credit per spread
        entry_date="2026-04-22",
    )
    assert pos.is_credit
    assert pos.width == 5.0
    assert pos.max_profit == pytest.approx(1.00)
    assert pos.max_loss == pytest.approx(4.00)
    assert pos.capital_at_risk == pytest.approx(800.0)  # 4 × 100 × 2

    # 50% decay: spread worth $0.50 → we captured $0.50 per spread × 2 = $100
    assert pos.unrealized_pnl(0.50) == pytest.approx(100.0)
    # Spread fully decayed to $0 → max profit = $1 × 100 × 2 = $200
    assert pos.unrealized_pnl(0.00) == pytest.approx(200.0)
    # Spread doubled against us to $2 → loss = -$1 × 100 × 2 = -$200
    assert pos.unrealized_pnl(2.00) == pytest.approx(-200.0)


def test_multi_leg_debit_spread_pnl():
    """Bull call debit: buy $700c / sell $705c for $2 debit on $5 wide."""
    from core.position_tracker import OptionLeg, MultiLegPosition
    pos = MultiLegPosition(
        key="SPY_bull_call_debit",
        strategy="bull_call_debit",
        underlying="SPY",
        legs=[
            OptionLeg("O:SPY260620C00700000", "long",  "call", 700, "2026-06-20", 7.00),
            OptionLeg("O:SPY260620C00705000", "short", "call", 705, "2026-06-20", 5.00),
        ],
        qty=3,           # long 3 spread units (debit paid)
        net_entry=2.00,  # $2 debit per spread
        entry_date="2026-04-22",
    )
    assert not pos.is_credit
    assert pos.max_loss == pytest.approx(2.00)
    assert pos.max_profit == pytest.approx(3.00)  # width - debit
    assert pos.capital_at_risk == pytest.approx(600.0)  # debit × 100 × 3

    # Spread at max ($5) → profit = (5 - 2) × 100 × 3 = $900
    assert pos.unrealized_pnl(5.00) == pytest.approx(900.0)
    # Spread worth half ($1) → loss = (1 - 2) × 100 × 3 = -$300
    assert pos.unrealized_pnl(1.00) == pytest.approx(-300.0)


def test_multi_leg_exit_credit_tp():
    from risk.risk_manager import RiskManager
    # Credit: entry $1, current $0.50 → 50% decay → TP
    should, reason = RiskManager.should_exit_multi_leg(1.00, 0.50, qty=-2, dte=30)
    assert should
    assert "TP" in reason


def test_multi_leg_exit_credit_sl():
    from risk.risk_manager import RiskManager
    # Credit: entry $1, current $2 → doubled → SL
    should, reason = RiskManager.should_exit_multi_leg(1.00, 2.00, qty=-2, dte=30)
    assert should
    assert "SL" in reason


def test_multi_leg_exit_debit_tp():
    from risk.risk_manager import RiskManager
    # Debit: entry $2, current $3 → +50% → TP
    should, reason = RiskManager.should_exit_multi_leg(2.00, 3.00, qty=3, dte=30)
    assert should
    assert "TP" in reason


def test_multi_leg_exit_hold_when_flat():
    from risk.risk_manager import RiskManager
    # Credit: entry $1, current $0.80 → only 20% decay, hold
    should, _ = RiskManager.should_exit_multi_leg(1.00, 0.80, qty=-2, dte=30)
    assert not should


def test_iron_condor_position():
    """4-leg iron condor: short wing on both sides, long wings further OTM."""
    from core.position_tracker import OptionLeg, MultiLegPosition
    pos = MultiLegPosition(
        key="SPY_iron_condor",
        strategy="iron_condor",
        underlying="SPY",
        legs=[
            OptionLeg("O:SPY260620P00690000", "short", "put",  690, "2026-06-20", 1.50),
            OptionLeg("O:SPY260620P00685000", "long",  "put",  685, "2026-06-20", 0.75),
            OptionLeg("O:SPY260620C00730000", "short", "call", 730, "2026-06-20", 1.80),
            OptionLeg("O:SPY260620C00735000", "long",  "call", 735, "2026-06-20", 0.95),
        ],
        qty=-1,
        net_entry=1.60,   # total credit from both sides
        entry_date="2026-04-22",
    )
    assert pos.is_credit
    assert pos.width == 5.0   # max of put wing (5) and call wing (5)
    assert pos.max_profit == pytest.approx(1.60)
    assert pos.max_loss == pytest.approx(3.40)


def test_bot_state_roundtrip_with_multi_leg(tmp_path, monkeypatch):
    import config.settings as cfg
    state_file = tmp_path / "bot_state.json"
    monkeypatch.setattr(cfg, "STATE_FILE", str(state_file))
    import core.position_tracker as pt
    monkeypatch.setattr(pt, "STATE_FILE", str(state_file))

    from core.position_tracker import BotState, OptionLeg, MultiLegPosition
    state = BotState()
    state.multi_leg_positions["SPY_bpc_20260422"] = MultiLegPosition(
        key="SPY_bpc_20260422",
        strategy="bull_put_credit",
        underlying="SPY",
        legs=[
            OptionLeg("O:SPY260620P00700000", "short", "put", 700, "2026-06-20", 2.0),
            OptionLeg("O:SPY260620P00695000", "long",  "put", 695, "2026-06-20", 1.0),
        ],
        qty=-1, net_entry=1.00, entry_date="2026-04-22",
    )
    state.save()

    restored = BotState.load()
    assert "SPY_bpc_20260422" in restored.multi_leg_positions
    rp = restored.multi_leg_positions["SPY_bpc_20260422"]
    assert rp.strategy == "bull_put_credit"
    assert len(rp.legs) == 2
    assert rp.legs[0].contract_symbol == "O:SPY260620P00700000"
    assert rp.qty == -1


def test_robinhood_occ_parser():
    """Verify OCC → (underlying, expiry, type, strike) parsing used by RobinhoodBroker."""
    from executor.robinhood_broker import _parse_occ
    parsed = _parse_occ("O:SPY260619C00720000")
    assert parsed == {
        "underlying": "SPY",
        "expiry": "2026-06-19",
        "contract_type": "call",
        "strike": 720.0,
    }
    parsed_put = _parse_occ("O:AAPL250117P00150000")
    assert parsed_put == {
        "underlying": "AAPL",
        "expiry": "2025-01-17",
        "contract_type": "put",
        "strike": 150.0,
    }
    assert _parse_occ("garbage") is None


def test_spread_config_exposed():
    import config.runtime_config as rc
    cfg = rc.load()
    for k in ("spreads_enabled", "iron_condor_enabled",
              "spread_target_short_delta", "spread_wing_width",
              "spread_take_profit_pct", "spread_stop_loss_pct",
              "iron_condor_short_delta", "iron_condor_wing_width"):
        assert k in cfg, f"missing runtime config key: {k}"
    assert cfg["spreads_enabled"] is False  # opt-in default
    assert cfg["iron_condor_enabled"] is False
