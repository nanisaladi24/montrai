"""
Streamlit dashboard — run with: streamlit run dashboard/app.py
"""
import streamlit as st
import pandas as pd
import json
import os
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    TRADE_LOG_FILE, STATE_FILE, LOCKOUT_FILE, DASHBOARD_REFRESH_SECONDS,
    REGIME_NAMES,
)
import config.runtime_config as rc

st.set_page_config(page_title="Montrai", layout="wide", page_icon="📈")

tab_live, tab_settings, tab_data = st.tabs(["Live Dashboard", "Settings", "Data Sources"])

# ── LIVE DASHBOARD ─────────────────────────────────────────────────────────────
with tab_live:
    st.title("📈 Montrai — Live Dashboard")
    st.caption(f"Auto-refreshes every {DASHBOARD_REFRESH_SECONDS}s — last updated: {datetime.now().strftime('%H:%M:%S')}")

    if Path(LOCKOUT_FILE).exists():
        with open(LOCKOUT_FILE) as f:
            msg = f.read()
        st.error(f"🚨 BOT LOCKED OUT\n\n{msg}")
        st.stop()

    state_data = {}
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            state_data = json.load(f)

    cfg = rc.load()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Peak Equity", f"${state_data.get('peak_equity', 0):,.2f}")
    col2.metric("Daily Spent", f"${state_data.get('daily_spent', 0):,.2f}",
                f"Max ${cfg['max_daily_spend_usd']}")
    col3.metric("Realized P&L", f"${state_data.get('total_realized_pnl', 0):+,.2f}")
    is_halved = state_data.get("is_halved", False)
    col4.metric("Position Sizing", "HALVED ⚠️" if is_halved else "Normal ✅")

    st.subheader("Circuit Breaker Status")
    c1, c2 = st.columns(2)
    daily_pct = min(state_data.get("daily_spent", 0) / max(cfg["max_daily_spend_usd"], 1), 1.0)
    c1.progress(daily_pct, text=f"Daily Spend: ${state_data.get('daily_spent', 0):.0f} / ${cfg['max_daily_spend_usd']}")
    peak = state_data.get("peak_equity", 1) or 1
    c2.caption(f"Drawdown lockout triggers at {cfg['peak_drawdown_lockout_pct']:.0%} from peak ${peak:,.2f}")

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

    st.subheader("Trade History")
    if Path(TRADE_LOG_FILE).exists():
        trades_df = pd.read_csv(TRADE_LOG_FILE)
        if not trades_df.empty:
            trades_df["timestamp"] = pd.to_datetime(trades_df["timestamp"])
            trades_df = trades_df.sort_values("timestamp", ascending=False)
            buys = trades_df[trades_df["side"] == "BUY"]["value_usd"].sum()
            st.metric("Total Bought", f"${buys:,.2f}")
            st.dataframe(trades_df.head(50), use_container_width=True)
    else:
        st.info("No trade history yet.")

    with st.expander("Regime Legend"):
        alloc = rc.get_regime_allocation()
        for k, v in REGIME_NAMES.items():
            st.write(f"**{k} — {v.upper()}**: allocation multiplier = {alloc.get(k, '?')}")

# ── SETTINGS ───────────────────────────────────────────────────────────────────
with tab_settings:
    st.title("⚙️ Settings")
    st.caption("Changes take effect on the next bot cycle — no restart needed.")

    cfg = rc.load()
    changed = False

    # ── Watchlist ───────────────────────────────────────────────────────────────
    st.subheader("Watchlist")
    current_symbols = cfg.get("watchlist", [])

    col_add, col_spacer = st.columns([2, 3])
    with col_add:
        new_sym = st.text_input("Add symbol", placeholder="e.g. NFLX", key="new_sym").upper().strip()
        if st.button("Add") and new_sym:
            if new_sym not in current_symbols:
                current_symbols = current_symbols + [new_sym]
                cfg["watchlist"] = current_symbols
                rc.save(cfg)
                st.success(f"{new_sym} added.")
                st.rerun()
            else:
                st.warning(f"{new_sym} already in watchlist.")

    if current_symbols:
        cols = st.columns(6)
        remove = None
        for i, sym in enumerate(current_symbols):
            with cols[i % 6]:
                if st.button(f"✕ {sym}", key=f"rm_{sym}"):
                    remove = sym
        if remove:
            cfg["watchlist"] = [s for s in current_symbols if s != remove]
            rc.save(cfg)
            st.success(f"{remove} removed.")
            st.rerun()
    else:
        st.info("Watchlist is empty.")

    st.divider()

    # ── Risk limits ─────────────────────────────────────────────────────────────
    st.subheader("Risk Limits")
    r1, r2, r3 = st.columns(3)
    with r1:
        max_daily = st.number_input("Max daily spend ($)",
            min_value=50.0, max_value=50000.0, step=50.0,
            value=float(cfg["max_daily_spend_usd"]))
        stop_loss = st.slider("Stop loss (%)",
            min_value=1, max_value=20, step=1,
            value=int(cfg["stop_loss_pct"] * 100))
    with r2:
        max_pos_pct = st.slider("Max position size (% of portfolio)",
            min_value=1, max_value=50, step=1,
            value=int(cfg["max_position_size_pct"] * 100))
        take_profit = st.slider("Take profit (%)",
            min_value=2, max_value=50, step=1,
            value=int(cfg["take_profit_pct"] * 100))
    with r3:
        max_positions = st.number_input("Max open positions",
            min_value=1, max_value=20, step=1,
            value=int(cfg["max_open_positions"]))
        peak_dd = st.slider("Peak drawdown lockout (%)",
            min_value=5, max_value=40, step=1,
            value=int(cfg["peak_drawdown_lockout_pct"] * 100))

    st.divider()

    # ── Trading behaviour ───────────────────────────────────────────────────────
    st.subheader("Trading Behaviour")
    t1, t2, t3 = st.columns(3)
    with t1:
        signal_interval = st.number_input("Signal interval (minutes)",
            min_value=5, max_value=240, step=5,
            value=int(cfg["signal_interval_minutes"]))
    with t2:
        min_hold = st.number_input("Min hold days",
            min_value=1, max_value=30, step=1,
            value=int(cfg["min_hold_days"]))
        max_hold = st.number_input("Max hold days",
            min_value=1, max_value=60, step=1,
            value=int(cfg["max_hold_days"]))
    with t3:
        daily_loss_halt = st.slider("Daily loss halt (%)",
            min_value=1, max_value=10, step=1,
            value=int(cfg["daily_loss_halt_pct"] * 100))
        extended_hours = st.checkbox("Extended hours trading",
            value=bool(cfg["extended_hours_enabled"]))

    st.divider()

    # ── Regime allocation multipliers ───────────────────────────────────────────
    st.subheader("Regime Allocation Multipliers")
    st.caption("Scales position size in each market regime (0 = no trades, 1 = full size).")
    alloc = rc.get_regime_allocation()
    ra_cols = st.columns(5)
    new_alloc = {}
    for i, (regime_id, name) in enumerate(REGIME_NAMES.items()):
        with ra_cols[i]:
            new_alloc[regime_id] = st.slider(
                name.capitalize(),
                min_value=0.0, max_value=1.5, step=0.1,
                value=float(alloc.get(regime_id, 0.5)),
                key=f"alloc_{regime_id}",
            )

    st.divider()

    # ── Save button ─────────────────────────────────────────────────────────────
    if st.button("💾 Save All Settings", type="primary"):
        cfg.update({
            "max_daily_spend_usd": max_daily,
            "max_position_size_pct": max_pos_pct / 100,
            "max_open_positions": int(max_positions),
            "daily_loss_halt_pct": daily_loss_halt / 100,
            "peak_drawdown_lockout_pct": peak_dd / 100,
            "stop_loss_pct": stop_loss / 100,
            "take_profit_pct": take_profit / 100,
            "min_hold_days": int(min_hold),
            "max_hold_days": int(max_hold),
            "signal_interval_minutes": int(signal_interval),
            "extended_hours_enabled": extended_hours,
            "regime_allocation": {str(k): v for k, v in new_alloc.items()},
        })
        rc.save(cfg)
        st.success("Settings saved. Bot will pick them up on the next cycle.")

# ── DATA SOURCES ───────────────────────────────────────────────────────────────
with tab_data:
    st.title("🔌 Data Sources")
    st.caption("API keys are saved to config/runtime.json. Keys activate additional HMM features automatically.")

    cfg = rc.load()
    ds = cfg.get("data_sources", {})

    SOURCES = [
        {
            "key": "fred_api_key",
            "name": "FRED (Federal Reserve)",
            "cost": "Free",
            "signup": "https://fred.stlouisfed.org/docs/api/api_key.html",
            "unlocks": "`yield_curve_spread`, `fed_funds_rate`, `hy_credit_spread`",
            "why": "Yield curve inversion is the single best recession predictor. Free — do this first.",
            "priority": "🟢 Do today",
        },
        {
            "key": "polygon_api_key",
            "name": "Polygon.io",
            "cost": "Free tier | $29/mo real-time | $79/mo + options",
            "signup": "https://polygon.io/",
            "unlocks": "Real-time quotes, options chain data, tick-level data for intraday",
            "why": "yfinance has 15-min delays on some feeds. Polygon fixes that. Essential for intraday strategies.",
            "priority": "🟡 When adding intraday/options",
        },
        {
            "key": "unusual_whales_api_key",
            "name": "Unusual Whales",
            "cost": "~$50/mo",
            "signup": "https://unusualwhales.com/",
            "unlocks": "`options_flow_score` per symbol — large unusual options bets, dark pool prints",
            "why": "Large options flow often precedes moves by 1–3 days. Best for individual stock signals.",
            "priority": "🟠 Add after bot is profitable",
        },
        {
            "key": "nasdaq_data_link_api_key",
            "name": "Nasdaq Data Link (Quandl)",
            "cost": "Free tier | paid bundles $50–$500+/mo",
            "signup": "https://data.nasdaq.com/",
            "unlocks": "`short_interest_ratio`, `cot_net_positioning` — short interest, COT futures positioning",
            "why": "Short interest is a useful contrarian signal. COT data adds macro regime context.",
            "priority": "🔵 Nice to have",
        },
    ]

    updated_ds = dict(ds)
    any_changed = False

    for source in SOURCES:
        with st.expander(f"{source['priority']}  **{source['name']}** — {source['cost']}"):
            col_info, col_key = st.columns([2, 2])
            with col_info:
                st.markdown(f"**Unlocks:** {source['unlocks']}")
                st.markdown(f"**Why:** {source['why']}")
                st.markdown(f"[Sign up →]({source['signup']})")
            with col_key:
                current_val = ds.get(source["key"], "")
                is_set = bool(current_val)
                st.markdown("**Status:** " + ("✅ Configured" if is_set else "⬜ Not set"))
                new_val = st.text_input(
                    "API Key",
                    value=current_val,
                    type="password",
                    key=f"ds_{source['key']}",
                    label_visibility="collapsed",
                    placeholder="Paste API key here",
                )
                if new_val != current_val:
                    updated_ds[source["key"]] = new_val
                    any_changed = True

    if st.button("💾 Save API Keys", type="primary"):
        cfg["data_sources"] = updated_ds
        rc.save(cfg)
        st.success("API keys saved. Active data sources will be used on the next bot cycle.")
        st.rerun()

    st.divider()
    st.subheader("Currently Active Features")
    active_cfg = rc.load()
    active_ds = active_cfg.get("data_sources", {})
    base_features = [
        "ret_1d/5d/20d/60d", "realised_vol", "atr_pct", "bb_position", "vol_ratio",
        "vix_rank", "vix_term_ratio", "vvix_rank", "vvix_vix_ratio",
        "tlt_ret", "dxy_ret", "hyg_ret", "smh_spy_rs",
        "gex_per_spot", "gamma_flip_dist",
    ]
    st.markdown("**Always on (yfinance + BS calc):** " + ", ".join(f"`{f}`" for f in base_features))
    if active_ds.get("fred_api_key"):
        st.markdown("**FRED (active):** `yield_curve_spread`, `fed_funds_rate`, `hy_credit_spread`")
    if active_ds.get("unusual_whales_api_key"):
        st.markdown("**Unusual Whales (active):** `options_flow_score`")

import time
time.sleep(DASHBOARD_REFRESH_SECONDS)
st.rerun()
