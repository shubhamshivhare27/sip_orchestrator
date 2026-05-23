"""
orchestrator/engine/allocation_engine.py
─────────────────────────────────────────
Sleeve drift calculator and SIP splitter.
Implements Section 3 rules from the Hybrid SIP Framework document exactly.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("allocation_engine")


@dataclass
class SleeveStatus:
    name:            str
    label:           str
    target_pct:      float
    current_pct:     float
    current_value:   float
    drift_pct:       float
    status:          str        # STOP | BOOST | ON_TRACK
    sip_allocation:  float
    allocation_rule: str
    holdings:        list = field(default_factory=list)


@dataclass
class AllocationPlan:
    total_portfolio_value: float
    total_sip_amount:      float
    total_allocated:       float
    sleeves:               dict
    run_date:              str
    cycle_phase:           str
    cycle_boost_applied:   bool
    cycle_boost_sleeve:    str | None
    cycle_boost_amount:    float


def classify_holdings(holdings: list[dict], config: dict) -> list[dict]:
    """Map each Upstox holding to a sleeve via config instrument lists."""
    sleeve_map = {}
    for sleeve, scfg in config["sleeves"].items():
        for t in scfg["instruments"]:
            sleeve_map[t.upper()] = sleeve

    return [{
        **h,
        "sleeve": sleeve_map.get(h["ticker"].upper(), "Core")
    } for h in holdings]


def compute_portfolio_weights(classified: list[dict]) -> dict:
    total    = sum(h["current_value"] for h in classified)
    by_sleeve = {s: {"value": 0.0, "holdings": []} for s in ["Core","Tactical","Thematic","Hedge"]}

    for h in classified:
        s = h["sleeve"]
        if s not in by_sleeve:
            by_sleeve[s] = {"value": 0.0, "holdings": []}
        by_sleeve[s]["value"]    += h["current_value"]
        by_sleeve[s]["holdings"].append(h)

    for s in by_sleeve:
        by_sleeve[s]["weight_pct"] = round(by_sleeve[s]["value"] / total * 100, 2) if total else 0.0

    return {"total_value": round(total, 2), "by_sleeve": by_sleeve}


def compute_sip_allocation(
    sip_amount:  float,
    weights:     dict,
    config:      dict,
    cycle_phase: str = "UNKNOWN",
) -> AllocationPlan:
    """
    Framework priority rules (Section 3):
      1. Overweight > 5%    → Stop SIP entirely
      2. Underweight > 3%   → Aggressive allocation (65% of SIP proportionally)
      3. Cycle phase boost  → +4% extra to Tactical in EXPANSION phases
      4. On track           → Normal proportional allocation from remainder
    """
    rules        = config["sleeve_rules"]
    agg_thr      = rules["underweight_aggressive_threshold_pct"]
    stop_thr     = rules["overweight_stop_threshold_pct"]
    uw_share     = rules["underweight_budget_share"]
    boost_pct    = rules["cycle_boost_pct"]
    boost_phases = rules["cycle_boost_phases"]
    boost_sleeve = rules["cycle_boost_sleeve"]
    by_sleeve    = weights["by_sleeve"]

    drifts = {
        s: round(by_sleeve.get(s, {}).get("weight_pct", 0.0) - scfg["target_pct"], 2)
        for s, scfg in config["sleeves"].items()
    }

    overweight  = {s: d for s, d in drifts.items() if d > stop_thr}
    underweight = dict(sorted(
        {s: d for s, d in drifts.items() if d < -agg_thr}.items(),
        key=lambda x: x[1]
    ))
    normal = {s: d for s, d in drifts.items() if s not in overweight and s not in underweight}

    allocs = {}
    rules_text = {}

    # Rule 1: Stop overweight
    for s in overweight:
        allocs[s] = 0.0
        rules_text[s] = f"PAUSED — overweight +{drifts[s]:.1f}% exceeds +{stop_thr}% threshold"

    # Rule 2: Boost underweight
    remaining = sip_amount
    if underweight:
        uw_budget   = sip_amount * uw_share
        total_drift = sum(abs(d) for d in underweight.values())
        for s, d in underweight.items():
            allocs[s] = round((abs(d) / total_drift) * uw_budget)
            remaining -= allocs[s]
            rules_text[s] = f"AGGRESSIVE — underweight {d:.1f}%, priority share {abs(d)/total_drift*100:.0f}% of underweight budget"

    # Rule 4: Normal allocation from remainder
    if normal:
        total_normal_target = sum(config["sleeves"][s]["target_pct"] for s in normal)
        for s in normal:
            allocs[s] = round((config["sleeves"][s]["target_pct"] / total_normal_target) * remaining)
            rules_text[s] = f"NORMAL — drift {drifts[s]:+.1f}%, within ±{agg_thr}% tolerance"

    # Rule 3: Cycle boost
    boost_applied = False
    boost_amount  = 0.0
    if cycle_phase in boost_phases and drifts.get(boost_sleeve, 0) < 0:
        boost_amount  = round(sip_amount * boost_pct / 100)
        allocs[boost_sleeve] = allocs.get(boost_sleeve, 0) + boost_amount
        boost_applied = True
        rules_text[boost_sleeve] = rules_text.get(boost_sleeve,"") + f" | +₹{boost_amount:,} cycle boost ({cycle_phase})"

    total_allocated = sum(allocs.values())

    sleeve_statuses = {}
    for sleeve, scfg in config["sleeves"].items():
        current_pct  = by_sleeve.get(sleeve, {}).get("weight_pct", 0.0)
        status = "STOP" if sleeve in overweight else "BOOST" if sleeve in underweight else "ON_TRACK"
        sleeve_statuses[sleeve] = SleeveStatus(
            name=sleeve, label=scfg["label"],
            target_pct=scfg["target_pct"], current_pct=current_pct,
            current_value=by_sleeve.get(sleeve, {}).get("value", 0.0),
            drift_pct=drifts[sleeve], status=status,
            sip_allocation=allocs.get(sleeve, 0.0),
            allocation_rule=rules_text.get(sleeve, ""),
            holdings=by_sleeve.get(sleeve, {}).get("holdings", []),
        )

    log.info(f"Allocation: ₹{sip_amount:,} → deployed ₹{total_allocated:,} | boost={boost_applied} | stopped={list(overweight.keys())}")
    return AllocationPlan(
        total_portfolio_value=weights["total_value"], total_sip_amount=sip_amount,
        total_allocated=total_allocated, sleeves=sleeve_statuses,
        run_date=datetime.now().strftime("%Y-%m-%d"), cycle_phase=cycle_phase,
        cycle_boost_applied=boost_applied,
        cycle_boost_sleeve=boost_sleeve if boost_applied else None,
        cycle_boost_amount=boost_amount,
    )
