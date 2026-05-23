"""
orchestrator/main.py
─────────────────────
8-step SIP orchestration pipeline.

HOW IT WORKS (plain English):
  Step 1  Load config from allocation_config.json
  Step 2  Connect to Gmail via IMAP → find Engine 3 email → parse it
  Step 3  Connect to Gmail via IMAP → find Engine 2 email → parse it
  Step 4  Call Upstox API → fetch live holdings (uses Engine 2's token)
  Step 5  Classify each holding into a sleeve → compute current weights
  Step 6  Compare weights vs targets → compute SIP split per sleeve
  Step 7  Score each eligible ETF (macro × signal × RSI × momentum)
  Step 8  Assign buy dates → compute exit actions → write JSON output

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


# ── Config & SIP amount ───────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_sip_amount() -> float | None:
    """Read SIP amount saved by the dashboard."""
    if SIP_CFG_PATH.exists():
        with open(SIP_CFG_PATH) as f:
            data = json.load(f)
        return float(data.get("sip_amount", 0)) or None
    return None


def save_sip_amount(amount: float):
    """Persist SIP amount so dashboard and GitHub Actions share the same value."""
    SIP_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIP_CFG_PATH, "w") as f:
        json.dump({"sip_amount": amount, "updated_at": datetime.now().isoformat()}, f, indent=2)


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

    # Resolve SIP amount
    if not sip_amount:
        sip_amount = load_sip_amount()
    if not sip_amount:
        raise ValueError(
            "SIP amount not provided. Either:\n"
            "  1. Pass via CLI: --sip 50000\n"
            "  2. Set it in the Streamlit dashboard (persists to data/inputs/sip_config.json)"
        )

    today = datetime.now().date()

    log.info("=" * 60)
    log.info(f"SIP ORCHESTRATOR  |  ₹{sip_amount:,.0f}  |  {today}  |  dry_run={dry_run}")
    log.info("=" * 60)

    # ── Step 1 & 2: Engine 3 macro email ─────────────────────────────────────
    log.info("STEP 1/8 — Engine 3 macro email (Gmail IMAP)...")
    macro_data = None
    if not dry_run:
        raw = fetch_latest_macro_email(config)
        if raw:
            macro_data = parse_macro_email(raw)
            save_json(macro_data, "macro_signal", INPUTS_DIR)

    if not macro_data:
        log.warning("Live fetch skipped or failed — loading cached macro data...")
        macro_data = load_cached("macro_signal")
        if not macro_data:
            raise RuntimeError(
                "No macro data available. Run live (remove --dry-run) or "
                "ensure data/inputs/macro_signal.json exists from a prior sync."
            )
    log.info(f"  Phase: {macro_data.get('phase')}  Score: {macro_data.get('score')}  Momentum: {macro_data.get('momentum')}")

    # ── Step 3: Engine 2 signal email ────────────────────────────────────────
    log.info("STEP 2/8 — Engine 2 signal email (Gmail IMAP)...")
    signal_data = None
    if not dry_run:
        raw = fetch_latest_signal_email(config)
        if raw:
            signal_data = parse_signal_email(raw)
            save_json(signal_data, "signal_engine", INPUTS_DIR)

    if not signal_data:
        log.warning("Live fetch skipped or failed — loading cached signal data...")
        signal_data = load_cached("signal_engine")
    if not signal_data:
        log.warning("No signal data found — proceeding with empty signals.")
        signal_data = {"buy_signals":[],"sell_signals":[],"urgent_alerts":[],
                       "next_run_date":None,"signal_date":None,"strategies_run":[]}
    log.info(f"  BUY: {len(signal_data.get('buy_signals',[]))}  SELL: {len(signal_data.get('sell_signals',[]))}  Alerts: {len(signal_data.get('urgent_alerts',[]))}")

    # ── Step 4: Upstox holdings ───────────────────────────────────────────────
    log.info("STEP 3/8 — Upstox holdings (API v2 using Engine 2 token)...")
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
        log.warning("No holdings data — proceeding with empty portfolio.")
        snapshot = {"holdings":[], "total_value":0.0, "as_of":datetime.now().isoformat()}
    log.info(f"  Portfolio: ₹{snapshot.get('total_value',0):,.0f}  Holdings: {len(snapshot.get('holdings',[]))}")

    # ── Step 5: Classify holdings + compute sleeve weights ───────────────────
    log.info("STEP 4/8 — Classifying holdings into sleeves...")
    classified = classify_holdings(snapshot.get("holdings",[]), config)
    weights    = compute_portfolio_weights(classified)

    # ── Step 6: Sleeve drift + SIP split ─────────────────────────────────────
    log.info("STEP 5/8 — Computing sleeve drift and SIP allocation...")
    cycle_phase = macro_data.get("phase", "UNKNOWN")
    alloc_plan  = compute_sip_allocation(sip_amount, weights, config, cycle_phase)
    for sleeve, s in alloc_plan.sleeves.items():
        log.info(f"  {sleeve}: {s.current_pct:.1f}%/{s.target_pct}% drift={s.drift_pct:+.1f}% → ₹{s.sip_allocation:,.0f} [{s.status}]")

    # ── Step 7: Score instruments + attach buy dates ──────────────────────────
    log.info("STEP 6/8 — Scoring eligible ETFs...")
    scored = score_instruments(macro_data, signal_data, alloc_plan, config)

    log.info("STEP 7/8 — Resolving buy dates...")
    scored = attach_buy_dates(scored, signal_data, config, today)
    for inst in scored:
        log.info(f"  {inst.ticker}: score={inst.composite} ₹{inst.allocated_inr:,.0f} buy={inst.buy_date} [{'ENG2✓' if inst.has_engine2_signal else 'MACRO'}]")

    # ── Step 8: Exit actions + write output ───────────────────────────────────
    log.info("STEP 8/8 — Computing exit actions...")
    exits = compute_exit_actions(alloc_plan, classified, signal_data, config, today)

    # Assemble result
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
            "dry_run":          dry_run,
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
            "buy_date":            i.buy_date,
            "buy_date_rule":       i.buy_date_rule,
            "buy_date_source":     i.buy_date_source,
            "has_engine2_signal":  i.has_engine2_signal,
            "engine2_strategy":    i.engine2_strategy,
            "engine2_strategy_id": i.engine2_strategy_id,
            "engine2_conditions":  i.engine2_conditions,
            "engine2_ssf50":       i.engine2_ssf50,
            "engine2_rsi_weekly":  i.engine2_rsi_weekly,
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
        "macro_signal":  macro_data,
        "signal_engine": signal_data,
    }

    # Write outputs
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    save_json(result, f"execution_plan_{ts}", OUTPUTS_DIR)
    save_json(result, "latest_execution_plan", OUTPUTS_DIR)

    log.info("=" * 60)
    log.info(f"DONE  {len(scored)} instruments  ₹{alloc_plan.total_allocated:,.0f} deployed  {len(exits)} exits")
    log.info("=" * 60)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SIP Orchestrator")
    parser.add_argument("--sip",     type=float, help="Monthly SIP amount in ₹")
    parser.add_argument("--dry-run", action="store_true", help="Use cached data, skip live API")
    args   = parser.parse_args()
    result = run(sip_amount=args.sip, dry_run=args.dry_run)
    print(json.dumps(result["meta"], indent=2, default=str))
