"""
Streamlit dashboard — run with: streamlit run dashboard/app.py
"""
import streamlit as st
import pandas as pd
import json
import os
from pathlib import Path
from datetime import datetime, timezone

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    TRADE_LOG_FILE, STATE_FILE, LOCKOUT_FILE, DASHBOARD_REFRESH_SECONDS,
    REGIME_NAMES,
)
import config.runtime_config as rc

HEARTBEAT_FILE = "bot_heartbeat.json"

st.set_page_config(page_title="Montrai", layout="wide", page_icon="📈")


def bot_status_pill():
    """Read bot_heartbeat.json and render a status pill at the top of the page."""
    cfg = rc.load()
    interval = cfg.get("signal_interval_minutes", 30)
    hb_path = Path(HEARTBEAT_FILE)
    if not hb_path.exists():
        return st.error("🔴 **Bot Stopped** — no heartbeat file found. Start with `python main.py`.")
    try:
        hb = json.loads(hb_path.read_text())
    except Exception:
        return st.error("🔴 **Bot Stopped** — heartbeat file unreadable.")

    # Staleness: allow up to 2× the configured interval plus a buffer.
    # Heartbeat writer emits tz-aware ISO strings — compare in UTC to avoid
    # naive-vs-aware subtraction errors.
    now = datetime.now(timezone.utc)
    try:
        raw_ts = hb["ts"].rstrip("Z")
        ts = datetime.fromisoformat(raw_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        ts = now
    age_s = (now - ts).total_seconds()
    stale_threshold_s = interval * 60 * 2 + 120
    mode = hb.get("mode", "unknown")
    regime_name = (hb.get("regime_name") or "").upper()

    if age_s > stale_threshold_s:
        return st.error(
            f"🔴 **Bot Stopped** — last heartbeat {int(age_s)}s ago (pid {hb.get('pid','?')}). "
            "Process may have crashed."
        )
    if mode == "halted":
        return st.error(f"🟠 **Bot Halted (lockout)** — pid {hb.get('pid','?')}, last beat {int(age_s)}s ago.")
    if mode == "sleeping" or regime_name == "MARKET_CLOSED":
        return st.warning(
            f"🟡 **Bot Sleeping — market closed** · pid {hb.get('pid','?')} · "
            f"cycles {hb.get('cycles',0)} · last beat {int(age_s)}s ago"
        )

    stock_on = "✅" if hb.get("stock_trading_enabled") else "⏸"
    opts_on = "✅" if hb.get("options_trading_enabled") else "⏸"
    intra = "✅" if hb.get("intraday_enabled") else "⏸"
    st.success(
        f"🟢 **Bot Running** · regime **{regime_name or '—'}** · "
        f"cycles {hb.get('cycles',0)} · last beat {int(age_s)}s ago · "
        f"stock {stock_on} · options {opts_on} · intraday {intra} · pid {hb.get('pid','?')}"
    )


bot_status_pill()

tab_live, tab_options, tab_settings, tab_data = st.tabs(
    ["Live Dashboard", "Options", "Settings", "Data Sources"]
)

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

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Peak Equity", f"${state_data.get('peak_equity', 0):,.2f}")
    opt_spent = state_data.get("options_daily_spent", 0)
    opt_cap = cfg.get("options_max_daily_usd", 1000.0)
    col2.metric("Options Spent Today", f"${opt_spent:,.2f}", f"Cap ${opt_cap:,.0f}")
    opt_pnl = state_data.get("options_realized_pnl", 0)
    stk_pnl = state_data.get("total_realized_pnl", 0)
    col3.metric("Realized P&L", f"${(opt_pnl + stk_pnl):+,.2f}",
                f"opt ${opt_pnl:+,.2f} · stk ${stk_pnl:+,.2f}")
    is_halved = state_data.get("is_halved", False)
    col4.metric("Position Sizing", "HALVED ⚠️" if is_halved else "Normal ✅")
    col5.metric("Cycles Run", f"{state_data.get('cycles', 0):,}",
                f"every {cfg['signal_interval_minutes']} min")

    st.subheader("Circuit Breaker Status")
    c1, c2, c3 = st.columns(3)
    opt_pct = min(opt_spent / max(opt_cap, 1), 1.0)
    c1.progress(opt_pct, text=f"Options Premium: ${opt_spent:.0f} / ${opt_cap:,.0f}")
    stock_spent = state_data.get("daily_spent", 0)
    stock_cap = cfg.get("stock_max_daily_usd", cfg.get("max_daily_spend_usd", 5000.0))
    stock_pct = min(stock_spent / max(stock_cap, 1), 1.0)
    c2.progress(stock_pct, text=f"Stock Notional: ${stock_spent:.0f} / ${stock_cap:,.0f}")
    peak = state_data.get("peak_equity", 1) or 1
    c3.caption(f"Drawdown lockout at {cfg['peak_drawdown_lockout_pct']:.0%} from peak ${peak:,.2f}")

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


# ── OPTIONS TAB ────────────────────────────────────────────────────────────────
with tab_options:
    st.title("🎯 Options Positions")
    cfg = rc.load()

    state_data = {}
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            state_data = json.load(f)

    opt_positions = state_data.get("options_positions", {})
    o1, o2, o3 = st.columns(3)
    o1.metric("Open Contracts", f"{sum(p['qty'] for p in opt_positions.values())}")
    o2.metric("Options Daily Premium", f"${state_data.get('options_daily_spent', 0):,.2f}",
              f"Cap ${cfg.get('options_max_daily_usd', 1000):,.0f}")
    o3.metric("Options Realized P&L", f"${state_data.get('options_realized_pnl', 0):+,.2f}")

    if opt_positions:
        rows = []
        for key, p in opt_positions.items():
            rows.append({
                "Contract": p["contract_symbol"],
                "Underlying": p["underlying"],
                "Strategy": p.get("strategy", "long_call"),
                "Side": p["side"],
                "Strike": p["strike"],
                "Expiry": p["expiry"],
                "Qty": p["qty"],
                "Entry Premium": f"${p['entry_premium']:.2f}",
                "Cost Basis": f"${p['entry_premium'] * p['qty'] * 100:.2f}",
                "Entry Date": p["entry_date"],
                "Regime": p.get("regime_at_entry", "—"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("No open options positions.")

    st.caption(
        f"Exit rules: take-profit +{cfg.get('options_take_profit_pct',0.5)*100:.0f}% · "
        f"stop-loss -{cfg.get('options_stop_loss_pct',0.5)*100:.0f}% · "
        f"DTE ≤ {cfg.get('options_min_dte_exit',7)} days"
    )
    st.caption(
        f"Entry rules: {cfg.get('options_target_dte_min',30)}-{cfg.get('options_target_dte_max',45)}DTE · "
        f"target Δ ≈ {cfg.get('options_target_delta',0.40):.2f} · "
        f"long calls in BULL/NEUTRAL · long puts in BEAR/CRASH · score threshold ±0.6"
    )

# ── SETTINGS ───────────────────────────────────────────────────────────────────
with tab_settings:
    st.title("⚙️ Settings")
    st.caption("Changes take effect on the next bot cycle — no restart needed.")

    cfg = rc.load()
    changed = False

    # ── Trade Mode ──────────────────────────────────────────────────────────────
    st.subheader("Trade Mode")
    st.caption("Independent toggles — enable options, stocks, or both. Options is the default primary path.")
    m1, m2, m3 = st.columns(3)
    with m1:
        opt_on = st.toggle("Options trading", value=bool(cfg.get("options_trading_enabled", True)))
        opt_cap = st.number_input(
            "Options max daily premium ($)",
            min_value=100.0, max_value=10000.0, step=50.0,
            value=float(cfg.get("options_max_daily_usd", 1000.0)),
            help="Per-day premium-outlay cap for all option buys.",
        )
    with m2:
        stock_on = st.toggle("Stock trading", value=bool(cfg.get("stock_trading_enabled", False)))
        stock_cap = st.number_input(
            "Stock max daily notional ($)",
            min_value=100.0, max_value=500000.0, step=100.0,
            value=float(cfg.get("stock_max_daily_usd", cfg.get("max_daily_spend_usd", 5000.0))),
            help="Per-day notional cap for stock buys. Can be set high; only applies when stock trading is enabled.",
        )
    with m3:
        intraday_on = st.toggle("Intraday trading", value=bool(cfg.get("intraday_enabled", False)),
                                help="Phase 1: flag only. Phase 2 wires Alpaca minute bars + EOD-flatten logic.")

    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        cc_on = st.toggle("Covered calls", value=bool(cfg.get("covered_call_enabled", False)),
                          help="Writes short calls against 100-share stock lots held at the broker.")
        cc_auto = st.toggle("Auto-acquire shares for CC",
                            value=bool(cfg.get("covered_call_auto_acquire", False)),
                            help="If no 100-share lot exists, buy the cheapest candidate that fits in stock_max_daily_usd. "
                                 "Strict cap enforcement — skips when nothing fits.")
    with cc2:
        cc_delta = st.slider("CC target Δ (OTM)", min_value=0.10, max_value=0.50, step=0.05,
                             value=float(cfg.get("covered_call_target_delta", 0.25)),
                             help="Lower Δ = further OTM = less assignment risk but lower premium.")
    with cc3:
        cc_dte_min = st.number_input("CC DTE min", min_value=7, max_value=90, step=1,
                                     value=int(cfg.get("covered_call_target_dte_min", 30)))
        cc_dte_max = st.number_input("CC DTE max", min_value=7, max_value=120, step=1,
                                     value=int(cfg.get("covered_call_target_dte_max", 45)))
    st.divider()

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
        # Legacy combined cap — left for back-compat, but the bot reads the
        # per-class caps above. Hidden behind an expander to avoid confusion.
        with st.expander("Legacy combined cap"):
            max_daily = st.number_input("Max daily spend (combined, legacy) ($)",
                min_value=50.0, max_value=500000.0, step=50.0,
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

    # ── Options behavior ───────────────────────────────────────────────────────
    st.subheader("Options Behavior")
    ob1, ob2, ob3 = st.columns(3)
    with ob1:
        opt_tp = st.slider("Take-profit (% of premium)",
            min_value=10, max_value=200, step=5,
            value=int(cfg.get("options_take_profit_pct", 0.50) * 100))
        opt_sl = st.slider("Stop-loss (% of premium)",
            min_value=10, max_value=100, step=5,
            value=int(cfg.get("options_stop_loss_pct", 0.50) * 100))
    with ob2:
        dte_min = st.number_input("Target DTE min (days)",
            min_value=1, max_value=120, step=1,
            value=int(cfg.get("options_target_dte_min", 30)))
        dte_max = st.number_input("Target DTE max (days)",
            min_value=1, max_value=180, step=1,
            value=int(cfg.get("options_target_dte_max", 45)))
    with ob3:
        min_dte_exit = st.number_input("Force-close when DTE ≤",
            min_value=0, max_value=30, step=1,
            value=int(cfg.get("options_min_dte_exit", 7)))
        target_delta = st.slider("Target absolute Δ", min_value=0.10, max_value=0.70, step=0.05,
            value=float(cfg.get("options_target_delta", 0.40)))

    st.divider()

    # ── Save button ─────────────────────────────────────────────────────────────
    if st.button("💾 Save All Settings", type="primary"):
        cfg.update({
            "options_trading_enabled": bool(opt_on),
            "stock_trading_enabled":   bool(stock_on),
            "intraday_enabled":        bool(intraday_on),
            "options_max_daily_usd":   float(opt_cap),
            "stock_max_daily_usd":     float(stock_cap),
            "max_daily_spend_usd":     max_daily,
            "max_position_size_pct":   max_pos_pct / 100,
            "max_open_positions":      int(max_positions),
            "daily_loss_halt_pct":     daily_loss_halt / 100,
            "peak_drawdown_lockout_pct": peak_dd / 100,
            "stop_loss_pct":           stop_loss / 100,
            "take_profit_pct":         take_profit / 100,
            "min_hold_days":           int(min_hold),
            "max_hold_days":           int(max_hold),
            "signal_interval_minutes": int(signal_interval),
            "extended_hours_enabled":  extended_hours,
            "regime_allocation":       {str(k): v for k, v in new_alloc.items()},
            "options_take_profit_pct": opt_tp / 100,
            "options_stop_loss_pct":   opt_sl / 100,
            "options_target_dte_min":  int(dte_min),
            "options_target_dte_max":  int(dte_max),
            "options_min_dte_exit":    int(min_dte_exit),
            "options_target_delta":    float(target_delta),
            "covered_call_enabled":        bool(cc_on),
            "covered_call_auto_acquire":   bool(cc_auto),
            "covered_call_target_delta":   float(cc_delta),
            "covered_call_target_dte_min": int(cc_dte_min),
            "covered_call_target_dte_max": int(cc_dte_max),
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
            "key": "financial_datasets_api_key",
            "name": "Financial Datasets",
            "cost": "Free tier (250 req/mo) | paid plans available",
            "signup": "https://financialdatasets.ai",
            "unlocks": "Fundamental overlay on every swing signal: P/E, profit margin, ROE, earnings beat streak, analyst EPS revisions, insider buying — 30% weight blended with technical score",
            "why": "Technicals tell you when; fundamentals tell you what. Filtering losers by bad fundamentals and boosting quality names meaningfully improves signal precision.",
            "priority": "🟢 Active — configure key to enable",
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
    import os
    if active_ds.get("financial_datasets_api_key") or os.getenv("FINANCIAL_DATASETS_API_KEY"):
        st.markdown("**Financial Datasets (active):** `pe_ratio`, `profit_margin`, `roe`, `debt_equity`, "
                    "`earnings_beats`, `eps_revision`, `insider_score` — blended 30% into swing signal")
    if active_ds.get("unusual_whales_api_key"):
        st.markdown("**Unusual Whales (active):** `options_flow_score`")

import time
time.sleep(DASHBOARD_REFRESH_SECONDS)
st.rerun()
