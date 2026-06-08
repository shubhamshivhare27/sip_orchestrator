"""
scripts/sync_signals.py
────────────────────────
Saturday 8 AM IST weekly sync.
Now also checks dip conditions and deploys tranches if triggered.
Sends email notification when a tranche fires.
"""
import json, logging, os, sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sync")

from orchestrator.parsers.gmail_reader  import fetch_latest_macro_email, fetch_latest_signal_email
from orchestrator.parsers.macro_parser  import parse_macro_email
from orchestrator.parsers.signal_parser import parse_signal_email
from orchestrator.bridge.upstox_client  import get_portfolio_snapshot, save_snapshot

CONFIG_PATH = ROOT / "config/allocation_config.json"
INPUTS_DIR  = ROOT / "data/inputs"
CACHE_DIR   = ROOT / "data/cache"
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

with open(CONFIG_PATH) as f:
    config = json.load(f)

# ── Sync emails ───────────────────────────────────────────────────────────────
log.info("Syncing Engine 3 macro email...")
raw = fetch_latest_macro_email(config)
if raw:
    data = parse_macro_email(raw)
    with open(INPUTS_DIR/"macro_signal.json","w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"  Macro: {data.get('phase')} | {data.get('score')}")

log.info("Syncing Engine 2 signal email...")
raw = fetch_latest_signal_email(config)
if raw:
    data = parse_signal_email(raw)
    with open(INPUTS_DIR/"signal_engine.json","w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"  Signals: {len(data.get('buy_signals',[]))} BUY | {len(data.get('urgent_alerts',[]))} alerts")

# ── Sync holdings ─────────────────────────────────────────────────────────────
log.info("Syncing Upstox holdings...")
try:
    snap = get_portfolio_snapshot()
    save_snapshot(snap, CACHE_DIR)
    with open(INPUTS_DIR/"holdings.json","w") as f:
        json.dump(snap, f, indent=2, default=str)
    log.info(f"  Holdings: Rs.{snap.get('total_value',0):,.0f}")
except Exception as e:
    log.warning(f"  Holdings failed: {e}")

# ── Check tranche dip triggers ────────────────────────────────────────────────
log.info("Checking tranche dip triggers...")
try:
    from orchestrator.engine.live_scorer import fetch_daily, _rsi, _ssf, fetch_india_vix
    from orchestrator.engine.tranche_manager import check_and_deploy, get_monthly_summary

    # Read saved SIP amount
    sip_path = INPUTS_DIR / "sip_config.json"
    sip_amount = 50000
    if sip_path.exists():
        with open(sip_path) as f:
            sip_amount = float(json.load(f).get("sip_amount", 50000))

    # Get Nifty RSI and VIX for tranche triggers
    nifty_df = fetch_daily("NIFTYIETF.NS", 100)
    rsi_val  = None
    nifty_vs_sma = None
    if nifty_df is not None and len(nifty_df) >= 50:
        import numpy as np
        rsi_series = _rsi(nifty_df["close"], 14)
        rsi_val = float(rsi_series.iloc[-1])
        ssf50   = _ssf(nifty_df["close"], 50)
        ssf_now = float(ssf50.iloc[-1])
        price   = float(nifty_df["close"].iloc[-1])
        nifty_vs_sma = (price - ssf_now) / ssf_now * 100

    vix_val = fetch_india_vix()

    # Weekly return (this week's close vs last week)
    weekly_ret = None
    if nifty_df is not None and len(nifty_df) >= 10:
        weekly_ret = (float(nifty_df["close"].iloc[-1]) / float(nifty_df["close"].iloc[-6]) - 1) * 100

    log.info(f"  Dip inputs: RSI={rsi_val:.1f if rsi_val else 'N/A'} "
             f"Nifty_vs_SSF50={nifty_vs_sma:.1f if nifty_vs_sma else 'N/A'}% "
             f"Weekly={weekly_ret:.1f if weekly_ret else 'N/A'}% "
             f"VIX={vix_val:.1f if vix_val else 'N/A'}")

    result = check_and_deploy(
        sip_amount=sip_amount,
        rsi=rsi_val,
        nifty_vs_sma50=nifty_vs_sma,
        weekly_return=weekly_ret,
        vix=vix_val,
        inputs_dir=INPUTS_DIR,
    )
    log.info(f"  Tranche result: {result.get('action')} — {result.get('reason','')}")

    # Send email notification if a tranche was deployed
    if result.get("action") == "DEPLOYED":
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            user = os.environ.get("GMAIL_USER","").strip()
            pwd  = os.environ.get("GMAIL_PASS","").strip()
            if user and pwd:
                recipient = config.get("email_sender",{}).get("recipient", user)
                tranche   = result["tranche"]
                amount    = result["amount_actual"]
                trigger   = result["trigger_type"]
                mult      = result["multiplier"]

                subject = f"[SIP Tranche {tranche}] Rs.{amount:,.0f} ({mult}x) — {trigger}"
                body = f"""
                <html><body style="font-family:Georgia,serif;background:#F7F6F2;padding:20px;">
                <div style="max-width:600px;margin:auto;background:#fff;border:1px solid #E2DDD5;border-radius:10px;padding:24px;">
                <h2 style="color:#1B4FD8;margin:0 0 8px;">Tranche {tranche} Deployed</h2>
                <table style="width:100%;font-size:14px;">
                <tr><td style="color:#8C847A;">Amount</td><td style="font-weight:bold;color:#146B3A;">Rs.{amount:,.0f}</td></tr>
                <tr><td style="color:#8C847A;">Multiplier</td><td>{mult}x</td></tr>
                <tr><td style="color:#8C847A;">Trigger</td><td style="font-weight:bold;color:#92400E;">{trigger}</td></tr>
                <tr><td style="color:#8C847A;">Reason</td><td>{result.get('dip_condition','')}</td></tr>
                <tr><td style="color:#8C847A;">Date</td><td>{result.get('deploy_date','')}</td></tr>
                </table>
                <p style="font-size:12px;color:#8C847A;margin-top:16px;">
                Open your Streamlit dashboard to see the full execution plan.
                Execute the trade manually in Upstox.
                </p>
                </div></body></html>
                """

                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = user
                msg["To"]      = recipient
                msg.attach(MIMEText(body, "html"))

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(user, pwd)
                    server.sendmail(user, recipient, msg.as_string())
                log.info(f"  Tranche email sent: {subject}")
        except Exception as e:
            log.warning(f"  Tranche email failed: {e}")

    summary = get_monthly_summary(sip_amount, INPUTS_DIR)
    log.info(f"  Monthly: {summary['remaining_tranches']} tranches remaining, "
             f"Rs.{summary['total_deployed']:,.0f} deployed of Rs.{summary['total_base']:,.0f}")

except Exception as e:
    log.warning(f"  Tranche check failed: {e}")

log.info("Weekly sync complete.")
