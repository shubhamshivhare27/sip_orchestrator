"""
orchestrator/main.py
─────────────────────
12-step SIP orchestration pipeline (v3 — Phase B integrated).

  Step 1   Load config
  Step 2   Fetch Engine 3 macro email → parse
  Step 3   Fetch Engine 2 signal email → parse
  Step 4   Fetch Upstox holdings (equity + MF)
  Step 5   Classify holdings into sleeves → compute weights
  Step 6   Compute sleeve drift → SIP split
  Step 7   Check thematic phase rotation (EXIT/ENTER signals)
  Step 8   Run 12-indicator live scoring on all ETFs
  Step 9   Assess dip conditions → tranche deployment
  Step 10  Score instruments + attach buy dates
  Step 11  Compute exit actions
  Step 12  Write outputs + send email

Usage:
  python orchestrator/main.py --sip 50000
  python orchestrator/main.py --sip 50000 --dry-run
  python orchestrator/main.py            # reads SIP from data/inputs/sip_config.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.parsers.gmail_reader     import fetch_latest_macro_email, fetch_latest_signal_email
from orchestrator.parsers.macro_parser     import parse_macro_email
from orchestrator.parsers.signal_parser    import parse_signal_email
from orchestrator.bridge.upstox_client     import get_portfolio_snapshot, save_snapshot, load_snapshot
from orchestrator.engine.allocation_engine import classify_holdings, compute_portfolio_weights, compute_sip_allocation
from orchestrator.engine.instrument_scorer import score_instruments
from orchestrator.engine.buy_date_resolver import attach_buy_dates
from orchestrator.engine.exit_advisor      import compute_exit_actions
from orchestrator.engine.live_scorer       import score_all_etfs, scores_to_dict
from orchestrator.engine.phase_rotation    import compute_rotation_signals, signals_to_dict as rotation_to_dict
from orchestrator.engine.tranche_manager   import check_and_deploy, get_monthly_summary, assess_dip_condition
from orchestrator.email_sender             import send_execution_plan_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

CONFIG_PATH  = ROOT / "config" / "allocation_config.json"
CACHE_DIR    = ROOT / "data" / "cache"
INPUTS_DIR   = ROOT / "data" / "inputs"
OUTPUTS_DIR  = ROOT / "data" / "outputs"
SIP_CFG_PATH = ROOT / "data" / "inputs" / "sip_config.json"

for d in [CACHE_DIR, INPUTS_DIR, OUTPUTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

def load_sip_amount() -> float | None:
    if SIP_CFG_PATH.exists():
        with open(SIP_CFG_PATH) as f:
            data = json.load(f)
        return float(data.get("sip_amount", 0)) or None
    return None

def save_json(data: dict, name: str, directory: Path):
    path = directory / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Saved: {path}")

def load_cached(name: str) -> dict | None:
    path = INPUTS_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(sip_amount: float = None, dry_run: bool = False) -> dict:

    config = load_config()

    if not sip_amount:
        sip_amount = load_sip_amount()
    if not sip_amount:
        raise ValueError(
            "SIP amount not provided. Either:\n"
            "  1. Pass via CLI: --sip 50000\n"
            "  2. Set it in the Streamlit dashboard"
        )

    today = datetime.now().date()
    log.info("=" * 70)
    log.info(f"SIP ORCHESTRATOR v3  |  Rs.{sip_amount:,.0f}  |  {today}  |  dry_run={dry_run}")
    log.info("=" * 70)

    # ── Step 1: Engine 3 macro email ──────────────────────────────────────────
    log.info("STEP 1/12 — Engine 3 macro email (Gmail IMAP)...")
    macro_data = None
    if not dry_run:
        raw = fetch_latest_macro_email(config)
        if raw:
            macro_data = parse_macro_email(raw)
            save_json(macro_data, "macro_signal", INPUTS_DIR)
    if not macro_data:
        log.warning("Live fetch skipped — loading cached macro data...")
        macro_data = load_cached("macro_signal")
        if not macro_data:
            raise RuntimeError("No macro data. Run live or ensure data/inputs/macro_signal.json exists.")
    log.info(f"  Phase: {macro_data.get('phase')}  Score: {macro_data.get('score')}  Momentum: {macro_data.get('momentum')}")

    # ── Step 2: Engine 2 signal email ─────────────────────────────────────────
    log.info("STEP 2/12 — Engine 2 signal email (Gmail IMAP)...")
    signal_data = None
    if not dry_run:
        raw = fetch_latest_signal_email(config)
        if raw:
            signal_data = parse_signal_email(raw)
            save_json(signal_data, "signal_engine", INPUTS_DIR)
    if not signal_data:
        log.warning("Live fetch skipped — loading cached signal data...")
        signal_data = load_cached("signal_engine")
    if not signal_data:
        signal_data = {"buy_signals":[],"sell_signals":[],"urgent_alerts":[],
                       "next_run_date":None,"signal_date":None,"strategies_run":[]}
    log.info(f"  BUY: {len(signal_data.get('buy_signals',[]))}  SELL: {len(signal_data.get('sell_signals',[]))}  Alerts: {len(signal_data.get('urgent_alerts',[]))}")

    # ── Step 3: Upstox holdings ───────────────────────────────────────────────
    log.info("STEP 3/12 — Upstox holdings (equity + MF)...")
    snapshot = None
    if not dry_run:
        try:
            snapshot = get_portfolio_snapshot()
            save_snapshot(snapshot, CACHE_DIR)
        except Exception as e:
            log.warning(f"Upstox fetch failed: {e} — using cached snapshot")
    if not snapshot:
        snapshot = load_snapshot(CACHE_DIR)
    if not snapshot:
        snapshot = {"holdings":[], "total_value":0.0, "as_of":datetime.now().isoformat()}
    log.info(f"  Portfolio: Rs.{snapshot.get('total_value',0):,.0f}  Holdings: {len(snapshot.get('holdings',[]))}")

    # ── Step 4: Classify holdings → sleeve weights ────────────────────────────
    log.info("STEP 4/12 — Classifying holdings into sleeves...")
    classified = classify_holdings(snapshot.get("holdings",[]), config)
    weights    = compute_portfolio_weights(classified)

    # ── Step 5: Sleeve drift + SIP split ──────────────────────────────────────
    log.info("STEP 5/12 — Computing sleeve drift and SIP allocation...")
    cycle_phase = macro_data.get("phase", "UNKNOWN")
    alloc_plan  = compute_sip_allocation(sip_amount, weights, config, cycle_phase)
    for sleeve, s in alloc_plan.sleeves.items():
        log.info(f"  {sleeve}: {s.current_pct:.1f}%/{s.target_pct}% drift={s.drift_pct:+.1f}% -> Rs.{s.sip_allocation:,.0f} [{s.status}]")

    # ── Step 6: Thematic phase rotation ───────────────────────────────────────
    log.info("STEP 6/12 — Checking thematic phase rotation...")
    rotation_signals = compute_rotation_signals(cycle_phase, config, INPUTS_DIR)
    if rotation_signals:
        for sig in rotation_signals:
            log.info(f"  {sig.action}: {sig.ticker} ({sig.old_weight}% -> {sig.new_weight}%) — {sig.reason}")
    else:
        log.info("  No rotation — phase unchanged or first run.")

    # ── Step 7: 12-indicator live scoring ─────────────────────────────────────
    log.info("STEP 7/12 — Running 12-indicator live scoring...")
    all_etf_tickers = []
    for sleeve_cfg in config["sleeves"].values():
        all_etf_tickers.extend(sleeve_cfg.get("instruments", []))
    all_etf_tickers = list(set(all_etf_tickers))

    live_scores = {}
    if not dry_run:
        try:
            live_scores = score_all_etfs(all_etf_tickers)
        except Exception as e:
            log.warning(f"Live scoring failed: {e} — proceeding without live scores")
    else:
        log.info("  Dry run — live scoring skipped")

    # ── Step 8: Tranche deployment check ──────────────────────────────────────
    log.info("STEP 8/12 — Assessing dip conditions for tranche deployment...")
    tranche_result = None
    if not dry_run and live_scores:
        # Use Nifty ETF RSI as the index-level RSI for tranche triggers
        nifty_score = live_scores.get("NIFTYIETF.NS") or live_scores.get("NIFTYBEES.NS")
        if nifty_score and nifty_score.indicators:
            rsi_ind = next((i for i in nifty_score.indicators if i.name == "RSI Zone"), None)
            vix_ind = next((i for i in nifty_score.indicators if i.name == "India VIX"), None)
            rsi_val = float(rsi_ind.value) if rsi_ind else None
            vix_val = float(vix_ind.value) if vix_ind and vix_ind.value != "N/A" else None

            # Compute Nifty vs SMA50
            sma_ind = next((i for i in nifty_score.indicators if "SSF50" in i.name), None)
            nifty_vs_sma = None
            if sma_ind and nifty_score.price:
                try:
                    ssf_val = float(sma_ind.value)
                    nifty_vs_sma = (nifty_score.price - ssf_val) / ssf_val * 100
                except (ValueError, TypeError):
                    pass

            tranche_result = check_and_deploy(
                sip_amount=sip_amount,
                rsi=rsi_val,
                nifty_vs_sma50=nifty_vs_sma,
                vix=vix_val,
                inputs_dir=INPUTS_DIR,
                today=today,
            )
            log.info(f"  Tranche: {tranche_result.get('action')} — {tranche_result.get('reason','')}")

    if not tranche_result:
        tranche_result = {"action": "SKIPPED", "reason": "Dry run or no live data"}
        log.info("  Tranche check skipped")

    tranche_summary = get_monthly_summary(sip_amount, INPUTS_DIR)

    # ── Compute effective deployment amount based on tranche state ─────────
    # The execution plan shows the FULL monthly target per ETF.
    # deploy_now_pct tells you what fraction to actually deploy RIGHT NOW.
    deploy_now_pct = 1.0    # default: deploy full amount (no tranche system active)
    deploy_multiplier = 1.0
    active_tranche = "FULL"

    if tranche_summary.get("tranches"):
        # Tranche system is active — check what just deployed
        if tranche_result and tranche_result.get("action") == "DEPLOYED":
            # A tranche just fired — deploy that tranche's share × multiplier
            t_name = tranche_result["tranche"]
            t_pct_map = {"A": 0.50, "B": 0.30, "C": 0.20}
            deploy_now_pct = t_pct_map.get(t_name, 0.50)
            deploy_multiplier = tranche_result.get("multiplier", 1.0)
            active_tranche = f"Tranche {t_name}"
            log.info(f"  Deploy: Tranche {t_name} ({deploy_now_pct*100:.0f}%) × {deploy_multiplier}× = {deploy_now_pct * deploy_multiplier * 100:.0f}% of monthly plan")
        elif tranche_result and tranche_result.get("action") == "HOLD":
            # No trigger fired — don't deploy anything this run
            deploy_now_pct = 0.0
            active_tranche = "HOLD"
            log.info(f"  Deploy: HOLD — waiting for dip trigger. Fallback: {tranche_result.get('fallback_date','')}")
        elif tranche_result and tranche_result.get("action") == "ALL_DEPLOYED":
            # All tranches already deployed this month — nothing more
            deploy_now_pct = 0.0
            active_tranche = "ALL_DONE"
            log.info("  Deploy: All 3 tranches already deployed this month.")

    effective_deploy_factor = deploy_now_pct * deploy_multiplier

    # ── Step 9: Score instruments + buy dates ─────────────────────────────────
    log.info("STEP 9/12 — Scoring eligible ETFs (macro x signal)...")
    scored = score_instruments(macro_data, signal_data, alloc_plan, config)

    log.info("STEP 10/12 — Resolving buy dates...")
    scored = attach_buy_dates(scored, signal_data, config, today)

    # Compute deploy_now amounts for each instrument
    total_deploy_now = 0
    for inst in scored:
        inst.deploy_now_inr = round(inst.allocated_inr * effective_deploy_factor)
        inst.deploy_tranche = active_tranche
        inst.deploy_multiplier = deploy_multiplier
        total_deploy_now += inst.deploy_now_inr

        # Override buy date when tranche fires — deploy now, not on fixed 15th
        if active_tranche not in ("FULL", "HOLD", "ALL_DONE") and inst.deploy_now_inr > 0:
            if not inst.has_engine2_signal:  # Engine 2 signals keep their Friday date
                inst.buy_date = today.strftime("%d %b %Y")
                inst.buy_date_rule = (
                    f"{active_tranche} deployed at {deploy_multiplier}x. "
                    f"Dip trigger fired — deploy immediately instead of waiting for 15th."
                )
                inst.buy_date_source = f"Tranche system — {tranche_result.get('reason','')}"

        ls = live_scores.get(inst.ticker)
        ls_info = f" live={ls.pct}%/{ls.signal}" if ls else ""
        deploy_info = f" deploy_now=Rs.{inst.deploy_now_inr:,.0f}" if effective_deploy_factor != 1.0 else ""
        log.info(f"  {inst.ticker}: score={inst.composite} Rs.{inst.allocated_inr:,.0f}{deploy_info} buy={inst.buy_date} [{'ENG2' if inst.has_engine2_signal else 'MACRO'}]{ls_info}")

    # ── Step 10: Exit actions ─────────────────────────────────────────────────
    log.info("STEP 11/12 — Computing exit actions...")
    exits = compute_exit_actions(alloc_plan, classified, signal_data, config, today)

    # Add rotation EXIT signals to exit actions list
    for sig in rotation_signals:
        if sig.action == "EXIT":
            # Find the holding for this ticker if it exists
            matching_hold = next((h for h in classified if h["ticker"].upper() == sig.ticker.upper()), None)
            if matching_hold:
                from orchestrator.engine.exit_advisor import ExitAction
                from orchestrator.engine.buy_date_resolver import resolve_exit_date
                exit_info = resolve_exit_date(today, config)
                exits.append(ExitAction(
                    ticker=sig.ticker, sleeve="Thematic", exit_type="ROTATION",
                    reason=sig.reason,
                    units_held=matching_hold.get("quantity", 0),
                    units_to_exit=matching_hold.get("quantity", 0),
                    exit_value=matching_hold.get("current_value", 0),
                    avg_cost=matching_hold.get("avg_cost_price", 0),
                    current_price=matching_hold.get("last_price", 0),
                    pnl=matching_hold.get("pnl", 0),
                    pnl_pct=matching_hold.get("pnl_pct", 0),
                    tax_note="Equity ETF — STCG 20% if <1yr | LTCG 12.5% above 1.25L if >1yr",
                    drift_pct=None,
                    suggested_date=exit_info["date"],
                    date_rule=exit_info["rule"],
                ))

    # ── Assemble result ───────────────────────────────────────────────────────
    result = {
        "meta": {
            "run_at":           datetime.now().isoformat(),
            "run_date":         today.isoformat(),
            "sip_amount":       sip_amount,
            "cycle_phase":      cycle_phase,
            "macro_score":      macro_data.get("score"),
            "macro_momentum":   macro_data.get("momentum"),
            "macro_confidence": macro_data.get("confidence"),
            "signal_date":      signal_data.get("signal_date"),
            "next_signal_run":  signal_data.get("next_run_date"),
            "portfolio_value":  weights["total_value"],
            "total_allocated":  alloc_plan.total_allocated,
            "cycle_boost":      alloc_plan.cycle_boost_applied,
            "active_tranche":   active_tranche,
            "deploy_multiplier": deploy_multiplier,
            "deploy_factor":    effective_deploy_factor,
            "total_deploy_now": total_deploy_now,
            "dry_run":          dry_run,
            "pipeline_version": "v3_phase_b",
        },
        "sleeve_status": {
            name: {
                "label":           s.label,
                "target_pct":      s.target_pct,
                "current_pct":     s.current_pct,
                "drift_pct":       s.drift_pct,
                "status":          s.status,
                "sip_allocation":  s.sip_allocation,
                "allocation_rule": s.allocation_rule,
                "current_value":   s.current_value,
                "holdings": [{
                    "ticker":        h["ticker"],
                    "quantity":      h.get("quantity",0),
                    "avg_cost":      h.get("avg_cost_price",0),
                    "last_price":    h.get("last_price",0),
                    "current_value": h.get("current_value",0),
                    "pnl_pct":       h.get("pnl_pct",0),
                } for h in s.holdings],
            }
            for name, s in alloc_plan.sleeves.items()
        },
        "execution_plan": [{
            "ticker":              i.ticker,
            "sleeve":              i.sleeve,
            "tag":                 i.tag,
            "stance":              i.stance,
            "price":               i.price,
            "rsi":                 i.rsi,
            "mom_4w":              i.mom_4w,
            "ma_signal":           i.ma_signal,
            "ann_vol":             i.ann_vol,
            "composite":           i.composite,
            "macro_score":         i.macro_score,
            "signal_score":        i.signal_score,
            "rsi_score":           i.rsi_score,
            "mom_score":           i.mom_score,
            "confidence":          i.confidence,
            "allocated_inr":       i.allocated_inr,
            "deploy_now_inr":      i.deploy_now_inr,
            "deploy_tranche":      active_tranche,
            "deploy_multiplier":   deploy_multiplier,
            "buy_date":            i.buy_date,
            "buy_date_rule":       i.buy_date_rule,
            "buy_date_source":     i.buy_date_source,
            "has_engine2_signal":  i.has_engine2_signal,
            "engine2_strategy":    i.engine2_strategy,
            "engine2_strategy_id": i.engine2_strategy_id,
            "engine2_conditions":  i.engine2_conditions,
            "engine2_ssf50":       i.engine2_ssf50,
            "engine2_rsi_weekly":  i.engine2_rsi_weekly,
            "live_score_pct":      live_scores[i.ticker].pct if i.ticker in live_scores else None,
            "live_signal":         live_scores[i.ticker].signal if i.ticker in live_scores else None,
        } for i in scored],
        "exit_actions": [{
            "ticker":         e.ticker,
            "sleeve":         e.sleeve,
            "exit_type":      e.exit_type,
            "reason":         e.reason,
            "units_held":     e.units_held,
            "units_to_exit":  e.units_to_exit,
            "exit_value":     e.exit_value,
            "avg_cost":       e.avg_cost,
            "current_price":  e.current_price,
            "pnl":            e.pnl,
            "pnl_pct":        e.pnl_pct,
            "tax_note":       e.tax_note,
            "drift_pct":      e.drift_pct,
            "suggested_date": e.suggested_date,
            "date_rule":      e.date_rule,
        } for e in exits],
        "live_scores": scores_to_dict(live_scores),
        "thematic_rotation": {
            "phase_changed": len([s for s in rotation_signals if s.action in ("EXIT","ENTER")]) > 0,
            "signals": rotation_to_dict(rotation_signals),
        },
        "tranche_deployment": {
            "current_check": tranche_result,
            "monthly_summary": tranche_summary,
        },
        "macro_signal":  macro_data,
        "signal_engine": signal_data,
    }

    # Write outputs
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    save_json(result, f"execution_plan_{ts}", OUTPUTS_DIR)
    save_json(result, "latest_execution_plan", OUTPUTS_DIR)

    # ── Step 12: Send email ───────────────────────────────────────────────────
    log.info("STEP 12/12 — Sending execution plan email...")
    if not dry_run:
        email_sent = send_execution_plan_email(result, config)
        if email_sent:
            log.info("  Email sent successfully")
        else:
            log.warning("  Email not sent (check config/credentials)")
    else:
        log.info("  Dry run — email skipped")

    log.info("=" * 70)
    log.info(f"DONE  {len(scored)} instruments  Rs.{alloc_plan.total_allocated:,.0f} deployed  {len(exits)} exits")
    log.info("=" * 70)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SIP Orchestrator v3")
    parser.add_argument("--sip",     type=float, help="Monthly SIP amount in Rs")
    parser.add_argument("--dry-run", action="store_true", help="Use cached data, skip live API")
    args   = parser.parse_args()
    result = run(sip_amount=args.sip, dry_run=args.dry_run)
    print(json.dumps(result["meta"], indent=2, default=str))
