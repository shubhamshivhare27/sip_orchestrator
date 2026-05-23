"""
dashboard/app.py
─────────────────
Streamlit Cloud dashboard for the SIP Orchestrator.

KEY FEATURE — PERSISTENT SIP AMOUNT:
  You enter ₹X once. It is saved to data/inputs/sip_config.json
  (committed to the repo). The GitHub Actions monthly run reads the
  same file — so whatever you set here is what the automation uses.
  It stays until you change it here.

Deploy: share.streamlit.io → repo: sip_orchestrator → main file: dashboard/app.py
Secrets (Streamlit Cloud → App Settings → Secrets):
  GMAIL_USER  = "shubhamshivhare27@gmail.com"
  GMAIL_PASS  = "your-gmail-app-password"
  UPSTOX_TOKEN = "current-daily-token"
  UPSTOX_TOKEN_EXPIRY = "2026-05-09T03:30:00"
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
import pandas as pd

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SIP Orchestrator",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Light-mode styling ────────────────────────────────────────────────────────
st.markdown("""
<style>
  html, body, [class*="css"] {
    font-family: Georgia, 'Times New Roman', serif;
    background: #F7F6F2;
    color: #1A1714;
  }
  .stMetric label  { font-size:10px !important; letter-spacing:1.5px;
                     color:#8C847A !important; text-transform:uppercase; }
  .stTabs [role="tab"] { font-size:12px; font-weight:600; letter-spacing:.3px; }
  .card  { background:#fff; border:1px solid #E2DDD5; border-radius:10px;
           padding:16px 20px; margin-bottom:14px; }
  .lbl   { font-size:10px; font-weight:700; letter-spacing:1.5px;
           color:#8C847A; text-transform:uppercase; margin-bottom:6px; }
  .stag  { display:inline-block; border-radius:4px; padding:2px 8px;
           font-size:10px; font-weight:700; margin:1px; }
  div[data-testid="stNumberInput"] label { font-size:11px !important; }
</style>
""", unsafe_allow_html=True)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).parent.parent
PLAN_PATH     = ROOT / "data/outputs/latest_execution_plan.json"
SIP_CFG_PATH  = ROOT / "data/inputs/sip_config.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

SLEEVE_CLR = {"Core":"#1B4FD8","Tactical":"#92400E","Thematic":"#5B21B6","Hedge":"#0F766E"}
PHASE_CLR  = {
    "STRONG RECOVERY":"#146B3A","EARLY EXPANSION":"#146B3A",
    "MID CYCLE":"#92400E","LATE CYCLE":"#5B21B6","CONTRACTION":"#991B1B"
}

def inr(v):
    try:    return f"₹{float(v):,.0f}"
    except: return "—"

def stance_full(s):
    return {"OW":"Overweight","N":"Neutral","UW":"Underweight"}.get(s, s)

# ── SIP config persistence ────────────────────────────────────────────────────

def load_sip_config() -> dict:
    if SIP_CFG_PATH.exists():
        with open(SIP_CFG_PATH) as f:
            return json.load(f)
    return {"sip_amount": 50000}

def save_sip_config(amount: float):
    SIP_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIP_CFG_PATH, "w") as f:
        json.dump({
            "sip_amount": amount,
            "updated_at": datetime.now().isoformat(),
            "updated_by": "dashboard",
        }, f, indent=2)

# ── Plan loader ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_plan():
    if PLAN_PATH.exists():
        with open(PLAN_PATH) as f:
            return json.load(f)
    return None

# ── Live clock ────────────────────────────────────────────────────────────────
now = datetime.now()

# ══════════════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════════════
col_title, col_refresh = st.columns([6,1])
with col_title:
    st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:3px;color:#1B4FD8;margin-bottom:2px;">HYBRID SIP ORCHESTRATOR</p>', unsafe_allow_html=True)
    st.markdown('<h1 style="font-size:24px;font-weight:700;margin:0 0 4px 0;">Monthly Allocation Engine</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<p style="font-size:12px;color:#8C847A;margin:0;">'
        f'📅 {now.strftime("%A, %d %B %Y")} &nbsp;·&nbsp; '
        f'🕐 <b style="color:#1B4FD8;">{now.strftime("%I:%M:%S %p IST")}</b></p>',
        unsafe_allow_html=True
    )
with col_refresh:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear(); st.rerun()

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# SIP AMOUNT — PERSISTENT ENTRY
# ══════════════════════════════════════════════════════════════════════════════
sip_cfg        = load_sip_config()
current_amount = sip_cfg.get("sip_amount", 50000)
updated_at     = sip_cfg.get("updated_at","")

st.markdown('<p class="lbl">Monthly SIP Amount</p>', unsafe_allow_html=True)
sip_col1, sip_col2, sip_col3 = st.columns([3,2,4])

with sip_col1:
    new_amount = st.number_input(
        label="Enter your monthly SIP amount (₹)",
        min_value=1000, max_value=10000000,
        value=int(current_amount), step=5000,
        help="This value is saved and used by the GitHub Actions monthly run automatically.",
        label_visibility="collapsed",
    )

with sip_col2:
    if st.button("💾 Save & Apply", use_container_width=True, type="primary"):
        save_sip_config(float(new_amount))
        st.success(f"Saved ₹{new_amount:,}")
        st.cache_data.clear()
        st.rerun()

with sip_col3:
    if updated_at:
        st.markdown(
            f'<p style="font-size:11px;color:#8C847A;padding-top:10px;">'
            f'Last saved: {updated_at[:16].replace("T"," ")} &nbsp;·&nbsp; '
            f'Persists until you change it here &nbsp;·&nbsp; '
            f'GitHub Actions monthly run reads this value automatically.</p>',
            unsafe_allow_html=True
        )

st.markdown("---")

# ── Load plan ─────────────────────────────────────────────────────────────────
plan = load_plan()

if not plan:
    st.warning("No execution plan found yet.")
    st.markdown("""
**To generate your first plan:**
1. Go to your GitHub repo → Actions → **Manual SIP Run** → Run workflow
2. The plan will appear here within a few minutes.

Or run locally:
```bash
python orchestrator/main.py --dry-run
```
""")
    st.stop()

meta    = plan.get("meta", {})
sleeves = plan.get("sleeve_status", {})
insts   = plan.get("execution_plan", [])
exits   = plan.get("exit_actions", [])
macro   = plan.get("macro_signal", {})
signals = plan.get("signal_engine", {})
phase   = meta.get("cycle_phase","—")
p_color = PHASE_CLR.get(phase,"#555")

# ── Top metrics ───────────────────────────────────────────────────────────────
m1,m2,m3,m4,m5,m6,m7 = st.columns(7)
m1.metric("Cycle Phase",     phase)
m2.metric("Macro Score",     f"{float(meta.get('macro_score',0))*100:.1f}%" if meta.get('macro_score') else "—")
m3.metric("Momentum",        meta.get("macro_momentum","—"))
m4.metric("Portfolio",       inr(meta.get("portfolio_value")))
m5.metric("SIP (saved)",     inr(current_amount))
m6.metric("Instruments",     str(len(insts)))
m7.metric("Exit Alerts",     str(len(exits)))

run_at  = str(meta.get("run_at",""))[:16].replace("T"," ")
nxt_run = meta.get("next_signal_run","—")
boost   = "✓ Applied" if meta.get("cycle_boost") else "Not applied"
st.markdown(
    f'<div style="background:#EEF2FF;border:1px solid #1B4FD830;border-radius:8px;'
    f'padding:10px 16px;font-size:11px;color:#1B4FD8;margin:8px 0 16px 0;">'
    f'Plan generated: <b>{run_at} IST</b> &nbsp;·&nbsp; '
    f'Signal date: <b>{meta.get("signal_date","—")}</b> &nbsp;·&nbsp; '
    f'Next signal run: <b>{nxt_run}</b> &nbsp;·&nbsp; '
    f'Cycle boost: <b>{boost}</b>'
    f'</div>', unsafe_allow_html=True
)

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
tab1,tab2,tab3,tab4,tab5 = st.tabs([
    "📋 Execution Plan",
    "⚖️ Sleeve Status",
    f"⚠️ Exit Actions ({len(exits)})",
    "📡 Macro Signal (Engine 3)",
    "🔔 Signal Engine (Engine 2)",
])

# ── TAB 1: EXECUTION PLAN ─────────────────────────────────────────────────────
with tab1:

    with st.expander("📅 How Buy Dates Are Decided — expand to see all 3 rules", expanded=False):
        r1,r2,r3 = st.columns(3)
        with r1:
            st.markdown("**Rule 1 — Engine 2 signal confirmed**")
            st.info(f"Buy date = **next Friday** (Engine 2's next run date)\n\nSignal already triggered. Waiting for next official Friday run to confirm no reversal before executing.")
        with r2:
            st.markdown("**Rule 2 — Macro BUY/WATCHLIST, no Engine 2 signal**")
            st.info("Buy date = **15th of current month**\n\nIf today > 15th → next month's 15th.\nIf 15th is NSE holiday → previous trading day.")
        with r3:
            st.markdown("**Rule 3 — Rebalance / Urgent exit**")
            st.info("Buy date = **next trading day**\n\nDrift should not compound.\nSkips NSE holidays and weekends automatically.")

    if not insts:
        st.info("No eligible instruments this run.")
    else:
        rows = []
        for i in insts:
            rows.append({
                "Instrument":  i["ticker"].replace(".NS",""),
                "Sleeve":      i["sleeve"],
                "Tag":         i["tag"],
                "Stance":      stance_full(i["stance"]),
                "Amount (₹)":  i["allocated_inr"],
                "Buy Date":    i["buy_date"],
                "Score":       i["composite"],
                "Confidence":  i["confidence"],
                "Eng2 ✓":      "✓" if i["has_engine2_signal"] else "",
                "Strategy":    i.get("engine2_strategy") or "Macro only",
                "RSI":         i.get("rsi") or "—",
                "4W Mom%":     i.get("mom_4w") or "—",
            })
        st.dataframe(
            pd.DataFrame(rows), use_container_width=True, hide_index=True,
            column_config={
                "Amount (₹)": st.column_config.NumberColumn(format="₹%d"),
                "Score":      st.column_config.ProgressColumn(min_value=0, max_value=100),
            }
        )

        st.markdown('<p class="lbl" style="margin-top:8px;">Score Breakdown — expand any row</p>', unsafe_allow_html=True)
        for i in insts:
            with st.expander(f"{i['ticker'].replace('.NS','')}  —  {inr(i['allocated_inr'])}  —  📅 {i['buy_date']}", expanded=False):
                c1,c2,c3,c4,c5 = st.columns(5)
                c1.metric("Composite",   i["composite"])
                c2.metric("Macro (40%)", i["macro_score"])
                c3.metric("Signal (35%)",i["signal_score"])
                c4.metric("RSI (15%)",   i["rsi_score"])
                c5.metric("Mom (10%)",   i["mom_score"])
                st.markdown(f"**Buy date rule:** {i.get('buy_date_rule','')}")
                st.markdown(f"**Source:** {i.get('buy_date_source','')}")
                if i.get("engine2_conditions"):
                    st.markdown(f"**Engine 2 conditions:** `{' | '.join(i['engine2_conditions'])}`")
                if i.get("engine2_ssf50"):
                    st.markdown(f"**SSF50 weekly:** {i['engine2_ssf50']}  ·  **RSI weekly:** {i.get('engine2_rsi_weekly','—')}")

        total_deployed = sum(i["allocated_inr"] for i in insts)
        eng2_count     = sum(1 for i in insts if i["has_engine2_signal"])
        st.markdown(
            f'<div style="background:#F7F6F2;border:1px solid #E2DDD5;border-radius:8px;'
            f'padding:10px 16px;font-size:11px;margin-top:8px;">'
            f'Total deployed: <b style="color:#1B4FD8;">{inr(total_deployed)}</b> of {inr(current_amount)} SIP &nbsp;·&nbsp; '
            f'Engine 2 confirmed: <b style="color:#146B3A;">{eng2_count} instruments</b>'
            f'</div>', unsafe_allow_html=True
        )

# ── TAB 2: SLEEVE STATUS ──────────────────────────────────────────────────────
with tab2:
    l, r = st.columns(2)
    with l:
        st.markdown('<p class="lbl">Sleeve Drift vs Target</p>', unsafe_allow_html=True)
        for sleeve, s in sleeves.items():
            cur = s["current_pct"]; tgt = s["target_pct"]; drift = s["drift_pct"]
            alloc = s["sip_allocation"]; stat = s["status"]
            color = SLEEVE_CLR.get(sleeve,"#555")
            sc    = {"STOP":"#991B1B","BOOST":"#146B3A","ON_TRACK":"#92400E"}.get(stat,"#555")
            st.markdown(
                f'<div style="margin-bottom:14px;">'
                f'<div style="display:flex;justify-content:space-between;margin-bottom:4px;">'
                f'<b style="font-size:13px;">{s.get("label",sleeve)}</b>'
                f'<span style="font-size:11px;color:{sc};font-weight:700;">{cur:.1f}% / {tgt}% &nbsp; {stat}</span></div>',
                unsafe_allow_html=True
            )
            st.progress(min(cur / 100, 1.0))
            dc = "#991B1B" if drift > 0 else "#146B3A"
            alloc_str = "SIP PAUSED" if alloc==0 else f"{inr(alloc)} this month"
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;font-size:10px;color:#8C847A;margin-top:2px;">'
                f'<span>Drift: <b style="color:{dc};">{drift:+.1f}%</b></span>'
                f'<span style="color:{color};font-weight:700;">{alloc_str}</span></div></div>',
                unsafe_allow_html=True
            )
        st.markdown("---")
        st.markdown('<p class="lbl">Allocation Rules Applied</p>', unsafe_allow_html=True)
        for sleeve, s in sleeves.items():
            color = SLEEVE_CLR.get(sleeve,"#555")
            alloc = s["sip_allocation"]
            a_color = "#991B1B" if alloc==0 else color
            st.markdown(
                f'<div style="padding:8px 0;border-bottom:1px solid #E2DDD5;">'
                f'<div style="display:flex;justify-content:space-between;">'
                f'<b style="font-size:12px;">{sleeve}</b>'
                f'<span style="font-size:14px;font-weight:800;color:{a_color};">{"₹0 — Paused" if alloc==0 else inr(alloc)}</span></div>'
                f'<div style="font-size:10px;color:#8C847A;margin-top:2px;">{s.get("allocation_rule","")}</div>'
                f'</div>', unsafe_allow_html=True
            )

    with r:
        st.markdown('<p class="lbl">Holdings by Sleeve (from Upstox)</p>', unsafe_allow_html=True)
        for sleeve, s in sleeves.items():
            color = SLEEVE_CLR.get(sleeve,"#555")
            st.markdown(
                f'<div style="font-size:11px;font-weight:700;color:{color};margin-bottom:4px;margin-top:10px;">'
                f'{s.get("label",sleeve).upper()} — {inr(s.get("current_value",0))} — {s["current_pct"]:.1f}%</div>',
                unsafe_allow_html=True
            )
            for h in s.get("holdings",[]):
                pc = "#146B3A" if float(h.get("pnl_pct",0))>=0 else "#991B1B"
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;background:#F7F6F2;'
                    f'border-radius:6px;padding:6px 10px;margin-bottom:3px;">'
                    f'<span style="font-size:11px;font-weight:600;">{h["ticker"].replace(".NS","")}'
                    f'<span style="color:#8C847A;font-size:10px;margin-left:8px;">{h.get("quantity",0)} units</span></span>'
                    f'<span style="font-size:11px;">{inr(h.get("current_value",0))} '
                    f'<b style="color:{pc};">{float(h.get("pnl_pct",0)):+.1f}%</b></span>'
                    f'</div>', unsafe_allow_html=True
                )

# ── TAB 3: EXIT ACTIONS ───────────────────────────────────────────────────────
with tab3:
    if not exits:
        st.success("✅ No exit actions required this month. All sleeves within tolerance bands.")
    else:
        st.error(f"⚠️ {len(exits)} exit action{'s' if len(exits)>1 else ''} — total: {inr(sum(e['exit_value'] for e in exits))}")
        for e in exits:
            border  = "#92400E" if e["exit_type"]=="URGENT" else "#991B1B"
            pc      = "#146B3A" if e.get("pnl_pct",0)>=0 else "#991B1B"
            st.markdown(
                f'<div class="card" style="border-left:4px solid {border};">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px;">'
                f'<div><b style="font-size:15px;">{e["ticker"].replace(".NS","")}</b>'
                f' <span class="stag" style="background:{border}18;color:{border};">{e["exit_type"]}</span>'
                f' <span class="stag" style="background:{SLEEVE_CLR.get(e["sleeve"],"#555")}18;color:{SLEEVE_CLR.get(e["sleeve"],"#555")};">{e["sleeve"]}</span>'
                + (f' <span class="stag" style="background:#FFF1F2;color:#991B1B;">Drift +{e.get("drift_pct","")}%</span>' if e.get("drift_pct") else "") +
                f'<div style="font-size:12px;color:#4A4540;margin-top:4px;">{e["reason"]}</div>'
                f'<div style="font-size:11px;color:#8C847A;margin-top:2px;">'
                f'P&L: <b style="color:{pc};">{float(e.get("pnl_pct",0)):+.1f}%</b> · {e.get("tax_note","")}</div>'
                f'<div style="font-size:11px;color:#1B4FD8;font-weight:600;margin-top:4px;">📅 Suggested exit: {e.get("suggested_date","—")}</div>'
                f'<div style="font-size:10px;color:#8C847A;">{e.get("date_rule","")}</div></div>'
                f'<div style="text-align:right;">'
                f'<div style="font-size:22px;font-weight:800;color:#991B1B;">{inr(e["exit_value"])}</div>'
                f'<div style="font-size:11px;color:#8C847A;">{e["units_to_exit"]} units @ ₹{e.get("current_price","—")}</div>'
                f'</div></div></div>', unsafe_allow_html=True
            )

# ── TAB 4: MACRO SIGNAL ───────────────────────────────────────────────────────
with tab4:
    mc1, mc2 = st.columns(2)
    with mc1:
        score = float(macro.get("score",0))*100
        fig = go.Figure(go.Indicator(
            mode="gauge+number", value=score,
            number={"valueformat":".1f","suffix":"%","font":{"size":26}},
            gauge={"axis":{"range":[0,100]},"bar":{"color":p_color},
                   "steps":[{"range":[0,20],"color":"#FFF1F2"},{"range":[20,40],"color":"#FEF3C7"},
                             {"range":[40,60],"color":"#FFFBEB"},{"range":[60,80],"color":"#ECFDF5"},
                             {"range":[80,100],"color":"#D1FAE5"}]},
            title={"text":f"{phase}","font":{"size":13}},
        ))
        fig.update_layout(height=240, margin=dict(l=20,r=20,t=40,b=10))
        st.plotly_chart(fig, use_container_width=True)

        for label, val in [("Phase",macro.get("phase","—")),("Score",f"{score:.1f}%"),
            ("Momentum",macro.get("momentum","—")),("Confidence",macro.get("confidence","—")),
            ("Report Date",macro.get("report_date","—")),
            ("Rebalance","YES ⚠" if macro.get("rebalance_signal") else "No"),
            ("Historical",macro.get("historical_precedent","—"))]:
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #E2DDD5;">'
                f'<span style="font-size:11px;color:#8C847A;">{label}</span>'
                f'<b style="font-size:12px;">{val}</b></div>', unsafe_allow_html=True
            )

        st.markdown('<p class="lbl" style="margin-top:12px;">Sector Stance</p>', unsafe_allow_html=True)
        for sector, stance in macro.get("sector_stance",{}).items():
            c = {"Overweight":"#146B3A","Neutral":"#92400E","Underweight":"#991B1B"}.get(stance,"#555")
            icon = {"Overweight":"⬆","Neutral":"➡","Underweight":"⬇"}.get(stance,"")
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #E2DDD5;">'
                f'<span style="font-size:12px;">{sector}</span>'
                f'<b style="font-size:11px;color:{c};">{icon} {stance}</b></div>', unsafe_allow_html=True
            )

    with mc2:
        etf_rows = [{"ETF":e["ticker"].replace(".NS",""),"Tag":e["tag"],
                     "Stance":stance_full(e["stance"]),
                     "Price":f"₹{e['price']:,.0f}" if e.get("price") else "—",
                     "RSI":e.get("rsi") or "—",
                     "4W Mom":f"{e['mom_4w']:+.1f}%" if e.get("mom_4w") is not None else "—",
                     "MA Signal":e.get("ma_signal") or "—"}
                    for e in macro.get("etf_tags",[])]
        if etf_rows:
            st.markdown('<p class="lbl">ETF Signal Table</p>', unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(etf_rows), use_container_width=True, hide_index=True)

        st.markdown('<p class="lbl" style="margin-top:12px;">All 15 Indicators</p>', unsafe_allow_html=True)
        for ind in macro.get("indicators",[]):
            sig = ind.get("signal","—")
            sc  = {"Bullish":"#146B3A","Neutral":"#92400E","Bearish":"#991B1B"}.get(sig,"#555")
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'padding:5px 0;border-bottom:1px solid #E2DDD5;">'
                f'<div style="display:flex;align-items:center;gap:6px;">'
                f'<span style="width:7px;height:7px;border-radius:50%;background:{sc};display:inline-block;"></span>'
                f'<span style="font-size:11px;">{ind.get("name","")}</span></div>'
                f'<div style="display:flex;gap:6px;align-items:center;">'
                f'<b style="font-size:11px;">{ind.get("value","—")}</b>'
                f'<span class="stag" style="background:{sc}18;color:{sc};">{sig}</span>'
                f'</div></div>', unsafe_allow_html=True
            )

# ── TAB 5: SIGNAL ENGINE ──────────────────────────────────────────────────────
with tab5:
    se1, se2 = st.columns(2)
    with se1:
        for label, val in [("Signal Date",signals.get("signal_date","—")),
            ("Next Run",signals.get("next_run_date","—")),
            ("BUY Signals",str(len(signals.get("buy_signals",[])))),
            ("SELL Signals",str(len(signals.get("sell_signals",[])))),
            ("Urgent Alerts",str(len(signals.get("urgent_alerts",[])))),
            ("Stocks Universe",str(signals.get("stocks_in_universe",0))),
            ("ETFs Universe",str(signals.get("etfs_in_universe",0)))]:
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #E2DDD5;">'
                f'<span style="font-size:11px;color:#8C847A;">{label}</span>'
                f'<b style="font-size:12px;">{val}</b></div>', unsafe_allow_html=True
            )
        if signals.get("strategies_run"):
            st.markdown('<p class="lbl" style="margin-top:12px;">Strategies Run</p>', unsafe_allow_html=True)
            for s in signals["strategies_run"]:
                st.markdown(f"- {s}")

    with se2:
        st.markdown('<p class="lbl">BUY Signals</p>', unsafe_allow_html=True)
        for sig in signals.get("buy_signals",[]):
            st.markdown(
                f'<div class="card" style="border-left:4px solid #146B3A;">'
                f'<b style="font-size:14px;">{sig["ticker"].replace(".NS","")}</b>'
                f' <span class="stag" style="background:#EDFAF3;color:#146B3A;">BUY</span><br>'
                f'<span style="font-size:11px;color:#4A4540;">{sig.get("strategy_name","")}</span><br>'
                f'<span style="font-size:10px;color:#8C847A;">'
                f'Date: {sig.get("date","—")} · RSI: {sig.get("rsi_weekly","—")} · SSF50: {sig.get("ssf50_weekly","—")}</span><br>'
                + "".join(f'<span class="stag" style="background:#EEF2FF;color:#1B4FD8;">{c}</span>' for c in sig.get("conditions",[]))
                + '</div>', unsafe_allow_html=True
            )
        if not signals.get("buy_signals"):
            st.info("No BUY signals this week.")

        st.markdown('<p class="lbl" style="margin-top:12px;">⚠️ Urgent Alerts</p>', unsafe_allow_html=True)
        for a in signals.get("urgent_alerts",[]):
            st.markdown(
                f'<div class="card" style="border-left:4px solid #92400E;">'
                f'<b style="font-size:13px;">{a["ticker"].replace(".NS","")}</b><br>'
                f'<span style="font-size:11px;color:#92400E;">{a.get("reason","")}</span>'
                f'</div>', unsafe_allow_html=True
            )
        if not signals.get("urgent_alerts"):
            st.success("No urgent alerts.")

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    f'<p style="font-size:10px;color:#B8B0A8;text-align:center;">'
    f'SIP Orchestrator · Personal investment research · Not financial advice · '
    f'Always verify before executing · Plan as of {run_at} IST'
    f'</p>', unsafe_allow_html=True
)
