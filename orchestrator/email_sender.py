"""
orchestrator/email_sender.py
──────────────────────────────
Sends the SIP execution plan as an HTML email after each orchestrator run.

Uses the SAME Gmail credentials as the email reader:
  GMAIL_USER = shubhamshivhare554@gmail.com (the receiving account)
  GMAIL_PASS = App Password for that account

The email is sent FROM and TO the same account (self-send),
so you receive it in the same inbox where Engine 2 and Engine 3 emails land.

Subject format:
  [SIP Orchestrator] ₹50,000 | EARLY EXPANSION | 5 instruments | 01-Jun-2026

Called by main.py as the final step after writing execution_plan.json.
"""

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("email_sender")


def _creds():
    user = os.environ.get("GMAIL_USER", "").strip()
    pwd  = os.environ.get("GMAIL_PASS", "").strip()
    return user, pwd


def _inr(v):
    try:
        return f"₹{float(v):,.0f}"
    except (ValueError, TypeError):
        return "—"


def _pct(v):
    try:
        return f"{float(v):+.1f}%"
    except (ValueError, TypeError):
        return "—"


def _build_html(result: dict) -> str:
    """Build a clean HTML email from the execution plan result dict."""
    meta    = result.get("meta", {})
    sleeves = result.get("sleeve_status", {})
    insts   = result.get("execution_plan", [])
    exits   = result.get("exit_actions", [])
    macro   = result.get("macro_signal", {})
    signals = result.get("signal_engine", {})

    phase       = meta.get("cycle_phase", "—")
    score       = float(meta.get("macro_score", 0)) * 100
    momentum    = meta.get("macro_momentum", "—")
    sip         = meta.get("sip_amount", 0)
    deployed    = meta.get("total_allocated", 0)
    port_value  = meta.get("portfolio_value", 0)
    run_date    = meta.get("run_date", "—")
    signal_date = signals.get("signal_date", "—")
    next_run    = signals.get("next_run_date", "—")
    boost       = "Applied" if meta.get("cycle_boost") else "Not applied"

    # Styles
    bg     = "#F7F6F2"
    ink    = "#1A1714"
    blue   = "#1B4FD8"
    green  = "#146B3A"
    amber  = "#92400E"
    red    = "#991B1B"
    border = "#E2DDD5"
    light  = "#8C847A"

    section = f"background:#fff;border:1px solid {border};border-radius:10px;padding:20px;margin-bottom:16px;"
    th_style = f"background:#EEF2FF;color:{blue};font-weight:bold;font-size:12px;padding:10px;text-align:left;border-bottom:2px solid {border};"
    td_style = f"padding:8px 10px;font-size:12px;border-bottom:1px solid {border};color:{ink};"

    # ── Header ────────────────────────────────────────────────────────────────
    html = f"""
    <html><head><meta charset="UTF-8"></head>
    <body style="background:{bg};color:{ink};font-family:Georgia,'Times New Roman',serif;padding:24px;max-width:800px;margin:auto;">

    <div style="text-align:center;margin-bottom:20px;">
      <div style="font-size:10px;font-weight:bold;letter-spacing:3px;color:{blue};margin-bottom:4px;">HYBRID SIP ORCHESTRATOR</div>
      <div style="font-size:22px;font-weight:bold;color:{ink};">Monthly Execution Plan</div>
      <div style="font-size:12px;color:{light};margin-top:4px;">{run_date} &nbsp;·&nbsp; Signal date: {signal_date} &nbsp;·&nbsp; Next signal: {next_run}</div>
    </div>
    """

    # ── Summary metrics ───────────────────────────────────────────────────────
    html += f"""
    <div style="{section}">
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="text-align:center;padding:12px;">
            <div style="font-size:10px;color:{light};text-transform:uppercase;letter-spacing:1px;">Phase</div>
            <div style="font-size:18px;font-weight:bold;color:{green};">{phase}</div>
          </td>
          <td style="text-align:center;padding:12px;">
            <div style="font-size:10px;color:{light};text-transform:uppercase;">Score</div>
            <div style="font-size:18px;font-weight:bold;">{score:.1f}%</div>
          </td>
          <td style="text-align:center;padding:12px;">
            <div style="font-size:10px;color:{light};text-transform:uppercase;">SIP Amount</div>
            <div style="font-size:18px;font-weight:bold;color:{blue};">{_inr(sip)}</div>
          </td>
          <td style="text-align:center;padding:12px;">
            <div style="font-size:10px;color:{light};text-transform:uppercase;">Deployed</div>
            <div style="font-size:18px;font-weight:bold;color:{green};">{_inr(deployed)}</div>
          </td>
          <td style="text-align:center;padding:12px;">
            <div style="font-size:10px;color:{light};text-transform:uppercase;">Portfolio</div>
            <div style="font-size:18px;font-weight:bold;">{_inr(port_value)}</div>
          </td>
        </tr>
      </table>
      <div style="font-size:11px;color:{light};text-align:center;margin-top:8px;">
        Momentum: {momentum} &nbsp;·&nbsp; Cycle boost: {boost} &nbsp;·&nbsp;
        Instruments: {len(insts)} &nbsp;·&nbsp; Exit actions: {len(exits)}
      </div>
    </div>
    """

    # ── Execution Plan table ──────────────────────────────────────────────────
    if insts:
        html += f'<div style="{section}">'
        html += f'<div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">📋 EXECUTION PLAN — What to Buy</div>'
        html += f'<table style="width:100%;border-collapse:collapse;">'
        html += f'<tr><th style="{th_style}">ETF</th><th style="{th_style}">Sleeve</th>'
        html += f'<th style="{th_style}">Amount</th><th style="{th_style}">Buy Date</th>'
        html += f'<th style="{th_style}">Score</th><th style="{th_style}">Eng2</th>'
        html += f'<th style="{th_style}">Source</th></tr>'

        for i in insts:
            ticker = i["ticker"].replace(".NS", "")
            eng2   = "✓" if i.get("has_engine2_signal") else ""
            eng2_c = green if eng2 else light
            source = i.get("engine2_strategy") or "Macro only"
            html += f"""
            <tr>
              <td style="{td_style}"><b>{ticker}</b></td>
              <td style="{td_style}">{i["sleeve"]}</td>
              <td style="{td_style}font-weight:bold;color:{blue};">{_inr(i["allocated_inr"])}</td>
              <td style="{td_style}color:{amber};font-weight:bold;">{i["buy_date"]}</td>
              <td style="{td_style}">{i["composite"]}/100</td>
              <td style="{td_style}color:{eng2_c};font-weight:bold;">{eng2}</td>
              <td style="{td_style}font-size:11px;color:{light};">{source}</td>
            </tr>"""

        total = sum(i["allocated_inr"] for i in insts)
        html += f"""
            <tr style="background:#EEF2FF;">
              <td style="{td_style}font-weight:bold;" colspan="2">TOTAL DEPLOYED</td>
              <td style="{td_style}font-weight:bold;color:{blue};font-size:14px;">{_inr(total)}</td>
              <td style="{td_style}" colspan="4"></td>
            </tr>"""
        html += '</table></div>'

    # ── Buy Date Rules ────────────────────────────────────────────────────────
    if insts:
        html += f'<div style="{section}">'
        html += f'<div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">📅 BUY DATE DETAILS</div>'
        for i in insts:
            ticker = i["ticker"].replace(".NS", "")
            html += f"""
            <div style="padding:8px 0;border-bottom:1px solid {border};">
              <b style="font-size:12px;">{ticker}</b>
              <span style="font-size:11px;color:{amber};font-weight:bold;margin-left:8px;">{i["buy_date"]}</span>
              <div style="font-size:11px;color:{light};margin-top:2px;">{i.get("buy_date_rule","")}</div>
              <div style="font-size:10px;color:{blue};margin-top:1px;">{i.get("buy_date_source","")}</div>
            </div>"""
        html += '</div>'

    # ── Exit Actions ──────────────────────────────────────────────────────────
    if exits:
        html += f'<div style="{section}border-left:4px solid {red};">'
        html += f'<div style="font-size:13px;font-weight:bold;color:{red};margin-bottom:10px;">⚠️ EXIT ACTIONS ({len(exits)})</div>'
        for e in exits:
            ticker   = e["ticker"].replace(".NS", "")
            pnl_c    = green if e.get("pnl_pct", 0) >= 0 else red
            html += f"""
            <div style="padding:10px 0;border-bottom:1px solid {border};">
              <b style="font-size:14px;">{ticker}</b>
              <span style="display:inline-block;background:{red}18;color:{red};border-radius:4px;padding:2px 8px;font-size:10px;font-weight:bold;margin-left:6px;">{e["exit_type"]}</span>
              <span style="display:inline-block;font-size:16px;font-weight:bold;color:{red};float:right;">{_inr(e["exit_value"])}</span>
              <div style="font-size:11px;color:{ink};margin-top:4px;">{e["reason"]}</div>
              <div style="font-size:11px;color:{light};margin-top:2px;">
                {e["units_to_exit"]} units @ ₹{e.get("current_price","—")} &nbsp;·&nbsp;
                P&L: <b style="color:{pnl_c};">{_pct(e.get("pnl_pct"))}</b> &nbsp;·&nbsp;
                {e.get("tax_note","")}
              </div>
              <div style="font-size:11px;color:{blue};font-weight:bold;margin-top:4px;">📅 Suggested exit: {e.get("suggested_date","—")}</div>
            </div>"""
        html += '</div>'

    # ── Sleeve Status ─────────────────────────────────────────────────────────
    html += f'<div style="{section}">'
    html += f'<div style="font-size:13px;font-weight:bold;color:{blue};margin-bottom:10px;">⚖️ SLEEVE STATUS</div>'
    html += f'<table style="width:100%;border-collapse:collapse;">'
    html += f'<tr><th style="{th_style}">Sleeve</th><th style="{th_style}">Current</th>'
    html += f'<th style="{th_style}">Target</th><th style="{th_style}">Drift</th>'
    html += f'<th style="{th_style}">SIP This Month</th><th style="{th_style}">Status</th></tr>'

    for name, s in sleeves.items():
        drift_c = green if s["drift_pct"] < 0 else red
        alloc   = "PAUSED" if s["sip_allocation"] == 0 else _inr(s["sip_allocation"])
        alloc_c = red if s["sip_allocation"] == 0 else blue
        status_c = {"STOP":red, "BOOST":green, "ON_TRACK":amber}.get(s["status"], light)
        html += f"""
        <tr>
          <td style="{td_style}font-weight:bold;">{s.get("label",name)}</td>
          <td style="{td_style}">{s["current_pct"]:.1f}%</td>
          <td style="{td_style}">{s["target_pct"]}%</td>
          <td style="{td_style}color:{drift_c};font-weight:bold;">{_pct(s["drift_pct"])}</td>
          <td style="{td_style}color:{alloc_c};font-weight:bold;">{alloc}</td>
          <td style="{td_style}color:{status_c};font-weight:bold;">{s["status"]}</td>
        </tr>"""
    html += '</table></div>'

    # ── Footer ────────────────────────────────────────────────────────────────
    html += f"""
    <div style="text-align:center;padding:16px;color:{light};font-size:11px;">
      SIP Orchestrator &nbsp;·&nbsp; Generated {datetime.now().strftime("%d %b %Y %I:%M %p IST")}
      &nbsp;·&nbsp; Personal investment research — not financial advice
      &nbsp;·&nbsp; Always verify before executing
    </div>
    </body></html>
    """
    return html


def send_execution_plan_email(result: dict, config: dict) -> bool:
    """
    Send the execution plan as an HTML email.
    Called by main.py after writing execution_plan.json.
    """
    email_cfg = config.get("email_sender", {})
    if not email_cfg.get("enabled", False):
        log.info("Email sender disabled in config.")
        return False

    user, pwd = _creds()
    if not user or not pwd:
        log.warning("GMAIL_USER/GMAIL_PASS not set — skipping email.")
        return False

    recipient = email_cfg.get("recipient", user)
    prefix    = email_cfg.get("subject_prefix", "[SIP Orchestrator]")
    meta      = result.get("meta", {})
    insts     = result.get("execution_plan", [])

    phase    = meta.get("cycle_phase", "—")
    sip      = meta.get("sip_amount", 0)
    run_date = meta.get("run_date", datetime.now().strftime("%Y-%m-%d"))

    subject = (
        f"{prefix} {_inr(sip)} | {phase} | "
        f"{len(insts)} instruments | {run_date}"
    )

    html_body = _build_html(result)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        smtp_host = email_cfg.get("smtp_host", "smtp.gmail.com")
        smtp_port = email_cfg.get("smtp_port", 465)
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(user, pwd)
            server.sendmail(user, recipient, msg.as_string())
        log.info(f"Execution plan email sent to {recipient}: '{subject}'")
        return True
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return False
