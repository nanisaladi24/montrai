"""
Streamlit dashboard — run with: streamlit run dashboard/app.py
"""
import streamlit as st
import pandas as pd
import json
import os
from pathlib import Path
from datetime import datetime

# Add parent to path so config/core imports work
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    TRADE_LOG_FILE, STATE_FILE, LOCKOUT_FILE, DASHBOARD_REFRESH_SECONDS,
    MAX_DAILY_SPEND_USD, PEAK_DRAWDOWN_LOCKOUT_PCT, DAILY_LOSS_HALT_PCT, REGIME_NAMES
)

st.set_page_config(page_title="AI Swing Trader", layout="wide", page_icon="📈")
st.title("📈 AI Swing Trader — Live Dashboard")

# Auto-refresh
st.write(f"*Auto-refreshes every {DASHBOARD_REFRESH_SECONDS}s — last updated: {datetime.now().strftime('%H:%M:%S')}*")

# ── Lockout Banner ─────────────────────────────────────────────────────────────
if Path(LOCKOUT_FILE).exists():
    with open(LOCKOUT_FILE) as f:
        msg = f.read()
    st.error(f"🚨 BOT LOCKED OUT\n\n{msg}")
    st.stop()

# ── Bot State ──────────────────────────────────────────────────────────────────
state_data = {}
if Path(STATE_FILE).exists():
    with open(STATE_FILE) as f:
        state_data = json.load(f)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Peak Equity", f"${state_data.get('peak_equity', 0):,.2f}")
col2.metric("Daily Spent", f"${state_data.get('daily_spent', 0):,.2f}", f"Max ${MAX_DAILY_SPEND_USD}")
col3.metric("Realized P&L", f"${state_data.get('total_realized_pnl', 0):+,.2f}")
is_halved = state_data.get("is_halved", False)
col4.metric("Position Sizing", "HALVED ⚠️" if is_halved else "Normal ✅")

# ── Safety Limits Gauges ───────────────────────────────────────────────────────
st.subheader("Circuit Breaker Status")
c1, c2 = st.columns(2)
daily_pct = min(state_data.get("daily_spent", 0) / MAX_DAILY_SPEND_USD, 1.0)
c1.progress(daily_pct, text=f"Daily Spend: ${state_data.get('daily_spent', 0):.0f} / ${MAX_DAILY_SPEND_USD}")
peak = state_data.get("peak_equity", 1)
# approximate current equity from positions
c2.caption(f"Drawdown lockout triggers at {PEAK_DRAWDOWN_LOCKOUT_PCT:.0%} from peak ${peak:,.2f}")

# ── Open Positions ─────────────────────────────────────────────────────────────
st.subheader("Open Positions")
positions = state_data.get("positions", {})
if positions:
    rows = []
    for sym, p in positions.items():
        rows.append({
            "Symbol": sym,
            "Qty": p["quantity"],
            "Entry": f"${p['entry_price']:.2f}",
            "Stop Loss": f"${p['stop_loss']:.2f}",
            "Take Profit": f"${p['take_profit']:.2f}",
            "Entry Date": p["entry_date"],
            "Regime": p.get("regime_at_entry", "—"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
else:
    st.info("No open positions.")

# ── Trade History ──────────────────────────────────────────────────────────────
st.subheader("Trade History")
if Path(TRADE_LOG_FILE).exists():
    trades_df = pd.read_csv(TRADE_LOG_FILE)
    if not trades_df.empty:
        trades_df["timestamp"] = pd.to_datetime(trades_df["timestamp"])
        trades_df = trades_df.sort_values("timestamp", ascending=False)

        # P&L cumulative chart
        buys = trades_df[trades_df["side"] == "BUY"]["value_usd"].sum()
        sells = trades_df[trades_df["side"] == "SELL"]["value_usd"].sum()
        st.metric("Total Bought", f"${buys:,.2f}")
        st.dataframe(trades_df.head(50), use_container_width=True)
else:
    st.info("No trade history yet.")

# ── Regime Legend ──────────────────────────────────────────────────────────────
with st.expander("Regime Legend"):
    for k, v in REGIME_NAMES.items():
        from config.settings import REGIME_ALLOCATION
        st.write(f"**{k} — {v.upper()}**: allocation multiplier = {REGIME_ALLOCATION.get(k, '?')}")

# Streamlit auto-rerun
import time
time.sleep(DASHBOARD_REFRESH_SECONDS)
st.rerun()
