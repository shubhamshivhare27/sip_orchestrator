"""
orchestrator/email_sender.py — Sends execution plan email with all v3 sections.
Sends FROM/TO shubhamshivhare554@gmail.com (self-send to same inbox).
"""
import logging, os, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
log = logging.getLogger("email_sender")

def _creds():
    return os.environ.get("GMAIL_USER","").strip(), os.environ.get("GMAIL_PASS","").strip()

def _inr(v):
    try: return f"₹{float(v):,.0f}"
    except: return "—"

def _build_html(result, config=None):
    meta=result.get("meta",{}); sleeves=result.get("sleeve_status",{})
    insts=result.get("execution_plan",[]); exits=result.get("exit_actions",[])
    live_scores=result.get("live_scores",[]); rotation=result.get("thematic_rotation",{})
    tranches=result.get("tranche_deployment",{})
    phase=meta.get("cycle_phase","—"); score=float(meta.get("macro_score",0))*100
    sip=meta.get("sip_amount",0); deployed=meta.get("total_allocated",0)
    run_date=meta.get("run_date","—")
    blue="#1B4FD8";green="#146B3A";amber="#92400E";red="#991B1B";light="#8C847A";bdr="#E2DDD5"
    sec=f"background:#fff;border:1px solid {bdr};border-radius:10px;padding:20px;margin-bottom:16px;"
    th=f"background:#EEF2FF;color:{blue};font-weight:bold;font-size:12px;padding:10px;text-align:left;border-bottom:2px solid {bdr};"
    td=f"padding:8px 10px;font-size:12px;border-bottom:1px solid {bdr};"

    html=f"""<html><body style="background:#F7F6F2;font-family:Georgia,serif;padding:24px;max-width:800px;margin:auto;">
    <div style="text-align:center;margin-bottom:20px;">
    <div style="font-size:10px;font-weight:bold;letter-spacing:3px;color:{blue};">HYBRID SIP ORCHESTRATOR v3</div>
    <div style="font-size:22px;font-weight:bold;">Monthly Execution Plan</div>
    <div style="font-size:12px;color:{light};">{run_date}</div></div>"""

    # Summary
    html+=f"""<div style="{sec}"><table style="width:100%;border-collapse:collapse;"><tr>
    <td style="text-align:center;padding:12px;"><div style="font-size:10px;color:{light};">PHASE</div><div style="font-size:18px;font-weight:bold;color:{green};">{phase}</div></td>
    <td style="text-align:center;"><div style="font-size:10px;color:{light};">SCORE</div><div style="font-size:18px;font-weight:bold;">{score:.1f}%</div></td>
    <td style="text-align:center;"><div style="font-size:10px;color:{light};">SIP</div><div style="font-size:18px;font-weight:bold;color:{blue};">{_inr(sip)}</div></td>
    <td style="text-align:center;"><div style="font-size:10px;color:{light};">DEPLOYED</div><div style="font-size:18px;font-weight:bold;color:{green};">{_inr(deployed)}</div></td>
    </tr></table></div>"""

    # Thematic rotation
    rot_sigs=rotation.get("signals",[])
    if rotation.get("phase_changed") and rot_sigs:
        old_p=rot_sigs[0].get("phase_from","?"); new_p=rot_sigs[0].get("phase_to","?")
        html+=f'<div style="{sec}border-left:4px solid {amber};"><div style="font-size:13px;font-weight:bold;color:{amber};">🔄 PHASE CHANGE: {old_p} → {new_p}</div><table style="width:100%;margin-top:8px;">'
        html+=f'<tr><th style="{th}">ETF</th><th style="{th}">Action</th><th style="{th}">Old %</th><th style="{th}">New %</th><th style="{th}">Reason</th></tr>'
        for s in rot_sigs:
            ac={"EXIT":red,"ENTER":green,"KEEP":blue}.get(s["action"],light)
            html+=f'<tr><td style="{td}font-weight:bold;">{s["ticker"].replace(".NS","")}</td><td style="{td}color:{ac};font-weight:bold;">{s["action"]}</td><td style="{td}">{s.get("old_weight",0)}%</td><td style="{td}">{s.get("new_weight",0)}%</td><td style="{td}font-size:11px;">{s.get("reason","")}</td></tr>'
        html+='</table></div>'

    # Execution plan
    if insts:
        html+=f'<div style="{sec}"><div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">📋 EXECUTION PLAN</div>'
        html+=f'<table style="width:100%;border-collapse:collapse;"><tr><th style="{th}">ETF</th><th style="{th}">Sleeve</th><th style="{th}">Amount</th><th style="{th}">Buy Date</th><th style="{th}">Score</th><th style="{th}">Live</th><th style="{th}">Eng2</th></tr>'
        for i in insts:
            t=i["ticker"].replace(".NS",""); ls_pct=i.get("live_score_pct","—"); ls_sig=i.get("live_signal","")
            eng2="✓" if i.get("has_engine2_signal") else ""
            html+=f'<tr><td style="{td}font-weight:bold;">{t}</td><td style="{td}">{i["sleeve"]}</td><td style="{td}color:{blue};font-weight:bold;">{_inr(i["allocated_inr"])}</td><td style="{td}color:{amber};font-weight:bold;">{i["buy_date"]}</td><td style="{td}">{i["composite"]}</td><td style="{td}">{ls_pct}% {ls_sig}</td><td style="{td}color:{green};">{eng2}</td></tr>'
        total=sum(i["allocated_inr"] for i in insts)
        html+=f'<tr style="background:#EEF2FF;"><td style="{td}font-weight:bold;" colspan="2">TOTAL</td><td style="{td}font-weight:bold;color:{blue};">{_inr(total)}</td><td colspan="4"></td></tr></table></div>'

    # Exit actions
    if exits:
        html+=f'<div style="{sec}border-left:4px solid {red};"><div style="font-size:13px;font-weight:bold;color:{red};">⚠️ EXIT ACTIONS ({len(exits)})</div>'
        for e in exits:
            html+=f'<div style="padding:8px 0;border-bottom:1px solid {bdr};"><b>{e["ticker"].replace(".NS","")}</b> <span style="background:{red}18;color:{red};border-radius:4px;padding:2px 6px;font-size:10px;">{e["exit_type"]}</span> <span style="float:right;font-weight:bold;color:{red};">{_inr(e["exit_value"])}</span><div style="font-size:11px;color:#4A4540;">{e["reason"]}</div><div style="font-size:11px;color:{blue};">📅 {e.get("suggested_date","—")}</div></div>'
        html+='</div>'

    # Tranche deployment
    summary=tranches.get("monthly_summary",{})
    if summary:
        rem=summary.get("remaining_tranches",3); dep=summary.get("total_deployed",0)
        html+=f'<div style="{sec}"><div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">📈 TRANCHE STATUS — {3-rem}/3 deployed</div>'
        for t in summary.get("tranches",[]):
            if t.get("deployed"):
                html+=f'<div style="padding:6px 0;border-bottom:1px solid {bdr};"><b>Tranche {t["name"]}</b> — <span style="color:{green};font-weight:bold;">{_inr(t["amount_actual"])} ({t["multiplier"]}×)</span> — {t.get("trigger_type","")} — {t.get("deploy_date","")}</div>'
            else:
                html+=f'<div style="padding:6px 0;border-bottom:1px solid {bdr};color:{light};">Tranche {t["name"]} — {_inr(t["amount_base"])} — waiting for dip trigger</div>'
        html+='</div>'

    # Sleeve status
    html+=f'<div style="{sec}"><div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">⚖️ SLEEVE STATUS</div>'
    html+=f'<table style="width:100%;border-collapse:collapse;"><tr><th style="{th}">Sleeve</th><th style="{th}">Current</th><th style="{th}">Target</th><th style="{th}">Drift</th><th style="{th}">SIP</th><th style="{th}">Status</th></tr>'
    for name,s in sleeves.items():
        dc=green if s["drift_pct"]<0 else red; alloc="PAUSED" if s["sip_allocation"]==0 else _inr(s["sip_allocation"])
        stc={"STOP":red,"BOOST":green,"ON_TRACK":amber}.get(s["status"],light)
        html+=f'<tr><td style="{td}font-weight:bold;">{s.get("label",name)}</td><td style="{td}">{s["current_pct"]:.1f}%</td><td style="{td}">{s["target_pct"]}%</td><td style="{td}color:{dc};font-weight:bold;">{s["drift_pct"]:+.1f}%</td><td style="{td}color:{blue};font-weight:bold;">{alloc}</td><td style="{td}color:{stc};font-weight:bold;">{s["status"]}</td></tr>'
    html+='</table></div>'

    # Top live scores
    if live_scores:
        html+=f'<div style="{sec}"><div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">📊 LIVE SCORES (12-indicator)</div>'
        html+=f'<table style="width:100%;border-collapse:collapse;"><tr><th style="{th}">ETF</th><th style="{th}">Score</th><th style="{th}">Signal</th><th style="{th}">Price</th></tr>'
        sorted_ls=sorted(live_scores,key=lambda x:x.get("pct",0),reverse=True)
        for ls in sorted_ls[:15]:
            sig=ls.get("signal","—"); sc={"STRONG BUY":green,"BUY":green,"PARTIAL":amber,"WATCH":amber,"AVOID":red}.get(sig,light)
            html+=f'<tr><td style="{td}font-weight:bold;">{ls["ticker"].replace(".NS","")}</td><td style="{td}">{ls.get("total_points",0)}/110 ({ls.get("pct",0)}%)</td><td style="{td}color:{sc};font-weight:bold;">{sig}</td><td style="{td}">₹{ls.get("price",0):,.1f}</td></tr>'
        html+='</table></div>'

    dash_url=config.get("email_sender",{}).get("dashboard_url","") if config else ""
    dash_btn=""
    if dash_url:
        dash_btn=f'<div style="text-align:center;margin:16px 0;"><a href="{dash_url}" style="display:inline-block;background:#1B4FD8;color:#fff;padding:14px 40px;border-radius:8px;font-size:15px;font-weight:bold;text-decoration:none;">Open Dashboard</a></div>'
    html+=dash_btn
    html+=f'<div style="text-align:center;padding:16px;color:{light};font-size:11px;">SIP Orchestrator v4</div></body></html>' · {datetime.now().strftime("%d %b %Y %I:%M %p")} · Not financial advice</div></body></html>'
    return html

def send_execution_plan_email(result, config):
    email_cfg=config.get("email_sender",{})
    if not email_cfg.get("enabled",False): log.info("Email disabled."); return False
    user,pwd=_creds()
    if not user or not pwd: log.warning("Gmail creds missing."); return False
    recipient=email_cfg.get("recipient",user)
    meta=result.get("meta",{}); insts=result.get("execution_plan",[])
    rotation=result.get("thematic_rotation",{})
    phase=meta.get("cycle_phase","—"); sip=meta.get("sip_amount",0)
    rot_flag=" | 🔄 ROTATION" if rotation.get("phase_changed") else ""
    subject=f"[SIP Orchestrator] {_inr(sip)} | {phase} | {len(insts)} instruments{rot_flag} | {meta.get('run_date','')}"
    msg=MIMEMultipart("alternative"); msg["Subject"]=subject; msg["From"]=user; msg["To"]=recipient
    msg.attach(MIMEText(_build_html(result, config),"html"))
    try:
        with smtplib.SMTP_SSL(email_cfg.get("smtp_host","smtp.gmail.com"),email_cfg.get("smtp_port",465)) as s:
            s.login(user,pwd); s.sendmail(user,recipient,msg.as_string())
        log.info(f"Email sent: {subject}"); return True
    except Exception as e:
        log.error(f"Email failed: {e}"); return False
