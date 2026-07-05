"""scripts/sync_signals.py (v4) — Saturday 8AM weekly sync + tranche deployment."""
import json, logging, os, smtplib, sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sync")

from orchestrator.parsers.gmail_reader  import fetch_latest_macro_email, fetch_latest_signal_email
from orchestrator.parsers.macro_parser  import parse_macro_email
from orchestrator.parsers.signal_parser import parse_signal_email
from orchestrator.bridge.upstox_client  import get_portfolio_snapshot, save_snapshot
from orchestrator.engine.allocation_engine import classify_holdings, compute_portfolio_weights, compute_sip_allocation
from orchestrator.engine import tranche_manager as tm

CONFIG = ROOT / "config/allocation_config.json"
INPUTS = ROOT / "data/inputs"
CACHE  = ROOT / "data/cache"
INPUTS.mkdir(parents=True, exist_ok=True); CACHE.mkdir(parents=True, exist_ok=True)

with open(CONFIG) as f: config = json.load(f)

# ── Sync emails ───────────────────────────────────────────────────────────────
log.info("=== Weekly Sync ===")
log.info("Syncing Engine 3...")
raw = fetch_latest_macro_email(config)
macro_data = None
if raw:
    macro_data = parse_macro_email(raw)
    with open(INPUTS/"macro_signal.json","w") as f: json.dump(macro_data,f,indent=2,default=str)
    log.info(f"  Phase: {macro_data.get('phase')} Score: {macro_data.get('score')}")

log.info("Syncing Engine 2...")
raw = fetch_latest_signal_email(config)
signal_data = None
if raw:
    signal_data = parse_signal_email(raw)
    with open(INPUTS/"signal_engine.json","w") as f: json.dump(signal_data,f,indent=2,default=str)
    log.info(f"  BUY: {len(signal_data.get('buy_signals',[]))} Alerts: {len(signal_data.get('urgent_alerts',[]))}")

log.info("Syncing holdings...")
snapshot = None
try:
    snapshot = get_portfolio_snapshot()
    save_snapshot(snapshot, CACHE)
    log.info(f"  Portfolio: Rs.{snapshot.get('total_value',0):,.0f}")
except Exception as e:
    log.warning(f"  Holdings failed: {e}")

# ── Compute sleeve budgets ────────────────────────────────────────────────────
sip_path = INPUTS / "sip_config.json"
sip_amount = 50000
if sip_path.exists():
    with open(sip_path) as f: sip_amount = float(json.load(f).get("sip_amount", 50000))

if snapshot:
    classified = classify_holdings(snapshot.get("holdings",[]), config)
    weights = compute_portfolio_weights(classified)
    phase = macro_data.get("phase","UNKNOWN") if macro_data else "UNKNOWN"
    alloc = compute_sip_allocation(sip_amount, weights, config, phase)
    sleeve_budgets = {name: s.sip_allocation for name, s in alloc.sleeves.items()}
else:
    alloc = None  # deploy_for_engine2 requires alloc for borrowing logic;
                  # passing None means it can't borrow across sleeves but
                  # won't crash — it falls back to own sleeve budget only.
    # Fallback: use target percentages when holdings unavailable (Upstox token
    # expired etc.) so the tranche check can still fire on dip/fallback dates.
    sleeve_budgets = {s: config["sleeves"][s]["target_pct"]/100*sip_amount for s in config["sleeves"]}

# ── Tranche dip check ─────────────────────────────────────────────────────────
log.info("Checking dip conditions for tranche deployment...")
today = datetime.now().date()
tranche_state = tm.load_state(INPUTS)
deployments = []

try:
    from orchestrator.engine.live_scorer import fetch_daily, _rsi, _ssf, fetch_india_vix
    nifty_df = fetch_daily("NIFTYIETF.NS", 100)
    rsi_val = vix_val = sma_val = wk_ret = None

    if nifty_df is not None and len(nifty_df) >= 50:
        import numpy as np
        rsi_s = _rsi(nifty_df["close"], 14)
        rsi_val = float(rsi_s.iloc[-1])
        ssf = _ssf(nifty_df["close"], 50)
        price = float(nifty_df["close"].iloc[-1])
        sma_val = (price - float(ssf.iloc[-1])) / float(ssf.iloc[-1]) * 100
        if len(nifty_df) >= 10:
            wk_ret = (float(nifty_df["close"].iloc[-1]) / float(nifty_df["close"].iloc[-6]) - 1) * 100

    vix_val = fetch_india_vix()
    rsi_str  = f"{rsi_val:.1f}"  if rsi_val  is not None else "N/A"
    vix_str  = f"{vix_val:.1f}"  if vix_val  is not None else "N/A"
    sma_str  = f"{sma_val:.1f}"  if sma_val  is not None else "N/A"
    wkr_str  = f"{wk_ret:.1f}"   if wk_ret   is not None else "N/A"
    log.info(f"  RSI={rsi_str} VIX={vix_str} SMA50={sma_str}% Weekly={wkr_str}%")

    dip = tm.assess_dip(rsi=rsi_val, vix=vix_val, nifty_vs_sma50=sma_val,
                        weekly_return=wk_ret, config=config)
    log.info(f"  Condition: {dip.trigger_type} — {dip.reason}")

    # Engine 2 BUY signals → priority deployment
    if signal_data:
        ticker_to_sleeve = {}
        for s, sc in config["sleeves"].items():
            for t in sc.get("instruments",[]):
                # Config instruments are .NS-suffixed; Engine 2 signal tickers
                # are bare (e.g. "AUTOBEES" not "AUTOBEES.NS") — strip here
                # so the lookup actually hits.
                ticker_to_sleeve[t.upper().replace(".NS","")] = s
        for sig in signal_data.get("buy_signals", []):
            ticker = sig["ticker"].upper()
            sleeve = ticker_to_sleeve.get(ticker)
            if sleeve:
                log.info(f"  Engine 2 BUY: {ticker} → deploying {sleeve} tranche")
                dep = tm.deploy_for_engine2(ticker, sleeve, sleeve_budgets, alloc,
                                           tranche_state, config, INPUTS, today)
                if dep.get("action") == "DEPLOYED":
                    deployments.append({"type":"ENGINE2","ticker":ticker,**dep})

    # Dip-based deployment
    if dip.trigger_type in ("DEEP_DIP", "MODERATE_DIP"):
        deps = tm.deploy_for_dip(sleeve_budgets, dip, tranche_state, config, INPUTS, today)
        for d in deps:
            deployments.append({"type":"DIP",**d})
            log.info(f"  Deployed: {d['sleeve']}/{d['tranche']} Rs.{d.get('actual',0):,.0f}")

    # 3rd Thursday fallback
    third_thu = tm._third_thursday(today.year, today.month)
    if today >= third_thu and not deployments:
        log.info("  3rd Thursday fallback...")
        deps = tm.deploy_for_dip(sleeve_budgets, dip, tranche_state, config, INPUTS, today)
        for d in deps:
            deployments.append({"type":"FALLBACK",**d})

    summary = tm.get_monthly_summary(sleeve_budgets, tranche_state, today)
    log.info(f"  Monthly: Rs.{summary.get('grand_total_deployed',0):,.0f} deployed")

except Exception as e:
    log.warning(f"  Tranche check failed: {e}")
    import traceback; traceback.print_exc()

# ── Email notification ────────────────────────────────────────────────────────
if deployments:
    try:
        user = os.environ.get("GMAIL_USER","").strip()
        pwd = os.environ.get("GMAIL_PASS","").strip()
        if user and pwd:
            recip = config.get("email_sender",{}).get("recipient", user)
            lines = []
            for d in deployments:
                dtype = d.get("type","")
                sleeve = d.get("sleeve","")
                tranche = d.get("tranche","")
                amount = d.get("actual",0)
                mult = d.get("multiplier",1)
                lines.append(f"<tr><td style='padding:8px;border-bottom:1px solid #E2DDD5;'><b>{dtype}</b></td>"
                    f"<td style='padding:8px;'>{sleeve}/{tranche}</td>"
                    f"<td style='padding:8px;color:#146B3A;font-weight:bold;'>Rs.{amount:,.0f} ({mult}x)</td></tr>")

            subject = f"[SIP Tranche] {len(deployments)} deployed | {dip.trigger_type}"
            body = f"""<html><body style="font-family:Georgia,serif;background:#F7F6F2;padding:20px;">
            <div style="max-width:600px;margin:auto;background:#fff;border:1px solid #E2DDD5;border-radius:10px;padding:24px;">
            <h2 style="color:#1B4FD8;">Tranche Deployment — {today}</h2>
            <p style="color:#8C847A;">Condition: <b style="color:#92400E;">{dip.trigger_type}</b> — {dip.reason}</p>
            <table style="width:100%;border-collapse:collapse;">
            <tr style="background:#EEF2FF;"><th style="padding:8px;text-align:left;">Trigger</th>
            <th style="padding:8px;">Sleeve/Tranche</th><th style="padding:8px;">Amount</th></tr>
            {"".join(lines)}
            </table>
            <p style="font-size:12px;color:#8C847A;margin-top:16px;">Execute trades manually in Upstox. Open dashboard for full details.</p>
            </div></body></html>"""

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject; msg["From"] = user; msg["To"] = recip
            msg.attach(MIMEText(body, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(user, pwd); s.sendmail(user, recip, msg.as_string())
            log.info(f"  Email sent: {subject}")
    except Exception as e:
        log.warning(f"  Email failed: {e}")

log.info("Weekly sync complete.")
