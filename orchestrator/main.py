"""
orchestrator/main.py (v4)
─────────────────────────
13-step pipeline: Sleeve-first allocation → Engine 2 priority → Tranche deployment → Rotation.

  Step 1   Load config
  Step 2   Fetch Engine 3 macro email
  Step 3   Fetch Engine 2 signal email
  Step 4   Fetch Upstox holdings (equity + MF)
  Step 5   Classify holdings → sleeve weights
  Step 6   Compute sleeve drift → SIP budget per sleeve
  Step 7   Check thematic rotation (Engine 3 phase change)
  Step 8   Run 12-indicator live scoring
  Step 9   Engine 2 BUY signals → priority tranche deployment
  Step 10  Dip assessment → tranche deployment per sleeve
  Step 11  Score instruments + buy dates + tranche amounts
  Step 12  Compute exit actions
  Step 13  Write outputs + send email
"""

import argparse, json, logging, sys
from datetime import datetime, date
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
from orchestrator.engine import tranche_manager as tm
from orchestrator.email_sender             import send_execution_plan_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main")

CONFIG_PATH  = ROOT / "config" / "allocation_config.json"
CACHE_DIR    = ROOT / "data" / "cache"
INPUTS_DIR   = ROOT / "data" / "inputs"
OUTPUTS_DIR  = ROOT / "data" / "outputs"
SIP_CFG_PATH = ROOT / "data" / "inputs" / "sip_config.json"

for d in [CACHE_DIR, INPUTS_DIR, OUTPUTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

def load_config():
    with open(CONFIG_PATH) as f: return json.load(f)

def load_sip_amount():
    if SIP_CFG_PATH.exists():
        with open(SIP_CFG_PATH) as f: return float(json.load(f).get("sip_amount",0)) or None
    return None

def save_json(data, name, directory):
    path = directory / f"{name}.json"
    with open(path, "w") as f: json.dump(data, f, indent=2, default=str)
    log.info(f"Saved: {path}")

def load_cached(name):
    path = INPUTS_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f: return json.load(f)
    return None


def run(sip_amount=None, dry_run=False):
    config = load_config()
    if not sip_amount: sip_amount = load_sip_amount()
    if not sip_amount: raise ValueError("SIP amount not set. Use --sip or set in dashboard.")

    today = datetime.now().date()
    log.info("=" * 70)
    log.info(f"SIP ORCHESTRATOR v4 | Rs.{sip_amount:,.0f} | {today} | dry_run={dry_run}")
    log.info("=" * 70)

    # ── Step 1-2: Engine 3 macro ──────────────────────────────────────────────
    log.info("STEP 1/13 — Engine 3 macro email...")
    macro_data = None
    if not dry_run:
        raw = fetch_latest_macro_email(config)
        if raw: macro_data = parse_macro_email(raw); save_json(macro_data, "macro_signal", INPUTS_DIR)
    if not macro_data:
        macro_data = load_cached("macro_signal")
        if not macro_data: raise RuntimeError("No macro data.")
    log.info(f"  Phase: {macro_data.get('phase')}  Score: {macro_data.get('score')}")

    # ── Step 3: Engine 2 signals ──────────────────────────────────────────────
    log.info("STEP 2/13 — Engine 2 signal email...")
    signal_data = None
    if not dry_run:
        raw = fetch_latest_signal_email(config)
        if raw: signal_data = parse_signal_email(raw); save_json(signal_data, "signal_engine", INPUTS_DIR)
    if not signal_data: signal_data = load_cached("signal_engine")
    if not signal_data:
        signal_data = {"buy_signals":[],"sell_signals":[],"urgent_alerts":[],"next_run_date":None}
    log.info(f"  BUY: {len(signal_data.get('buy_signals',[]))}  Alerts: {len(signal_data.get('urgent_alerts',[]))}")

    # ── Step 4: Upstox holdings ───────────────────────────────────────────────
    log.info("STEP 3/13 — Upstox holdings (equity + MF)...")
    snapshot = None
    if not dry_run:
        try: snapshot = get_portfolio_snapshot(); save_snapshot(snapshot, CACHE_DIR)
        except Exception as e: log.warning(f"Upstox failed: {e}")
    if not snapshot: snapshot = load_snapshot(CACHE_DIR)
    if not snapshot: snapshot = {"holdings":[],"total_value":0.0}
    log.info(f"  Portfolio: Rs.{snapshot.get('total_value',0):,.0f}  Holdings: {len(snapshot.get('holdings',[]))}")

    # ── Step 5: Classify + weights ────────────────────────────────────────────
    log.info("STEP 4/13 — Classifying holdings...")
    classified = classify_holdings(snapshot.get("holdings",[]), config)
    weights = compute_portfolio_weights(classified)

    # ── Step 6: Sleeve drift → SIP budget per sleeve ──────────────────────────
    log.info("STEP 5/13 — Sleeve drift + SIP allocation...")
    cycle_phase = macro_data.get("phase", "UNKNOWN")
    alloc_plan = compute_sip_allocation(sip_amount, weights, config, cycle_phase)
    sleeve_budgets = {name: s.sip_allocation for name, s in alloc_plan.sleeves.items()}
    for name, s in alloc_plan.sleeves.items():
        log.info(f"  {name}: {s.current_pct:.1f}%/{s.target_pct}% drift={s.drift_pct:+.1f}% Rs.{s.sip_allocation:,.0f} [{s.status}]")

    # ── Step 7: Thematic rotation ─────────────────────────────────────────────
    log.info("STEP 6/13 — Thematic phase rotation...")
    rotation_signals = compute_rotation_signals(cycle_phase, config, INPUTS_DIR)
    rotation_deployment = None
    tranche_state = tm.load_state(INPUTS_DIR)

    if any(s.action in ("EXIT","ENTER") for s in rotation_signals):
        log.info(f"  PHASE CHANGE detected → deploying Thematic tranche")
        if not dry_run:
            rotation_deployment = tm.deploy_for_rotation(cycle_phase, config, sleeve_budgets, tranche_state, INPUTS_DIR, today)
            log.info(f"  Rotation deploy: {rotation_deployment.get('action','')}")
        for sig in rotation_signals:
            log.info(f"  {sig.action}: {sig.ticker} ({sig.old_weight}%→{sig.new_weight}%)")
    else:
        log.info("  No rotation this run.")

    # ── Step 8: Live scoring ──────────────────────────────────────────────────
    log.info("STEP 7/13 — 12-indicator live scoring...")
    all_tickers = list(set(t for s in config["sleeves"].values() for t in s.get("instruments",[])))
    live_scores = {}
    if not dry_run:
        try: live_scores = score_all_etfs(all_tickers)
        except Exception as e: log.warning(f"Live scoring failed: {e}")

    # ── Step 9: Engine 2 BUY → priority tranche deployment ────────────────────
    log.info("STEP 8/13 — Engine 2 BUY signal priority...")
    engine2_deployments = []
    ticker_to_sleeve = {}
    for sleeve, scfg in config["sleeves"].items():
        for t in scfg.get("instruments",[]): ticker_to_sleeve[t.upper()] = sleeve

    if not dry_run:
        for sig in signal_data.get("buy_signals", []):
            ticker = sig["ticker"].upper()
            sleeve = ticker_to_sleeve.get(ticker)
            if not sleeve:
                log.info(f"  {ticker}: BUY signal but not in any sleeve — skip")
                continue
            log.info(f"  {ticker}: Engine 2 BUY → deploying {sleeve} tranche (priority)")
            dep = tm.deploy_for_engine2(ticker, sleeve, sleeve_budgets, alloc_plan,
                                        tranche_state, config, INPUTS_DIR, today)
            engine2_deployments.append({"ticker":ticker, "sleeve":sleeve, **dep})
            log.info(f"    → {dep.get('action','')}")

    # ── Step 10: Dip assessment → tranche deployment ──────────────────────────
    log.info("STEP 9/13 — Dip condition check...")
    dip = tm.DipCondition()
    dip_deployments = []
    if not dry_run and live_scores:
        nifty = live_scores.get("NIFTYIETF.NS") or live_scores.get("NIFTYBEES.NS")
        rsi_val = vix_val = sma_val = wk_ret = None
        if nifty and nifty.indicators:
            rsi_i = next((i for i in nifty.indicators if i.name == "RSI Zone"), None)
            vix_i = next((i for i in nifty.indicators if i.name == "India VIX"), None)
            ssf_i = next((i for i in nifty.indicators if "SSF50" in i.name), None)
            try: rsi_val = float(rsi_i.value) if rsi_i else None
            except: pass
            try: vix_val = float(vix_i.value) if vix_i and vix_i.value != "N/A" else None
            except: pass
            if ssf_i and nifty.price:
                try: sma_val = (nifty.price - float(ssf_i.value)) / float(ssf_i.value) * 100
                except: pass

        dip = tm.assess_dip(rsi=rsi_val, vix=vix_val, nifty_vs_sma50=sma_val,
                           weekly_return=wk_ret, config=config)
        log.info(f"  Dip: {dip.trigger_type} ({dip.reason})")

        if dip.trigger_type in ("DEEP_DIP", "MODERATE_DIP"):
            dip_deployments = tm.deploy_for_dip(sleeve_budgets, dip, tranche_state,
                                                config, INPUTS_DIR, today)
            for d in dip_deployments:
                log.info(f"  Deployed: {d['sleeve']}/{d['tranche']} Rs.{d.get('actual',0):,.0f} ({d.get('multiplier',1)}x)")

        # 3rd Thursday fallback check
        third_thu = tm._third_thursday(today.year, today.month)
        if today >= third_thu and dip.trigger_type not in ("DEEP_DIP","MODERATE_DIP"):
            log.info("  3rd Thursday → fallback deployment...")
            fallback_deps = tm.deploy_for_dip(sleeve_budgets, dip, tranche_state,
                                              config, INPUTS_DIR, today)
            dip_deployments.extend(fallback_deps)

    # Get monthly summary
    tranche_summary = tm.get_monthly_summary(sleeve_budgets, tranche_state, today)

    # ── Step 11: Score instruments + buy dates ────────────────────────────────
    log.info("STEP 10/13 — Scoring instruments...")
    scored = score_instruments(macro_data, signal_data, alloc_plan, config)

    log.info("STEP 11/13 — Buy dates + tranche amounts...")
    scored = attach_buy_dates(scored, signal_data, config, today)

    # Compute deploy amounts per instrument based on tranche state
    all_deployments = engine2_deployments + dip_deployments
    if rotation_deployment and rotation_deployment.get("action") == "DEPLOYED":
        all_deployments.append(rotation_deployment)

    for inst in scored:
        sleeve_ts = tranche_summary.get("sleeves", {}).get(inst.sleeve, {})
        sleeve_deployed = sleeve_ts.get("total_deployed", 0)
        sleeve_budget = sleeve_ts.get("budget", 0)

        if sleeve_budget > 0 and sleeve_deployed > 0:
            # Proportional share of deployed amount
            inst.deploy_now_inr = round(inst.allocated_inr * (sleeve_deployed / sleeve_budget))
        else:
            inst.deploy_now_inr = 0

        # Determine which trigger caused this deployment
        matching_dep = next((d for d in all_deployments if d.get("sleeve") == inst.sleeve), None)
        if matching_dep:
            inst.deploy_tranche = f"Tranche {matching_dep.get('tranche','?')}"
            inst.deploy_multiplier = matching_dep.get("multiplier", 1.0)
            trigger = matching_dep.get("trigger", "")
            if trigger == "ENGINE2" and not inst.has_engine2_signal:
                pass  # Another ETF in this sleeve triggered it
            elif trigger == "ENGINE2":
                inst.buy_date = today.strftime("%d %b %Y")
                inst.buy_date_rule = f"Engine 2 BUY confirmed → deploy immediately at {inst.deploy_multiplier}×"
                inst.buy_date_source = f"Engine 2 priority override"
            elif trigger == "DIP" and not inst.has_engine2_signal:
                inst.buy_date = today.strftime("%d %b %Y")
                inst.buy_date_rule = f"Dip trigger fired → {inst.deploy_tranche} at {inst.deploy_multiplier}×"
                inst.buy_date_source = dip.reason
            elif trigger == "ROTATION":
                inst.buy_date = today.strftime("%d %b %Y")
                inst.buy_date_rule = f"Phase rotation → {inst.deploy_tranche} for {cycle_phase} ETFs"
                inst.buy_date_source = f"Engine 3 phase change"

        # Apply per-ETF special rules
        if live_scores:
            adj_mult, adj_note = tm._adjust_etf_multiplier(inst.ticker, inst.deploy_multiplier, config, live_scores)
            if adj_mult == 0:
                inst.deploy_now_inr = 0
                inst.buy_date_rule = f"SKIP: {adj_note}"
            elif adj_mult != inst.deploy_multiplier:
                inst.deploy_now_inr = round(inst.deploy_now_inr * adj_mult / inst.deploy_multiplier) if inst.deploy_multiplier > 0 else 0
                inst.deploy_multiplier = adj_mult

        ls = live_scores.get(inst.ticker)
        log.info(f"  {inst.ticker}: score={inst.composite} alloc=Rs.{inst.allocated_inr:,.0f} "
                 f"deploy=Rs.{inst.deploy_now_inr:,.0f} [{inst.deploy_tranche or 'PENDING'}] "
                 f"buy={inst.buy_date} {'ENG2✓' if inst.has_engine2_signal else 'MACRO'}"
                 f"{f' live={ls.pct}%' if ls else ''}")

    # ── Step 12: Exit actions ─────────────────────────────────────────────────
    log.info("STEP 12/13 — Exit actions...")
    exits = compute_exit_actions(alloc_plan, classified, signal_data, config, today)

    for sig in rotation_signals:
        if sig.action == "EXIT":
            matching = next((h for h in classified if h["ticker"].upper() == sig.ticker.upper()), None)
            if matching:
                from orchestrator.engine.exit_advisor import ExitAction
                from orchestrator.engine.buy_date_resolver import resolve_exit_date
                ei = resolve_exit_date(today, config)
                exits.append(ExitAction(
                    ticker=sig.ticker, sleeve="Thematic", exit_type="ROTATION",
                    reason=sig.reason, units_held=matching.get("quantity",0),
                    units_to_exit=matching.get("quantity",0),
                    exit_value=matching.get("current_value",0),
                    avg_cost=matching.get("avg_cost_price",0),
                    current_price=matching.get("last_price",0),
                    pnl=matching.get("pnl",0), pnl_pct=matching.get("pnl_pct",0),
                    tax_note="Equity ETF — STCG 20%/<1yr | LTCG 12.5%/>1yr",
                    drift_pct=None, suggested_date=ei["date"], date_rule=ei["rule"],
                ))

    # ── Assemble result ───────────────────────────────────────────────────────
    result = {
        "meta": {
            "run_at": datetime.now().isoformat(), "run_date": today.isoformat(),
            "sip_amount": sip_amount, "cycle_phase": cycle_phase,
            "macro_score": macro_data.get("score"), "macro_momentum": macro_data.get("momentum"),
            "macro_confidence": macro_data.get("confidence"),
            "signal_date": signal_data.get("signal_date"),
            "next_signal_run": signal_data.get("next_run_date"),
            "portfolio_value": weights["total_value"],
            "total_allocated": alloc_plan.total_allocated,
            "cycle_boost": alloc_plan.cycle_boost_applied,
            "dip_condition": dip.trigger_type, "dip_reason": dip.reason,
            "dry_run": dry_run, "pipeline_version": "v4",
        },
        "sleeve_status": {
            name: {
                "label":s.label,"target_pct":s.target_pct,"current_pct":s.current_pct,
                "drift_pct":s.drift_pct,"status":s.status,
                "sip_allocation":s.sip_allocation,"allocation_rule":s.allocation_rule,
                "current_value":s.current_value,
                "holdings":[{"ticker":h["ticker"],"quantity":h.get("quantity",0),
                    "avg_cost":h.get("avg_cost_price",0),"last_price":h.get("last_price",0),
                    "current_value":h.get("current_value",0),"pnl_pct":h.get("pnl_pct",0),
                    "asset_type":h.get("asset_type","EQUITY"),"company_name":h.get("company_name",""),
                } for h in s.holdings],
            } for name, s in alloc_plan.sleeves.items()
        },
        "execution_plan": [{
            "ticker":i.ticker,"sleeve":i.sleeve,"tag":i.tag,"stance":i.stance,
            "price":i.price,"rsi":i.rsi,"mom_4w":i.mom_4w,
            "composite":i.composite,"confidence":i.confidence,
            "macro_score":i.macro_score,"signal_score":i.signal_score,
            "rsi_score":i.rsi_score,"mom_score":i.mom_score,
            "allocated_inr":i.allocated_inr,
            "deploy_now_inr":i.deploy_now_inr,
            "deploy_tranche":i.deploy_tranche or "PENDING",
            "deploy_multiplier":i.deploy_multiplier,
            "buy_date":i.buy_date,"buy_date_rule":i.buy_date_rule,
            "buy_date_source":i.buy_date_source or "",
            "has_engine2_signal":i.has_engine2_signal,
            "engine2_strategy":i.engine2_strategy,
            "live_score_pct":live_scores[i.ticker].pct if i.ticker in live_scores else None,
            "live_signal":live_scores[i.ticker].signal if i.ticker in live_scores else None,
        } for i in scored],
        "exit_actions": [{
            "ticker":e.ticker,"sleeve":e.sleeve,"exit_type":e.exit_type,
            "reason":e.reason,"units_held":e.units_held,"units_to_exit":e.units_to_exit,
            "exit_value":e.exit_value,"avg_cost":e.avg_cost,"current_price":e.current_price,
            "pnl":e.pnl,"pnl_pct":e.pnl_pct,"tax_note":e.tax_note,
            "drift_pct":e.drift_pct,"suggested_date":e.suggested_date,"date_rule":e.date_rule,
        } for e in exits],
        "live_scores": scores_to_dict(live_scores),
        "thematic_rotation": {
            "phase_changed": any(s.action in ("EXIT","ENTER") for s in rotation_signals),
            "signals": rotation_to_dict(rotation_signals),
        },
        "tranche_deployment": {
            "dip_condition": {"type":dip.trigger_type,"reason":dip.reason,"rsi":dip.rsi,"vix":dip.vix},
            "engine2_deployments": engine2_deployments,
            "dip_deployments": [{"sleeve":d["sleeve"],"tranche":d["tranche"],
                "actual":d.get("actual",0),"multiplier":d.get("multiplier",1)} for d in dip_deployments],
            "rotation_deployment": rotation_deployment,
            "monthly_summary": tranche_summary,
        },
        "macro_signal": macro_data,
        "signal_engine": signal_data,
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    save_json(result, f"execution_plan_{ts}", OUTPUTS_DIR)
    save_json(result, "latest_execution_plan", OUTPUTS_DIR)

    # ── Step 13: Email ────────────────────────────────────────────────────────
    log.info("STEP 13/13 — Email...")
    if not dry_run:
        try: send_execution_plan_email(result, config); log.info("  Email sent ✓")
        except Exception as e: log.warning(f"  Email failed: {e}")

    log.info("=" * 70)
    log.info(f"DONE {len(scored)} instruments | Rs.{tranche_summary.get('grand_total_deployed',0):,.0f} deployed | {len(exits)} exits")
    log.info("=" * 70)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sip", type=float)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    r = run(sip_amount=a.sip, dry_run=a.dry_run)
    print(json.dumps(r["meta"], indent=2, default=str))
