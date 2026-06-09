"""
dashboard/app.py — SIP Orchestrator Streamlit Dashboard (v3 — 8 tabs)
Tabs: Execution Plan | Sleeve Status | Exit Actions | Macro Signal | Signal Engine | Live Scores | Thematic Rotation | Tranche Status
"""
import json
from datetime import datetime
from pathlib import Path
import streamlit as st
import pandas as pd

st.set_page_config(page_title="SIP Orchestrator", page_icon="📊", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
html,body,[class*="css"]{font-family:Georgia,'Times New Roman',serif;background:#F7F6F2;color:#1A1714;}
.stMetric label{font-size:10px!important;letter-spacing:1.5px;color:#8C847A!important;text-transform:uppercase;}
.stTabs [role="tab"]{font-size:12px;font-weight:600;}
.card{background:#fff;border:1px solid #E2DDD5;border-radius:10px;padding:16px 20px;margin-bottom:14px;}
.lbl{font-size:10px;font-weight:700;letter-spacing:1.5px;color:#8C847A;text-transform:uppercase;margin-bottom:6px;}
.stag{display:inline-block;border-radius:4px;padding:2px 8px;font-size:10px;font-weight:700;margin:1px;}
</style>""", unsafe_allow_html=True)

ROOT=Path(__file__).parent.parent
PLAN_PATH=ROOT/"data/outputs/latest_execution_plan.json"
SIP_CFG_PATH=ROOT/"data/inputs/sip_config.json"

SLEEVE_CLR={"Core":"#1B4FD8","International":"#5B21B6","Thematic":"#92400E","Hedge":"#0F766E"}
def inr(v):
    try: return f"₹{float(v):,.0f}"
    except: return "—"

def load_sip():
    if SIP_CFG_PATH.exists():
        with open(SIP_CFG_PATH) as f: return json.load(f)
    return {"sip_amount":50000}

def save_sip(amt):
    SIP_CFG_PATH.parent.mkdir(parents=True,exist_ok=True)
    with open(SIP_CFG_PATH,"w") as f: json.dump({"sip_amount":amt,"updated_at":datetime.now().isoformat(),"updated_by":"dashboard"},f,indent=2)

@st.cache_data(ttl=300)
def load_plan():
    if PLAN_PATH.exists():
        with open(PLAN_PATH) as f: return json.load(f)
    return None

now=datetime.now()
c1,c2=st.columns([6,1])
with c1:
    st.markdown('<p style="font-size:10px;font-weight:700;letter-spacing:3px;color:#1B4FD8;margin-bottom:2px;">HYBRID SIP ORCHESTRATOR v3</p>',unsafe_allow_html=True)
    st.markdown(f'<p style="font-size:12px;color:#8C847A;">{now.strftime("%A, %d %B %Y")} · <b style="color:#1B4FD8;">{now.strftime("%I:%M %p IST")}</b></p>',unsafe_allow_html=True)
with c2:
    if st.button("🔄 Refresh",use_container_width=True): st.cache_data.clear(); st.rerun()

st.markdown("---")
sip_cfg=load_sip(); cur_amt=sip_cfg.get("sip_amount",50000)
st.markdown('<p class="lbl">Monthly SIP Amount</p>',unsafe_allow_html=True)
sc1,sc2,sc3=st.columns([3,2,4])
with sc1: new_amt=st.number_input("SIP",min_value=1000,max_value=10000000,value=int(cur_amt),step=5000,label_visibility="collapsed")
with sc2:
    if st.button("💾 Save",use_container_width=True,type="primary"): save_sip(float(new_amt)); st.success(f"Saved ₹{new_amt:,}"); st.cache_data.clear(); st.rerun()
with sc3: st.markdown(f'<p style="font-size:11px;color:#8C847A;padding-top:10px;">Persists for automated runs</p>',unsafe_allow_html=True)
st.markdown("---")

plan=load_plan()
if not plan:
    st.warning("No execution plan found. Run: Actions → Manual SIP Run → 50000")
    st.stop()

meta=plan.get("meta",{}); sleeves=plan.get("sleeve_status",{}); insts=plan.get("execution_plan",[])
exits=plan.get("exit_actions",[]); macro=plan.get("macro_signal",{}); signals=plan.get("signal_engine",{})
live_scores=plan.get("live_scores",[]); rotation=plan.get("thematic_rotation",{}); tranches=plan.get("tranche_deployment",{})
phase=meta.get("cycle_phase","—")

m1,m2,m3,m4,m5,m6,m7=st.columns(7)
m1.metric("Phase",phase); m2.metric("Score",f"{float(meta.get('macro_score',0))*100:.1f}%" if meta.get('macro_score') else "—")
m3.metric("Momentum",meta.get("macro_momentum","—")); m4.metric("Portfolio",inr(meta.get("portfolio_value")))
m5.metric("SIP",inr(cur_amt)); m6.metric("Instruments",str(len(insts))); m7.metric("Exits",str(len(exits)))

run_at=str(meta.get("run_at",""))[:16].replace("T"," ")
st.markdown(f'<div style="background:#EEF2FF;border:1px solid #1B4FD830;border-radius:8px;padding:10px 16px;font-size:11px;color:#1B4FD8;margin:8px 0 16px 0;">Plan: <b>{run_at}</b> · Signal: <b>{meta.get("signal_date","—")}</b> · Next run: <b>{meta.get("next_signal_run","—")}</b> · Boost: <b>{"✓" if meta.get("cycle_boost") else "—"}</b> · Pipeline: <b>{meta.get("pipeline_version","v2")}</b></div>',unsafe_allow_html=True)

# ── 8 TABS ────────────────────────────────────────────────────────────────────
tab1,tab2,tab3,tab4,tab5,tab6,tab7,tab8=st.tabs([
    "📋 Execution Plan","⚖️ Sleeve Status",f"⚠️ Exits ({len(exits)})",
    "📡 Macro (Eng3)","🔔 Signals (Eng2)",
    f"📊 Live Scores ({len(live_scores)})","🔄 Rotation","📈 Tranches"
])

# TAB 1: EXECUTION PLAN
with tab1:
    if not insts: st.info("No eligible instruments this run.")
    else:
        rows=[]
        for i in insts:
            rows.append({"ETF":i["ticker"].replace(".NS",""),"Sleeve":i["sleeve"],"Tag":i["tag"],
                "Monthly":i["allocated_inr"],"Deploy Now":i.get("deploy_now_inr",0),"Tranche":i.get("deploy_tranche","—"),"Buy Date":i["buy_date"],"Score":i["composite"],
                "Eng2":"✓" if i["has_engine2_signal"] else "","Strategy":i.get("engine2_strategy") or "Macro",
                "Live":f"{i.get('live_score_pct','')}%" if i.get("live_score_pct") else "—",
                "Live Sig":i.get("live_signal","—")})
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True,
            column_config={"Monthly":st.column_config.NumberColumn(format="₹%d"),"Deploy Now":st.column_config.NumberColumn(format="₹%d"),"Score":st.column_config.ProgressColumn(min_value=0,max_value=100)})
        for i in insts:
            with st.expander(f"{i['ticker'].replace('.NS','')} — {inr(i['allocated_inr'])} — 📅 {i['buy_date']}"):
                c1,c2,c3,c4,c5=st.columns(5)
                c1.metric("Composite",i["composite"]); c2.metric("Macro",i["macro_score"]); c3.metric("Signal",i["signal_score"])
                c4.metric("RSI",i["rsi_score"]); c5.metric("Mom",i["mom_score"])
                st.markdown(f"**Rule:** {i.get('buy_date_rule','')}")
                st.markdown(f"**Source:** {i.get('buy_date_source','')}")
                if i.get("live_score_pct"): st.markdown(f"**Live 12-indicator:** {i['live_score_pct']}% = {i.get('live_signal','')}")

# TAB 2: SLEEVE STATUS
with tab2:
    l,r=st.columns(2)
    with l:
        for sleeve,s in sleeves.items():
            color=SLEEVE_CLR.get(sleeve,"#555"); sc={"STOP":"#991B1B","BOOST":"#146B3A","ON_TRACK":"#92400E"}.get(s["status"],"#555")
            st.markdown(f'<div style="margin-bottom:14px;"><div style="display:flex;justify-content:space-between;"><b>{s.get("label",sleeve)}</b><span style="color:{sc};font-weight:700;">{s["current_pct"]:.1f}%/{s["target_pct"]}% {s["status"]}</span></div>',unsafe_allow_html=True)
            st.progress(min(s["current_pct"]/100,1.0))
            dc="#991B1B" if s["drift_pct"]>0 else "#146B3A"
            al="PAUSED" if s["sip_allocation"]==0 else inr(s["sip_allocation"])
            st.markdown(f'<div style="display:flex;justify-content:space-between;font-size:10px;color:#8C847A;"><span>Drift: <b style="color:{dc};">{s["drift_pct"]:+.1f}%</b></span><span style="color:{color};font-weight:700;">{al}</span></div></div>',unsafe_allow_html=True)
    with r:
        for sleeve,s in sleeves.items():
            color=SLEEVE_CLR.get(sleeve,"#555")
            st.markdown(f'<div style="font-size:11px;font-weight:700;color:{color};margin:10px 0 4px;">{s.get("label",sleeve).upper()} — {inr(s.get("current_value",0))}</div>',unsafe_allow_html=True)
            for h in s.get("holdings",[]):
                pc="#146B3A" if float(h.get("pnl_pct",0))>=0 else "#991B1B"
                st.markdown(f'<div style="display:flex;justify-content:space-between;background:#F7F6F2;border-radius:6px;padding:6px 10px;margin-bottom:3px;"><span style="font-size:11px;font-weight:600;">{h["ticker"].replace(".NS","")}<span style="color:#8C847A;font-size:10px;margin-left:8px;">{h.get("quantity",0)} units</span></span><span style="font-size:11px;">{inr(h.get("current_value",0))} <b style="color:{pc};">{float(h.get("pnl_pct",0)):+.1f}%</b></span></div>',unsafe_allow_html=True)

# TAB 3: EXIT ACTIONS
with tab3:
    if not exits: st.success("✅ No exit actions required.")
    else:
        for e in exits:
            border="#92400E" if e["exit_type"]=="ROTATION" else "#991B1B"
            pc="#146B3A" if e.get("pnl_pct",0)>=0 else "#991B1B"
            st.markdown(f'<div class="card" style="border-left:4px solid {border};"><b style="font-size:15px;">{e["ticker"].replace(".NS","")}</b> <span class="stag" style="background:{border}18;color:{border};">{e["exit_type"]}</span><div style="font-size:12px;color:#4A4540;margin-top:4px;">{e["reason"]}</div><div style="font-size:11px;color:#8C847A;">P&L: <b style="color:{pc};">{float(e.get("pnl_pct",0)):+.1f}%</b> · {e.get("tax_note","")}</div><div style="font-size:11px;color:#1B4FD8;font-weight:600;margin-top:4px;">📅 {e.get("suggested_date","—")}</div></div>',unsafe_allow_html=True)

# TAB 4: MACRO SIGNAL
with tab4:
    mc1,mc2=st.columns(2)
    with mc1:
        for label,val in [("Phase",macro.get("phase","—")),("Score",f"{float(macro.get('score',0))*100:.1f}%"),
            ("Momentum",macro.get("momentum","—")),("Confidence",macro.get("confidence","—")),
            ("Report Date",macro.get("report_date","—"))]:
            st.markdown(f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #E2DDD5;"><span style="font-size:11px;color:#8C847A;">{label}</span><b style="font-size:12px;">{val}</b></div>',unsafe_allow_html=True)
        st.markdown('<p class="lbl" style="margin-top:12px;">Sector Stance</p>',unsafe_allow_html=True)
        for sector,stance in macro.get("sector_stance",{}).items():
            c={"Overweight":"#146B3A","Neutral":"#92400E","Underweight":"#991B1B"}.get(stance,"#555")
            st.markdown(f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #E2DDD5;"><span>{sector}</span><b style="color:{c};">{stance}</b></div>',unsafe_allow_html=True)
    with mc2:
        etf_rows=[{"ETF":e["ticker"].replace(".NS",""),"Tag":e["tag"],"Stance":e.get("stance",""),"RSI":e.get("rsi") or "—","4W Mom":f"{e['mom_4w']:+.1f}%" if e.get("mom_4w") is not None else "—"} for e in macro.get("etf_tags",[])]
        if etf_rows: st.dataframe(pd.DataFrame(etf_rows),use_container_width=True,hide_index=True)

# TAB 5: SIGNAL ENGINE
with tab5:
    se1,se2=st.columns(2)
    with se1:
        for label,val in [("Signal Date",signals.get("signal_date","—")),("Next Run",signals.get("next_run_date","—")),
            ("BUY",str(len(signals.get("buy_signals",[])))),("SELL",str(len(signals.get("sell_signals",[])))),
            ("Alerts",str(len(signals.get("urgent_alerts",[]))))]:
            st.markdown(f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #E2DDD5;"><span style="color:#8C847A;">{label}</span><b>{val}</b></div>',unsafe_allow_html=True)
    with se2:
        for sig in signals.get("buy_signals",[]):
            st.markdown(f'<div class="card" style="border-left:4px solid #146B3A;"><b>{sig["ticker"].replace(".NS","")}</b> <span class="stag" style="background:#EDFAF3;color:#146B3A;">BUY</span><br><span style="font-size:11px;">{sig.get("strategy_name","")}</span></div>',unsafe_allow_html=True)
        if not signals.get("buy_signals"): st.info("No BUY signals this week.")
        for a in signals.get("urgent_alerts",[]):
            st.markdown(f'<div class="card" style="border-left:4px solid #92400E;"><b>{a["ticker"].replace(".NS","")}</b><br><span style="font-size:11px;color:#92400E;">{a.get("reason","")}</span></div>',unsafe_allow_html=True)

# TAB 6: LIVE SCORES (NEW)
with tab6:
    if not live_scores: st.info("No live scores available. Run without --dry-run to compute.")
    else:
        sig_colors={"STRONG BUY":"#146B3A","BUY":"#146B3A","PARTIAL":"#92400E","WATCH":"#92400E","AVOID":"#991B1B"}
        for ls in live_scores:
            sig=ls.get("signal","—"); sc=sig_colors.get(sig,"#555")
            with st.expander(f"{ls['ticker'].replace('.NS','')} — {ls['total_points']}/110 ({ls['pct']}%) — {sig}",expanded=False):
                st.progress(min(ls["pct"]/100,1.0))
                cats={"Trend":[],"Momentum":[],"Structure":[],"Macro":[]}
                for ind in ls.get("indicators",[]):
                    cats.get(ind["category"],[]).append(ind)
                for cat,inds_list in cats.items():
                    if not inds_list: continue
                    cat_total=sum(i["points"] for i in inds_list); cat_max=sum(i["max_points"] for i in inds_list)
                    st.markdown(f'<p class="lbl">{cat} ({cat_total}/{cat_max})</p>',unsafe_allow_html=True)
                    for ind in inds_list:
                        pct=ind["points"]/ind["max_points"]*100 if ind["max_points"]>0 else 0
                        bar_c="#146B3A" if pct>=70 else "#92400E" if pct>=40 else "#991B1B"
                        st.markdown(f'<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #E2DDD5;"><div><b style="font-size:12px;">{ind["name"]}</b><span style="font-size:10px;color:#8C847A;margin-left:8px;">{ind["value"]}</span></div><div style="display:flex;align-items:center;gap:8px;"><span style="font-size:11px;font-weight:700;color:{bar_c};">{ind["points"]}/{ind["max_points"]}</span></div></div>',unsafe_allow_html=True)
                        st.markdown(f'<div style="font-size:10px;color:#8C847A;margin-bottom:4px;">{ind["detail"]}</div>',unsafe_allow_html=True)

# TAB 7: THEMATIC ROTATION (NEW)
with tab7:
    rot_signals=rotation.get("signals",[])
    phase_changed=rotation.get("phase_changed",False)
    if phase_changed:
        exits_r=[s for s in rot_signals if s["action"]=="EXIT"]
        enters=[s for s in rot_signals if s["action"]=="ENTER"]
        keeps=[s for s in rot_signals if s["action"]=="KEEP"]
        if exits_r or enters:
            old_p=rot_signals[0].get("phase_from","?") if rot_signals else "?"
            new_p=rot_signals[0].get("phase_to","?") if rot_signals else "?"
            st.error(f"🔄 PHASE CHANGED: {old_p} → {new_p}")
        if exits_r:
            st.markdown('<p class="lbl">EXIT — Sell these thematic ETFs</p>',unsafe_allow_html=True)
            for s in exits_r:
                st.markdown(f'<div class="card" style="border-left:4px solid #991B1B;"><b>{s["ticker"].replace(".NS","")}</b> <span class="stag" style="background:#FFF1F2;color:#991B1B;">EXIT</span><span style="font-size:11px;color:#8C847A;margin-left:8px;">was {s["old_weight"]}%</span><div style="font-size:11px;color:#4A4540;margin-top:4px;">{s["reason"]}</div></div>',unsafe_allow_html=True)
        if enters:
            st.markdown('<p class="lbl">ENTER — Buy these thematic ETFs</p>',unsafe_allow_html=True)
            for s in enters:
                st.markdown(f'<div class="card" style="border-left:4px solid #146B3A;"><b>{s["ticker"].replace(".NS","")}</b> <span class="stag" style="background:#EDFAF3;color:#146B3A;">ENTER</span><span style="font-size:11px;color:#1B4FD8;font-weight:700;margin-left:8px;">{s["new_weight"]}%</span><div style="font-size:11px;color:#4A4540;margin-top:4px;">{s["reason"]}</div></div>',unsafe_allow_html=True)
        if keeps:
            st.markdown('<p class="lbl">KEEP — Active in both phases</p>',unsafe_allow_html=True)
            for s in keeps:
                wc="→" if s["old_weight"]==s["new_weight"] else f'{s["old_weight"]}% → {s["new_weight"]}%'
                st.markdown(f'<div class="card"><b>{s["ticker"].replace(".NS","")}</b> <span class="stag" style="background:#EEF2FF;color:#1B4FD8;">KEEP</span><span style="font-size:11px;color:#8C847A;margin-left:8px;">{wc}</span></div>',unsafe_allow_html=True)
    else:
        st.success(f"✅ No rotation — phase unchanged ({phase}). Thematic ETFs remain the same.")
        if rot_signals:
            st.markdown('<p class="lbl">Currently Active Thematic ETFs</p>',unsafe_allow_html=True)
            for s in rot_signals:
                st.markdown(f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #E2DDD5;"><b>{s["ticker"].replace(".NS","")}</b><span style="color:#1B4FD8;font-weight:700;">{s.get("new_weight",0)}%</span></div>',unsafe_allow_html=True)

# TAB 8: TRANCHE STATUS (NEW)
with tab8:
    current=tranches.get("current_check",{})
    summary=tranches.get("monthly_summary",{})

    if current:
        action=current.get("action","—")
        ac={"DEPLOYED":"#146B3A","HOLD":"#92400E","ALL_DEPLOYED":"#1B4FD8","SKIPPED":"#8C847A"}.get(action,"#555")
        st.markdown(f'<div class="card" style="border-left:4px solid {ac};"><div style="font-size:10px;color:#8C847A;text-transform:uppercase;">Latest Check</div><div style="font-size:18px;font-weight:700;color:{ac};">{action}</div><div style="font-size:12px;color:#4A4540;margin-top:4px;">{current.get("reason","")}</div>',unsafe_allow_html=True)
        if action=="DEPLOYED":
            st.markdown(f'<div style="margin-top:8px;font-size:14px;">Tranche <b>{current.get("tranche","")}</b> · <b style="color:#146B3A;">{inr(current.get("amount_actual",0))}</b> ({current.get("multiplier",1)}×) · {current.get("trigger_type","")}</div>',unsafe_allow_html=True)
        if current.get("fallback_date"):
            st.markdown(f'<div style="font-size:11px;color:#92400E;margin-top:4px;">3rd Thursday fallback: {current["fallback_date"]}</div>',unsafe_allow_html=True)
        st.markdown('</div>',unsafe_allow_html=True)

    if summary:
        st.markdown('<p class="lbl" style="margin-top:16px;">Monthly Summary</p>',unsafe_allow_html=True)
        s1,s2,s3=st.columns(3)
        s1.metric("Month",summary.get("month","—"))
        s2.metric("Total Deployed",inr(summary.get("grand_total_deployed",0)))
        s3.metric("SIP Amount",inr(meta.get("sip_amount",0)))
        
        # Per-sleeve breakdown
        for sl_name, sl_data in summary.get("sleeves",{}).items():
            color=SLEEVE_CLR.get(sl_name,"#555")
            st.markdown(f'<div style="margin-top:12px;font-weight:700;color:{color};">{sl_name} — Budget: {inr(sl_data.get("budget",0))} — Deployed: {inr(sl_data.get("total_deployed",0))} — {sl_data.get("remaining",0)} remaining</div>',unsafe_allow_html=True)

        for t in summary.get("tranches",[]):
            deployed=t.get("deployed",False)
            bc="#146B3A" if deployed else "#E2DDD5"
            st.markdown(f'<div style="display:flex;justify-content:space-between;align-items:center;padding:10px;border:1px solid {bc};border-radius:8px;margin-bottom:8px;{"background:#EDFAF3" if deployed else ""}"><div><b style="font-size:14px;">Tranche {t["name"]}</b><span style="font-size:11px;color:#8C847A;margin-left:8px;">({int(t["pct"]*100)}% of SIP = {inr(t["amount_base"])})</span></div><div>',unsafe_allow_html=True)
            if deployed:
                st.markdown(f'<span style="font-size:14px;font-weight:700;color:#146B3A;">{inr(t["amount_actual"])} ({t["multiplier"]}×)</span><br><span style="font-size:10px;color:#8C847A;">{t.get("trigger_type","")} · {t.get("deploy_date","")}</span>',unsafe_allow_html=True)
            else:
                st.markdown(f'<span style="font-size:12px;color:#8C847A;">Waiting for dip trigger...</span>',unsafe_allow_html=True)
            st.markdown('</div></div>',unsafe_allow_html=True)

st.markdown("---")
st.markdown(f'<p style="font-size:10px;color:#B8B0A8;text-align:center;">SIP Orchestrator v3 · Not financial advice · {run_at}</p>',unsafe_allow_html=True)
