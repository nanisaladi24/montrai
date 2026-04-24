"""
Streamlit dashboard — run with: streamlit run dashboard/app.py
"""
import streamlit as st
import pandas as pd
import json
import os
import signal
import subprocess
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


def trading_mode_banner():
    """Always-visible indicator of paper vs live trading at top of dashboard.
    Pulls from the heartbeat first (authoritative at runtime), falls back to
    settings.py if the bot hasn't written one yet."""
    broker = "?"
    mode = "?"
    is_paper = True
    try:
        if Path(HEARTBEAT_FILE).exists():
            hb = json.loads(Path(HEARTBEAT_FILE).read_text())
            broker = hb.get("broker", "?")
            mode = hb.get("trading_mode", "?")
            is_paper = bool(hb.get("alpaca_paper", True)) if broker == "alpaca" else (mode != "live")
        else:
            import config.settings as _cfg
            broker = _cfg.BROKER
            mode = _cfg.TRADING_MODE
            is_paper = getattr(_cfg, "ALPACA_PAPER", True) if broker == "alpaca" else (mode != "live")
    except Exception:
        pass

    broker_label = broker.upper()
    if is_paper or mode == "paper":
        st.info(
            f"📄 **PAPER TRADING** · broker: **{broker_label}** · simulated fills, no real money at risk"
        )
    else:
        st.error(
            f"🚨 **LIVE TRADING** · broker: **{broker_label}** · real money, real fills — "
            "check positions + caps before enabling any new strategy"
        )


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def bot_status_pill():
    """Read bot_heartbeat.json and render a status pill at the top of the page."""
    cfg = rc.load()
    interval = cfg.get("signal_interval_minutes", 30)
    hb_path = Path(HEARTBEAT_FILE)
    if not hb_path.exists():
        return st.error("🔴 **Bot Stopped** — no heartbeat file found. Start with `python main.py` or use the Start button below.")
    try:
        hb = json.loads(hb_path.read_text())
    except Exception:
        return st.error("🔴 **Bot Stopped** — heartbeat file unreadable.")

    pid = hb.get("pid")
    # Authoritative: if the PID in the heartbeat is not alive, the bot is gone.
    # Beats relying on a long staleness threshold (which made killed bots look
    # asleep for up to an hour).
    if not _pid_alive(pid):
        return st.error(
            f"🔴 **Bot Stopped** — pid {pid} is not running. Stale heartbeat will be cleared on next start."
        )

    # Staleness: the bot is alive but hasn't written a heartbeat recently.
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
            f"🔴 **Bot Unresponsive** — pid {pid} alive but last heartbeat {int(age_s)}s ago. "
            "Process may be stuck."
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


def bot_control_panel():
    """Start/Stop buttons next to the status pill. Start launches main.py
    detached so it survives dashboard reloads; Stop sends SIGTERM to the PID
    in the heartbeat (main.py's signal handler cleans up the heartbeat)."""
    repo_root = Path(__file__).parent.parent
    hb_path = repo_root / HEARTBEAT_FILE
    running_pid = None
    if hb_path.exists():
        try:
            running_pid = int(json.loads(hb_path.read_text()).get("pid") or 0)
        except Exception:
            running_pid = None
    is_running = bool(running_pid and _pid_alive(running_pid))

    c1, c2, _ = st.columns([1, 1, 6])
    if c1.button("▶ Start bot", disabled=is_running, help="Launch python main.py in the background."):
        venv_python = repo_root / ".venv" / "bin" / "python"
        py = str(venv_python) if venv_python.exists() else "python"
        log_dir = repo_root / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = open(log_dir / "bot.log", "a")
        try:
            proc = subprocess.Popen(
                [py, "main.py"],
                cwd=str(repo_root),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            st.success(f"Bot started (pid {proc.pid}). Log → logs/bot.log")
        except Exception as e:
            st.error(f"Failed to start bot: {e}")
        st.rerun()
    if c2.button("⏹ Stop bot", disabled=not is_running, help="Send SIGTERM to the running bot."):
        try:
            os.kill(running_pid, signal.SIGTERM)
            st.success(f"Stop signal sent to pid {running_pid}.")
        except ProcessLookupError:
            st.warning(f"pid {running_pid} already gone — clearing stale heartbeat.")
            try:
                hb_path.unlink()
            except FileNotFoundError:
                pass
        except Exception as e:
            st.error(f"Failed to stop bot: {e}")
        st.rerun()


trading_mode_banner()
bot_status_pill()
bot_control_panel()

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

    def _compute_capital_deployed(sd: dict) -> float:
        """Real capital in play right now — sum cost-basis / capital-at-risk
        across all open positions. More useful than a daily-flow counter
        because it reflects what's actually tied up at the broker."""
        total = 0.0
        for mlp in sd.get("multi_leg_positions", {}).values():
            qty = abs(mlp.get("qty", 0))
            net_entry = float(mlp.get("net_entry", 0))
            legs = mlp.get("legs", [])
            # Recompute width from legs so this reflects actual structure
            calls = [l for l in legs if l.get("contract_type") == "call"]
            puts  = [l for l in legs if l.get("contract_type") == "put"]
            call_w = abs(calls[0]["strike"] - calls[1]["strike"]) if len(calls) == 2 else 0.0
            put_w  = abs(puts[0]["strike"] - puts[1]["strike"]) if len(puts) == 2 else 0.0
            width = max(call_w, put_w) if (call_w or put_w) else 0.0
            if mlp.get("qty", 0) < 0:
                # Credit spread / iron condor: capital at risk = (width − net_credit) × qty × 100
                per_unit = max(width - net_entry, 0.0)
            else:
                # Debit spread: cost basis = net_entry × qty × 100
                per_unit = net_entry
            total += per_unit * qty * 100
        for op in sd.get("options_positions", {}).values():
            qty = abs(op.get("qty", 0))
            entry = float(op.get("entry_premium", 0))
            total += entry * qty * 100
        return total

    capital_deployed = _compute_capital_deployed(state_data)

    col1, col3, col4, col5 = st.columns(4)
    col1.metric("Peak Equity", f"${state_data.get('peak_equity', 0):,.2f}")
    # Keep these vars around — circuit-breaker section below still references them
    opt_spent = state_data.get("options_daily_spent", 0)
    opt_cap = cfg.get("options_max_daily_usd", 1000.0)
    opt_pnl = state_data.get("options_realized_pnl", 0)
    stk_pnl = state_data.get("total_realized_pnl", 0)
    peak = float(state_data.get("peak_equity", 0) or 0)

    # Lifetime account P&L from Alpaca's portfolio history (deposit-adjusted,
    # survives state wipes — authoritative). Falls back to state counters if
    # broker is unreachable.
    @st.cache_data(ttl=60)
    def _lifetime_account_pnl():
        try:
            from executor.order_executor import get_portfolio_history, get_portfolio_value
            h = get_portfolio_history(period="all", timeframe="1D")
            base = float(h.get("base_value") or 0)
            eq = float(get_portfolio_value() or 0)
            if base > 0 and eq > 0:
                return eq - base, (eq - base) / base * 100, base, eq
        except Exception:
            pass
        return None, None, 0.0, 0.0

    lt_pnl, lt_pct, lt_base, lt_eq = _lifetime_account_pnl()
    if lt_pnl is not None:
        col3.metric(
            "Account P&L (all-time)",
            f"${lt_pnl:+,.2f}",
            f"{lt_pct:+.2f}%",
        )
        col3.caption(
            f"since deposit ${lt_base:,.0f} · includes open positions · "
            f"state realized: opt ${opt_pnl:+,.0f} / stk ${stk_pnl:+,.0f}"
        )
    else:
        # Fallback to state counters if broker fetch fails
        total_pnl = opt_pnl + stk_pnl
        pnl_pct = (total_pnl / peak * 100) if peak > 0 else 0.0
        col3.metric("Realized P&L (state)", f"${total_pnl:+,.2f}", f"{pnl_pct:+.2f}%")
        col3.caption(f"opt ${opt_pnl:+,.2f} · stk ${stk_pnl:+,.2f} · broker offline")
    is_halved = state_data.get("is_halved", False)
    col4.metric("Position Sizing", "HALVED ⚠️" if is_halved else "Normal ✅")

    # Cycle cadence — mirror the logic in main_loop so the caption reflects
    # the *active* interval (not always the swing fallback).
    try:
        from core.market_data import is_market_open as _is_mkt_open
        market_open_now = _is_mkt_open()
    except Exception:
        market_open_now = False
    intraday_on = bool(cfg.get("intraday_enabled", False))
    options_on  = bool(cfg.get("options_trading_enabled", True))
    stocks_on   = bool(cfg.get("stock_trading_enabled", False))
    swing_min    = int(cfg.get("signal_interval_minutes", 30))
    intraday_min = int(cfg.get("intraday_scan_interval_min", swing_min))

    if intraday_on and market_open_now:
        active_min = intraday_min
        mode_desc = "intraday · market open"
    elif not market_open_now:
        active_min = swing_min
        mode_desc = "market closed"
    else:
        active_min = swing_min
        mode_desc = "swing"
    # What's actually being traded right now
    lanes = []
    if options_on: lanes.append("options")
    if stocks_on:  lanes.append("stocks")
    if intraday_on and market_open_now: lanes.append("ORB")
    lanes_str = "+".join(lanes) or "idle"

    col5.metric("Cycles Run", f"{state_data.get('cycles', 0):,}",
                f"every {active_min} min")
    col5.caption(f"{mode_desc} · {lanes_str}")

    # Broker sync freshness — so user knows how current this view is
    last_sync = state_data.get("last_broker_sync_at", "")
    if last_sync:
        try:
            dt = datetime.fromisoformat(last_sync)
            if dt.tzinfo is None:
                from datetime import timezone as _tz
                dt = dt.replace(tzinfo=_tz.utc)
            age_sec = (datetime.now(dt.tzinfo) - dt).total_seconds()
            age_str = f"{int(age_sec//60)}m {int(age_sec%60)}s ago" if age_sec < 3600 else f"{age_sec/3600:.1f}h ago"
            st.caption(f"📡 Last Alpaca sync: {dt.astimezone().strftime('%H:%M:%S %Z')} ({age_str})")
        except Exception:
            st.caption(f"📡 Last Alpaca sync: {last_sync}")
    else:
        st.caption("📡 Last Alpaca sync: never")

    # ── Performance graphs (fetched from Alpaca portfolio history) ────────────
    st.subheader("Performance")

    _PERIOD_MAP = {
        "1D":  {"period": "1D", "timeframe": "5Min"},
        "1W":  {"period": "1W", "timeframe": "1H"},
        "1M":  {"period": "1M", "timeframe": "1D"},
        "3M":  {"period": "3M", "timeframe": "1D"},
        "6M":  {"period": "6M", "timeframe": "1D"},
        "1Y":  {"period": "1A", "timeframe": "1D"},
        "5Y":  {"period": "5A", "timeframe": "1D"},
    }

    @st.cache_data(ttl=60)
    def _fetch_history(period_label: str) -> dict:
        from executor.order_executor import get_portfolio_history
        params = _PERIOD_MAP[period_label]
        return get_portfolio_history(period=params["period"], timeframe=params["timeframe"])

    perf_tabs = st.tabs(list(_PERIOD_MAP.keys()))
    for tab, label in zip(perf_tabs, _PERIOD_MAP.keys()):
        with tab:
            h = _fetch_history(label)
            ts = h.get("timestamps") or []
            eq = h.get("equity") or []
            pl = h.get("profit_loss") or []
            pl_pct = h.get("profit_loss_pct") or []
            if not ts or not eq:
                st.info(f"No portfolio history available for {label} (broker returned no points — paper account may be too new).")
                continue
            df = pd.DataFrame({
                "time": pd.to_datetime(ts, unit="s", utc=True).tz_convert("US/Eastern"),
                "equity": eq,
                "pnl": pl,
                "pnl_pct": [p * 100 for p in pl_pct],  # Alpaca returns as decimal
            }).set_index("time")
            # Summary metrics for this period
            start_eq = eq[0] or h.get("base_value") or 0
            end_eq = eq[-1]
            period_pnl = end_eq - start_eq
            period_pct = (period_pnl / start_eq * 100) if start_eq else 0.0
            m1, m2, m3 = st.columns(3)
            m1.metric(f"{label} Equity", f"${end_eq:,.2f}")
            m2.metric(f"{label} Return", f"${period_pnl:+,.2f}", f"{period_pct:+.2f}%")
            m3.metric("Data points", f"{len(eq):,}")
            st.line_chart(df[["equity"]], height=260)
            with st.expander("Show P&L curve (absolute $)"):
                st.line_chart(df[["pnl"]], height=220)

    st.subheader("Circuit Breakers")
    st.caption("Hard-coded safety invariants. Each row shows the current value, threshold, and whether the breaker is armed, warning, or tripped.")

    # Pull live portfolio value (broker truth) for drawdown math
    @st.cache_data(ttl=30)
    def _live_equity() -> float:
        try:
            from executor.order_executor import get_portfolio_value
            return float(get_portfolio_value() or 0)
        except Exception:
            return 0.0
    live_eq = _live_equity()

    peak = float(state_data.get("peak_equity", 0) or 0)
    stock_spent_now = float(state_data.get("daily_spent", 0) or 0)
    stock_cap = float(cfg.get("stock_max_daily_usd", cfg.get("max_daily_spend_usd", 5000.0)))
    opt_spent_now = float(opt_spent)
    opt_cap_now = float(opt_cap)
    dd_lockout_pct = float(cfg.get("peak_drawdown_lockout_pct", 0.10))
    daily_halt_pct = float(cfg.get("daily_loss_halt_pct", 0.02))
    max_open = int(cfg.get("max_open_positions", 8))
    position_count = (
        len(state_data.get("positions", {})) +
        len(state_data.get("options_positions", {})) +
        len(state_data.get("multi_leg_positions", {}))
    )
    # Deployed capital — computed live, stacked with the daily caps
    opt_deployed_cap   = float(cfg.get("options_max_deployed_usd", 10000.0))
    stock_deployed_cap = float(cfg.get("stock_max_deployed_usd", 50000.0))
    opt_deployed   = capital_deployed  # already computed above
    stock_deployed = sum(
        abs(float(p.get("entry_price", 0))) * abs(float(p.get("quantity", 0)))
        for p in state_data.get("positions", {}).values()
    )

    # Lockout file presence — most-severe breaker
    from pathlib import Path as _P
    lockout_exists = _P("LOCKOUT").exists()

    # Drawdown (live)
    dd_pct_live = ((peak - live_eq) / peak * 100) if (peak > 0 and live_eq > 0) else 0.0
    dd_frac_of_limit = (dd_pct_live / 100) / dd_lockout_pct if dd_lockout_pct > 0 else 0.0

    # Intraday loss vs halving threshold (approximate — uses peak as proxy for start-of-day)
    sod_loss_pct = dd_pct_live  # same proxy — the bot uses SOD equity at cycle start internally
    halt_frac = (sod_loss_pct / 100) / daily_halt_pct if daily_halt_pct > 0 else 0.0

    # Fractions for progress bars
    opt_frac   = min(opt_spent_now / max(opt_cap_now, 1), 1.0)
    stock_frac = min(stock_spent_now / max(stock_cap, 1), 1.0)
    pos_frac   = min(position_count / max(max_open, 1), 1.0)

    def _status_emoji(frac: float, tripped: bool = False) -> str:
        if tripped: return "🔴 TRIPPED"
        if frac >= 0.9: return "🟠 near limit"
        if frac >= 0.5: return "🟡 elevated"
        return "🟢 armed"

    # ── Row 1: most-severe breakers ──────────────────────────────────────────
    cb1, cb2, cb3 = st.columns(3)

    with cb1:
        if lockout_exists:
            st.error("**LOCKOUT file present** 🔴")
            st.caption("Bot halted. Remove ./LOCKOUT to resume after investigating.")
        else:
            st.success("**Lockout** 🟢")
            st.caption("No LOCKOUT file — bot is permitted to trade.")

    with cb2:
        tripped = dd_frac_of_limit >= 1.0
        label = _status_emoji(dd_frac_of_limit, tripped)
        st.markdown(f"**Peak Drawdown** · {label}")
        st.progress(min(dd_frac_of_limit, 1.0),
                    text=f"{dd_pct_live:.2f}% from peak · lockout at {dd_lockout_pct:.0%}")
        st.caption(f"peak ${peak:,.0f} → live ${live_eq:,.0f}")

    with cb3:
        tripped = bool(state_data.get("is_halved", False))
        label = "🔴 HALVED" if tripped else _status_emoji(min(halt_frac, 1.0))
        st.markdown(f"**Daily Loss Halt** · {label}")
        st.progress(min(max(halt_frac, 0), 1.0),
                    text=f"intraday loss vs halt @ {daily_halt_pct:.1%}")
        st.caption("Triggering this halves position size for the rest of the day")

    # ── Row 2: flow/usage breakers ───────────────────────────────────────────
    cb4, cb5, cb6 = st.columns(3)

    with cb4:
        label = _status_emoji(opt_frac)
        st.markdown(f"**Options Daily Premium** · {label}")
        st.progress(opt_frac, text=f"${opt_spent_now:,.0f} / ${opt_cap_now:,.0f}")
        st.caption("Flow counter — resets at UTC midnight each trading day")

    with cb5:
        label = _status_emoji(stock_frac)
        st.markdown(f"**Stock Daily Notional** · {label}")
        st.progress(stock_frac, text=f"${stock_spent_now:,.0f} / ${stock_cap:,.0f}")
        st.caption("Flow counter — resets daily")

    with cb6:
        label = _status_emoji(pos_frac)
        st.markdown(f"**Open Positions** · {label}")
        st.progress(pos_frac, text=f"{position_count} / {max_open} slots")
        st.caption(
            f"stk {len(state_data.get('positions', {}))} · "
            f"opt {len(state_data.get('options_positions', {}))} · "
            f"mleg {len(state_data.get('multi_leg_positions', {}))}"
        )

    # ── Row 3: deployed-capital breakers (no daily reset, release on close) ──
    cb7, cb8 = st.columns(2)

    opt_dep_frac   = min(opt_deployed   / max(opt_deployed_cap, 1),   1.0)
    stock_dep_frac = min(stock_deployed / max(stock_deployed_cap, 1), 1.0)

    with cb7:
        label = _status_emoji(opt_dep_frac)
        st.markdown(f"**Options Capital Deployed** · {label}")
        st.progress(opt_dep_frac, text=f"${opt_deployed:,.0f} / ${opt_deployed_cap:,.0f}")
        st.caption("Total cost-basis + capital-at-risk in play · does NOT reset daily · headroom frees when positions close")

    with cb8:
        label = _status_emoji(stock_dep_frac)
        st.markdown(f"**Stock Capital Deployed** · {label}")
        st.progress(stock_dep_frac, text=f"${stock_deployed:,.0f} / ${stock_deployed_cap:,.0f}")
        st.caption("Total notional in play · does NOT reset daily · headroom frees when positions close")

    # ── Recovery actions — deliberate operator controls ─────────────────────
    with st.expander("🛠 Recovery actions (operator-only)"):
        st.caption(
            "Use these only after reviewing the cause of a lockout or drawdown. "
            "**Stop the bot before clicking** — otherwise the in-memory state may "
            "overwrite your reset. Each action writes an entry to `logs/recovery.log`."
        )

        live_eq_now = live_eq or peak  # fallback if broker fetch failed
        try:
            from executor.order_executor import get_account_baseline as _broker_baseline
            broker_baseline = float(_broker_baseline() or 0)
        except Exception:
            broker_baseline = 0.0
        new_peak_suggested = max(live_eq_now, broker_baseline)

        st.markdown(
            f"**Reset peak equity** — current peak `${peak:,.2f}`, live equity "
            f"`${live_eq_now:,.2f}`, broker baseline `${broker_baseline:,.2f}` "
            f"→ will reset to `${new_peak_suggested:,.2f}`"
        )
        col_a, col_b = st.columns([1, 3])
        confirmed = col_a.checkbox("I reviewed the drawdown", key="reset_peak_confirm")
        if col_b.button("Reset peak + clear LOCKOUT", disabled=not confirmed, type="primary"):
            from datetime import datetime as _dt, timezone as _tz
            try:
                with open(STATE_FILE) as f:
                    sd = json.load(f)
                old_peak = sd.get("peak_equity", 0)
                sd["peak_equity"] = round(new_peak_suggested, 2)
                with open(STATE_FILE, "w") as f:
                    json.dump(sd, f, indent=2)
                lockout_removed = False
                if Path("LOCKOUT").exists():
                    Path("LOCKOUT").unlink()
                    lockout_removed = True
                Path("logs").mkdir(exist_ok=True)
                with open("logs/recovery.log", "a") as f:
                    f.write(
                        f"{_dt.now(_tz.utc).isoformat()} | reset_peak | "
                        f"old=${old_peak:.2f} new=${new_peak_suggested:.2f} "
                        f"live_eq=${live_eq_now:.2f} broker_baseline=${broker_baseline:.2f} "
                        f"lockout_cleared={lockout_removed}\n"
                    )
                st.success(
                    f"Peak reset: `${old_peak:,.2f}` → `${new_peak_suggested:,.2f}`. "
                    + ("LOCKOUT file removed. " if lockout_removed else "")
                    + "Restart the bot if it isn't running."
                )
                _live_equity.clear()  # invalidate cache so next read reflects fresh state
            except Exception as e:
                st.error(f"Reset failed: {e}")

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
        st.info("No tracked stock positions in bot state.")

    # ── Live broker positions (source of truth from Alpaca) ───────────────────
    st.subheader("Live Broker Positions (Alpaca)")
    st.caption("Fetched live from the broker — source of truth. The bot acts on tracked positions (bot state); any row here that the bot isn't tracking is an orphan from a crash or manual trade.")
    try:
        from executor.order_executor import get_stock_positions, get_option_positions
        live_stocks = get_stock_positions()
        live_opts = get_option_positions()
    except Exception as e:
        st.warning(f"Could not fetch live positions: {e}")
        live_stocks, live_opts = {}, []

    tracked_opt_keys = set()
    for mlp in state_data.get("multi_leg_positions", {}).values():
        for leg in mlp.get("legs", []):
            tracked_opt_keys.add(leg.get("contract_symbol"))
    for sym in state_data.get("options_positions", {}).keys():
        tracked_opt_keys.add(sym)

    if live_opts:
        st.write(f"**Option positions at broker: {len(live_opts)}**")
        opt_rows = []
        total_unreal = 0.0
        total_cost_basis = 0.0
        for p in live_opts:
            sym = p["symbol"]
            tracked = sym in tracked_opt_keys or (sym.startswith("O:") and sym[2:] in tracked_opt_keys)
            qty = p["qty"]
            avg_entry = p["avg_entry_price"]
            unreal = p["unrealized_pl"]
            cost_basis = abs(avg_entry * qty * 100)
            pnl_pct = (unreal / cost_basis * 100) if cost_basis > 0 else 0.0
            total_unreal += unreal
            total_cost_basis += cost_basis
            opt_rows.append({
                "Contract": sym,
                "Qty": qty,
                "Avg Entry": f"${avg_entry:.2f}",
                "Mark": f"${p['current_price']:.2f}",
                "Unreal P&L": f"${unreal:+,.2f}",
                "P&L %": f"{pnl_pct:+.2f}%",
                "Market Val": f"${p['market_value']:+,.2f}",
                "Tracked by bot": "✓" if tracked else "⚠ orphan",
            })
        st.dataframe(pd.DataFrame(opt_rows), use_container_width=True, hide_index=True)
        total_pct = (total_unreal / total_cost_basis * 100) if total_cost_basis > 0 else 0.0
        st.caption(
            f"Total unrealized P&L across option legs: **${total_unreal:+,.2f} ({total_pct:+.2f}%)** "
            f"on cost basis ${total_cost_basis:,.2f}"
        )
    else:
        st.info("No option positions at broker.")

    if live_stocks:
        st.write(f"**Stock positions at broker: {len(live_stocks)}**")
        stk_rows = [{"Symbol": s, "Qty": q} for s, q in live_stocks.items()]
        st.dataframe(pd.DataFrame(stk_rows), use_container_width=True, hide_index=True)
    elif not live_opts:
        # only hide entirely if both are empty and we already printed a note above
        pass

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

    # ── Today's Dynamic Watchlist ─────────────────────────────────────────────
    st.subheader("Today's Dynamic Watchlist")
    dyn = state_data.get("dynamic_watchlist", [])
    dyn_date = state_data.get("dynamic_watchlist_date", "")
    dyn_refreshed_at = state_data.get("dynamic_watchlist_refreshed_at", "")
    today = datetime.now().strftime("%Y-%m-%d")

    def _fmt_refresh(iso_ts: str) -> str:
        if not iso_ts:
            return "unknown"
        try:
            dt = datetime.fromisoformat(iso_ts)
            if dt.tzinfo is None:
                from datetime import timezone as _tz
                dt = dt.replace(tzinfo=_tz.utc)
            local = dt.astimezone()
            return local.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return iso_ts

    if dyn and dyn_date == today:
        st.caption(
            f"Discovered pre-market via Alpaca movers + most-actives · "
            f"{len(dyn)} symbols added on top of base watchlist · "
            f"last refreshed: {_fmt_refresh(dyn_refreshed_at)}"
        )
        rows = []
        for entry in dyn:
            rows.append({
                "Symbol": entry.get("symbol"),
                "Source": entry.get("source"),
                "Price": f"${entry.get('price', 0):.2f}" if entry.get("price") else "—",
                "% Change": f"{entry.get('percent_change', 0):+.2f}%" if entry.get("percent_change") else "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    elif dyn_date == today:
        st.info(
            f"Dynamic watchlist refreshed today — no eligible movers passed the liquidity filter. "
            f"Last refreshed: {_fmt_refresh(dyn_refreshed_at)}"
        )
    else:
        last = _fmt_refresh(dyn_refreshed_at) if dyn_refreshed_at else (dyn_date or "never")
        st.caption(f"Dynamic watchlist hasn't refreshed for today yet (last: {last}). Bot will refresh on next pre-market cycle.")

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
        st.subheader("Single-leg")
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("No open single-leg options positions.")

    # ── Multi-leg (spreads + iron condor) ──────────────────────────────────────
    mleg = state_data.get("multi_leg_positions", {})
    if mleg:
        st.subheader("Multi-leg (spreads + iron condor)")
        for key, p in mleg.items():
            direction = "CREDIT" if p["qty"] < 0 else "DEBIT"
            units = abs(p["qty"])
            origin = p.get("origin", "")
            origin_tag = ""
            if origin == "reconciled_ledger":
                origin_tag = " · _adopted (entry recovered from ledger)_"
            elif origin == "reconciled_orphan":
                origin_tag = " · _⚠ orphan (no ledger match — SL disabled, relies on TP/DTE)_"
            header = (f"**{p['strategy']}** · {p['underlying']} · {direction} "
                      f"· {units} unit{'s' if units != 1 else ''} · net ${p['net_entry']:.2f} "
                      f"· entered {p['entry_date']}{origin_tag}")
            with st.expander(header):
                leg_rows = []
                for leg in p["legs"]:
                    leg_rows.append({
                        "Leg": leg["side"].upper(),
                        "Type": leg["contract_type"],
                        "Strike": leg["strike"],
                        "Expiry": leg["expiry"],
                        "Contract": leg["contract_symbol"],
                        "Entry mid": f"${leg['entry_price']:.2f}",
                    })
                st.dataframe(pd.DataFrame(leg_rows), use_container_width=True, hide_index=True)

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
            min_value=100.0, max_value=50000.0, step=50.0,
            value=float(cfg.get("options_max_daily_usd", 1000.0)),
            help="Per-day premium-outlay cap for all option buys. Resets each day.",
        )
        opt_deployed_cap_input = st.number_input(
            "Options max deployed ($)",
            min_value=500.0, max_value=500000.0, step=500.0,
            value=float(cfg.get("options_max_deployed_usd", 10000.0)),
            help="Max cost-basis + capital-at-risk in play at any time. Does NOT reset daily — headroom frees when positions close.",
        )
    with m2:
        stock_on = st.toggle("Stock trading", value=bool(cfg.get("stock_trading_enabled", False)))
        stock_cap = st.number_input(
            "Stock max daily notional ($)",
            min_value=100.0, max_value=500000.0, step=100.0,
            value=float(cfg.get("stock_max_daily_usd", cfg.get("max_daily_spend_usd", 10000.0))),
            help="Per-day notional cap for stock buys. Resets each day.",
        )
        stock_deployed_cap_input = st.number_input(
            "Stock max deployed ($)",
            min_value=500.0, max_value=1000000.0, step=1000.0,
            value=float(cfg.get("stock_max_deployed_usd", 50000.0)),
            help="Max stock notional in play at any time. Does NOT reset daily.",
        )
    with m3:
        intraday_on = st.toggle("Intraday (ORB)", value=bool(cfg.get("intraday_enabled", False)),
                                help="Opening Range Breakout: builds 9:30-9:45 ET range, fires short-dated "
                                     "options on breakouts, flattens all intraday positions at 15:55 ET. "
                                     "Uses Polygon minute bars + HMM regime filter.")

    sp1, sp2, sp3 = st.columns(3)
    with sp1:
        sp_on = st.toggle("Vertical spreads", value=bool(cfg.get("spreads_enabled", False)),
                          help="Bull-put / bear-call credit spreads (defined-risk directional).")
        ic_on = st.toggle("Iron condor", value=bool(cfg.get("iron_condor_enabled", False)),
                          help="Neutral strategy. Fires when |score| < 0.3 — collects premium on chop.")
    with sp2:
        sp_delta = st.slider("Spread short-leg Δ", min_value=0.10, max_value=0.45, step=0.05,
                             value=float(cfg.get("spread_target_short_delta", 0.30)))
        sp_width = st.number_input("Spread wing width ($)", min_value=1.0, max_value=50.0, step=1.0,
                                   value=float(cfg.get("spread_wing_width", 5.0)))
    with sp3:
        ic_delta = st.slider("Iron condor short Δ", min_value=0.05, max_value=0.30, step=0.05,
                             value=float(cfg.get("iron_condor_short_delta", 0.15)))
        ic_width = st.number_input("Iron condor wing ($)", min_value=1.0, max_value=50.0, step=1.0,
                                   value=float(cfg.get("iron_condor_wing_width", 5.0)))

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
    # ── Dynamic Watchlist ──────────────────────────────────────────────────────
    st.subheader("Dynamic Watchlist")
    st.caption("Pre-market, pull top movers + most-actives from Alpaca, filter for options liquidity.")
    dw1, dw2, dw3 = st.columns(3)
    with dw1:
        dyn_on = st.toggle("Enable dynamic watchlist",
                           value=bool(cfg.get("dynamic_watchlist_enabled", True)),
                           help="When on: merges today's top movers into the scan set for bull/neutral regimes.")
    with dw2:
        dyn_limit = st.number_input("Max daily additions",
                                    min_value=5, max_value=50, step=5,
                                    value=int(cfg.get("dynamic_watchlist_limit", 20)))
        dyn_min_price = st.number_input("Min price ($)",
                                        min_value=1.0, max_value=50.0, step=1.0,
                                        value=float(cfg.get("dynamic_watchlist_min_price", 5.0)))
    with dw3:
        dyn_min_oi = st.number_input("Min ATM open interest",
                                     min_value=50, max_value=5000, step=50,
                                     value=int(cfg.get("dynamic_watchlist_min_oi", 500)))

    st.divider()

    # ── Signal Thresholds + Paper Safety Valve ─────────────────────────────────
    st.subheader("Signal Thresholds")
    st.caption("How aggressive the bot is about firing. Lower threshold = more trades, noisier edge.")
    t1, t2, t3 = st.columns(3)
    with t1:
        long_thr = st.slider("Long threshold (score ≥)", min_value=0.1, max_value=0.9, step=0.05,
                             value=float(cfg.get("signal_score_threshold_long", 0.4)))
        short_thr = st.slider("Short threshold (score ≤ -)", min_value=0.1, max_value=0.9, step=0.05,
                              value=abs(float(cfg.get("signal_score_threshold_short", -0.4))))
    with t2:
        force_on = st.toggle("Paper-only: force top-score if no fills by EOD",
                             value=bool(cfg.get("paper_force_top_score", False)),
                             help="Paper mode only. Fires ONE minimum-size position (1 contract) on the "
                                  "symbol with the highest |score| if nothing else filled today — "
                                  "observability, not conviction. Automatically skipped in LIVE mode.")
    with t3:
        force_h = st.number_input("Force after (ET hour)", min_value=9, max_value=15, step=1,
                                  value=int(cfg.get("paper_force_after_hour_et", 15)))
        force_m = st.number_input("Force after (ET minute)", min_value=0, max_value=59, step=5,
                                  value=int(cfg.get("paper_force_after_min_et", 30)))

    st.divider()

    # ── Intraday ORB controls ──────────────────────────────────────────────────
    st.subheader("Intraday ORB")
    st.caption("Opening Range Breakout. Builds range, fires short-dated options on breakouts, flattens at 15:55 ET.")
    ib1, ib2, ib3 = st.columns(3)
    with ib1:
        orb_window = st.number_input("Opening range (minutes)", min_value=5, max_value=60, step=5,
                                     value=int(cfg.get("intraday_opening_range_min", 15)))
        intraday_cap = st.number_input("Intraday daily premium cap ($)",
                                        min_value=50.0, max_value=5000.0, step=50.0,
                                        value=float(cfg.get("intraday_max_daily_usd", 500.0)))
    with ib2:
        intra_dte_min = st.number_input("DTE min", min_value=0, max_value=30, step=1,
                                         value=int(cfg.get("intraday_target_dte_min", 1)))
        intra_dte_max = st.number_input("DTE max", min_value=1, max_value=60, step=1,
                                         value=int(cfg.get("intraday_target_dte_max", 7)))
    with ib3:
        intra_delta = st.slider("Target Δ", min_value=0.20, max_value=0.70, step=0.05,
                                value=float(cfg.get("intraday_target_delta", 0.50)))
        intra_min_or_width = st.slider("Min OR width (%)", min_value=0.001, max_value=0.01, step=0.001,
                                        value=float(cfg.get("intraday_min_orb_width_pct", 0.003)))
    ib4, ib5, _ = st.columns(3)
    with ib4:
        intra_tp = st.slider("Intraday TP (%)", min_value=10, max_value=100, step=5,
                             value=int(cfg.get("intraday_take_profit_pct", 0.40) * 100))
    with ib5:
        intra_sl = st.slider("Intraday SL (%)", min_value=10, max_value=90, step=5,
                             value=int(cfg.get("intraday_stop_loss_pct", 0.30) * 100))

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
            "options_max_daily_usd":     float(opt_cap),
            "stock_max_daily_usd":       float(stock_cap),
            "options_max_deployed_usd":  float(opt_deployed_cap_input),
            "stock_max_deployed_usd":    float(stock_deployed_cap_input),
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
            "spreads_enabled":           bool(sp_on),
            "iron_condor_enabled":       bool(ic_on),
            "spread_target_short_delta": float(sp_delta),
            "spread_wing_width":         float(sp_width),
            "iron_condor_short_delta":   float(ic_delta),
            "iron_condor_wing_width":    float(ic_width),
            "signal_score_threshold_long":  float(long_thr),
            "signal_score_threshold_short": -float(short_thr),
            "paper_force_top_score":     bool(force_on),
            "paper_force_after_hour_et": int(force_h),
            "paper_force_after_min_et":  int(force_m),
            "intraday_enabled":          bool(intraday_on),
            "intraday_opening_range_min": int(orb_window),
            "intraday_max_daily_usd":    float(intraday_cap),
            "intraday_target_dte_min":   int(intra_dte_min),
            "intraday_target_dte_max":   int(intra_dte_max),
            "intraday_target_delta":     float(intra_delta),
            "intraday_min_orb_width_pct": float(intra_min_or_width),
            "intraday_take_profit_pct":  intra_tp / 100,
            "intraday_stop_loss_pct":    intra_sl / 100,
            "dynamic_watchlist_enabled":   bool(dyn_on),
            "dynamic_watchlist_limit":     int(dyn_limit),
            "dynamic_watchlist_min_price": float(dyn_min_price),
            "dynamic_watchlist_min_oi":    int(dyn_min_oi),
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
