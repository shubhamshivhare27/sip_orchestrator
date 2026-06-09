"""
orchestrator/engine/tranche_manager.py  (v4)
──────────────────────────────────────────────
Per-sleeve, per-ETF tranche deployment with weekly lock.

ARCHITECTURE:
  Layer 1: Sleeve allocation (60/20/15/5) → drift-adjusted budget per sleeve
  Layer 2: Each sleeve's budget → Tranche A(50%) + B(30%) + C(20%)
  Layer 3: Trigger determines WHEN and at WHAT multiplier
  Layer 4: Instrument scoring decides HOW MUCH per ETF within sleeve

TRIGGERS (priority order):
  1. Engine 2 BUY signal   → override even PAUSED sleeves, borrow from underweight
  2. Engine 3 phase change → deploy Thematic sleeve's next tranche
  3. Market dip (RSI/VIX)  → deploy available tranches per sleeve
  4. 3rd Thursday fallback → Tranche A at 1× if still undeployed

RULES:
  - Max 1 tranche deployment per sleeve per week
  - Per-ETF special rules (GOLDBEES fixed, MOMOMENTUM skip, etc.)
  - Unused tranches carry forward to next month
  - Engine 2 overrides PAUSED sleeve status
"""

import json, logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("tranche_mgr")

STATE_FILE = "tranche_state.json"
TRANCHE_PCTS = {"A": 0.50, "B": 0.30, "C": 0.20}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DipCondition:
    rsi: Optional[float] = None
    vix: Optional[float] = None
    nifty_vs_sma50: Optional[float] = None
    weekly_return: Optional[float] = None
    trigger_type: str = "NORMAL"
    multiplier: float = 1.0
    reason: str = ""


@dataclass
class TrancheDeployment:
    sleeve: str
    tranche: str           # A, B, C
    base_amount: float
    multiplier: float
    deployed_amount: float
    trigger: str           # ENGINE2 | ROTATION | DIP | FALLBACK
    trigger_detail: str
    deploy_date: str
    etf_allocations: dict = field(default_factory=dict)  # ticker → amount


@dataclass
class SleeveTrancheState:
    sleeve: str
    budget: float          # sleeve's SIP allocation for this month
    tranche_a: dict = field(default_factory=dict)
    tranche_b: dict = field(default_factory=dict)
    tranche_c: dict = field(default_factory=dict)
    carry_forward: float = 0.0  # from previous month


# ── State persistence ─────────────────────────────────────────────────────────

def _month_key(d: date = None) -> str:
    return (d or date.today()).strftime("%Y-%m")


def _week_key(d: date = None) -> str:
    d = d or date.today()
    # ISO week number
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def load_state(inputs_dir: Path) -> dict:
    path = inputs_dir / STATE_FILE
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(inputs_dir: Path, state: dict):
    path = inputs_dir / STATE_FILE
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _third_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_to_thu = (3 - first.weekday()) % 7
    return first + timedelta(days=days_to_thu + 14)


# ── Dip condition assessment ──────────────────────────────────────────────────

def assess_dip(rsi=None, vix=None, nifty_vs_sma50=None, weekly_return=None,
               config=None) -> DipCondition:
    """Assess market conditions using configurable thresholds."""
    thr = (config or {}).get("tranche_config", {}).get("dip_thresholds", {})
    rsi_deep = thr.get("rsi_deep", 35)
    rsi_mod_lo = thr.get("rsi_moderate_low", 35)
    rsi_mod_hi = thr.get("rsi_moderate_high", 45)
    rsi_ob = thr.get("rsi_overbought", 70)
    vix_deep = thr.get("vix_deep", 20)
    sma_deep = thr.get("sma_deep_pct", -5)
    sma_mod = thr.get("sma_moderate_pct", -3)
    sma_ob = thr.get("sma_overbought_pct", 5)
    wk_mod = thr.get("weekly_ret_moderate", -3)

    reasons = []
    is_deep = False
    if rsi is not None and rsi < rsi_deep:
        is_deep = True; reasons.append(f"RSI {rsi:.1f}<{rsi_deep}")
    if nifty_vs_sma50 is not None and nifty_vs_sma50 < sma_deep:
        is_deep = True; reasons.append(f"Index {nifty_vs_sma50:.1f}% below SMA50")
    if vix is not None and vix > vix_deep:
        is_deep = True; reasons.append(f"VIX {vix:.1f}>{vix_deep}")

    if is_deep:
        return DipCondition(rsi=rsi, vix=vix, nifty_vs_sma50=nifty_vs_sma50,
                           weekly_return=weekly_return, trigger_type="DEEP_DIP",
                           multiplier=2.0, reason="DEEP DIP: " + " | ".join(reasons))

    is_mod = False
    if rsi is not None and rsi_mod_lo <= rsi < rsi_mod_hi:
        is_mod = True; reasons.append(f"RSI {rsi:.1f} in {rsi_mod_lo}-{rsi_mod_hi}")
    if nifty_vs_sma50 is not None and sma_deep <= nifty_vs_sma50 < sma_mod:
        is_mod = True; reasons.append(f"Index {nifty_vs_sma50:.1f}% below SMA50")
    if weekly_return is not None and weekly_return < wk_mod:
        is_mod = True; reasons.append(f"Weekly {weekly_return:.1f}%")

    if is_mod:
        return DipCondition(rsi=rsi, vix=vix, nifty_vs_sma50=nifty_vs_sma50,
                           weekly_return=weekly_return, trigger_type="MODERATE_DIP",
                           multiplier=1.5, reason="MODERATE DIP: " + " | ".join(reasons))

    is_ob = False
    if rsi is not None and rsi > rsi_ob:
        is_ob = True; reasons.append(f"RSI {rsi:.1f}>{rsi_ob}")
    if nifty_vs_sma50 is not None and nifty_vs_sma50 > sma_ob:
        is_ob = True; reasons.append(f"Index {nifty_vs_sma50:.1f}% above SMA50")

    if is_ob:
        return DipCondition(rsi=rsi, vix=vix, nifty_vs_sma50=nifty_vs_sma50,
                           weekly_return=weekly_return, trigger_type="OVERBOUGHT",
                           multiplier=0.5, reason="OVERBOUGHT: " + " | ".join(reasons))

    return DipCondition(rsi=rsi, vix=vix, nifty_vs_sma50=nifty_vs_sma50,
                       weekly_return=weekly_return, trigger_type="NORMAL",
                       multiplier=1.0, reason=f"Normal: RSI={rsi or 'N/A'}")


# ── Per-ETF multiplier adjustment ─────────────────────────────────────────────

def _adjust_etf_multiplier(ticker: str, base_mult: float, config: dict,
                           live_scores: dict = None) -> tuple:
    """Apply per-ETF special rules from config. Returns (adjusted_mult, rule_note)."""
    rules = config.get("tranche_config", {}).get("etf_special_rules", {})
    rule = rules.get(ticker, {})
    if not rule:
        return base_mult, ""

    mode = rule.get("mode", "normal")

    if mode == "skip_unless_golden_cross":
        # Check if Golden Cross is active from live scores
        if live_scores and ticker in live_scores:
            ls = live_scores[ticker]
            gc_ind = next((i for i in ls.indicators if "Cross" in i.name), None)
            if gc_ind and "Golden" in gc_ind.value:
                return min(base_mult, rule.get("max_mult", 1.0)), "Golden Cross active → allowed"
        return 0.0, f"SKIP: {rule.get('note','Death Cross — skip mode')}"

    if mode == "capped":
        cap = rule.get("max_mult", base_mult)
        if base_mult > cap:
            return cap, f"Capped at {cap}× ({rule.get('note','')})"
        return base_mult, ""

    if mode == "low_priority":
        default = rule.get("default_mult", 0.5)
        cap = rule.get("max_mult", 1.5)
        return min(max(default, base_mult * 0.5), cap), f"Low priority: {default}× default"

    if mode == "fixed":
        return rule.get("max_mult", 1.0), f"Fixed SIP: {rule.get('note','')}"

    return base_mult, ""


# ── Core deployment logic ─────────────────────────────────────────────────────

def get_sleeve_tranche_status(sleeve: str, sip_budget: float, state: dict,
                              month: str) -> dict:
    """Get deployment status for a sleeve's 3 tranches."""
    ms = state.get(month, {}).get(sleeve, {})
    carry = state.get("carry_forward", {}).get(sleeve, 0)
    result = {"budget": sip_budget + carry, "carry_forward": carry}
    for t_name, t_pct in TRANCHE_PCTS.items():
        t_state = ms.get(t_name, {})
        base = (sip_budget + carry) * t_pct
        result[t_name] = {
            "deployed": t_state.get("deployed", False),
            "base": round(base, 2),
            "multiplier": t_state.get("multiplier", 1.0),
            "actual": round(base * t_state.get("multiplier", 1.0), 2) if t_state.get("deployed") else 0,
            "trigger": t_state.get("trigger", ""),
            "trigger_detail": t_state.get("trigger_detail", ""),
            "date": t_state.get("date", ""),
            "week": t_state.get("week", ""),
        }
    result["total_deployed"] = sum(result[t]["actual"] for t in "ABC" if result[t]["deployed"])
    result["remaining"] = sum(1 for t in "ABC" if not result[t]["deployed"])
    return result


def deploy_tranche(sleeve: str, tranche: str, sip_budget: float,
                   multiplier: float, trigger: str, trigger_detail: str,
                   state: dict, inputs_dir: Path, today: date = None) -> dict:
    """Record a tranche deployment. Returns the deployment record."""
    today = today or date.today()
    month = _month_key(today)
    week = _week_key(today)

    if month not in state:
        state[month] = {}
    if sleeve not in state[month]:
        state[month][sleeve] = {}

    carry = state.get("carry_forward", {}).get(sleeve, 0)
    base = (sip_budget + carry) * TRANCHE_PCTS[tranche]
    actual = round(base * multiplier, 2)

    state[month][sleeve][tranche] = {
        "deployed": True,
        "base": round(base, 2),
        "multiplier": multiplier,
        "actual": actual,
        "trigger": trigger,
        "trigger_detail": trigger_detail,
        "date": today.isoformat(),
        "week": week,
    }

    save_state(inputs_dir, state)
    log.info(f"Deployed {sleeve}/{tranche}: Rs.{actual:,.0f} ({multiplier}x) — {trigger}: {trigger_detail}")
    return state[month][sleeve][tranche]


def _can_deploy_this_week(sleeve: str, state: dict, today: date, config: dict) -> bool:
    """Check if this sleeve already deployed a tranche this week."""
    month = _month_key(today)
    week = _week_key(today)
    max_per_week = config.get("tranche_config", {}).get("max_deployments_per_sleeve_per_week", 1)

    ms = state.get(month, {}).get(sleeve, {})
    deployed_this_week = sum(1 for t in "ABC" if ms.get(t, {}).get("week") == week)
    return deployed_this_week < max_per_week


def _next_undeployed(sleeve: str, state: dict, month: str) -> str:
    """Find next undeployed tranche (A → B → C)."""
    ms = state.get(month, {}).get(sleeve, {})
    for t in ["A", "B", "C"]:
        if not ms.get(t, {}).get("deployed", False):
            return t
    return None


# ── High-level deployment functions ───────────────────────────────────────────

def deploy_for_dip(sleeve_budgets: dict, dip: DipCondition, state: dict,
                   config: dict, inputs_dir: Path, today: date = None) -> list:
    """
    Called by weekly sync. Deploys next available tranche per eligible sleeve.
    Returns list of deployment records.
    """
    today = today or date.today()
    month = _month_key(today)
    deployments = []

    for sleeve, budget in sleeve_budgets.items():
        if budget <= 0:
            continue
        if not _can_deploy_this_week(sleeve, state, today, config):
            log.info(f"  {sleeve}: already deployed this week — skip")
            continue

        tranche = _next_undeployed(sleeve, state, month)
        if not tranche:
            log.info(f"  {sleeve}: all tranches deployed this month")
            continue

        # Check 3rd Thursday fallback
        third_thu = _third_thursday(today.year, today.month)
        is_fallback = (today >= third_thu and tranche == "A"
                      and dip.trigger_type in ("NORMAL", "OVERBOUGHT"))

        if dip.trigger_type in ("DEEP_DIP", "MODERATE_DIP") or is_fallback:
            mult = dip.multiplier if not is_fallback else 1.0
            trigger = "FALLBACK" if is_fallback else "DIP"
            detail = f"3rd Thursday fallback at 1x" if is_fallback else dip.reason

            rec = deploy_tranche(sleeve, tranche, budget, mult, trigger, detail,
                                state, inputs_dir, today)
            deployments.append({"sleeve": sleeve, "tranche": tranche, **rec})

    return deployments


def deploy_for_engine2(ticker: str, sleeve: str, sleeve_budgets: dict,
                       alloc_plan, state: dict, config: dict,
                       inputs_dir: Path, today: date = None) -> dict:
    """
    Called when Engine 2 fires a BUY signal.
    Priority 1: overrides even PAUSED sleeves.
    Borrows budget from largest underweight sleeve if needed.
    """
    today = today or date.today()
    month = _month_key(today)
    e2_cfg = config.get("tranche_config", {}).get("engine2_override", {})

    if not e2_cfg.get("enabled", True):
        return {"action": "DISABLED"}

    budget = sleeve_budgets.get(sleeve, 0)

    # If sleeve is PAUSED (overweight), borrow from largest underweight
    if budget <= 0 and e2_cfg.get("override_paused_sleeve", True):
        # Find largest underweight sleeve
        drifts = {}
        for s_name, s_obj in alloc_plan.sleeves.items():
            if s_obj.drift_pct < 0 and s_name != sleeve:
                drifts[s_name] = s_obj.drift_pct
        if drifts:
            donor = min(drifts, key=drifts.get)  # most underweight
            budget = sleeve_budgets.get(donor, 0) * 0.5  # borrow 50% of donor's budget
            log.info(f"  Engine 2 override: {sleeve} PAUSED → borrowing Rs.{budget:,.0f} from {donor}")

    if budget <= 0:
        return {"action": "NO_BUDGET"}

    tranche = _next_undeployed(sleeve, state, month)
    if not tranche:
        return {"action": "ALL_DEPLOYED"}

    rec = deploy_tranche(sleeve, tranche, budget, 1.5, "ENGINE2",
                        f"Engine 2 BUY: {ticker} — priority override",
                        state, inputs_dir, today)
    return {"action": "DEPLOYED", "sleeve": sleeve, "tranche": tranche,
            "ticker": ticker, **rec}


def deploy_for_rotation(new_phase: str, config: dict, sleeve_budgets: dict,
                        state: dict, inputs_dir: Path,
                        today: date = None) -> dict:
    """
    Called when Engine 3 phase changes.
    Deploys Thematic sleeve's next tranche for the new rotation ETFs.
    """
    today = today or date.today()
    month = _month_key(today)
    budget = sleeve_budgets.get("Thematic", 0)

    if budget <= 0:
        return {"action": "NO_BUDGET"}

    tranche = _next_undeployed("Thematic", state, month)
    if not tranche:
        return {"action": "ALL_DEPLOYED"}

    rec = deploy_tranche("Thematic", tranche, budget, 1.0, "ROTATION",
                        f"Phase changed to {new_phase} — deploy for new cycle ETFs",
                        state, inputs_dir, today)
    return {"action": "DEPLOYED", "tranche": tranche, "phase": new_phase, **rec}


# ── Month-end carry forward ───────────────────────────────────────────────────

def process_carry_forward(sleeve_budgets: dict, state: dict, inputs_dir: Path,
                          today: date = None):
    """Called at month end. Unused B/C tranches carry forward to next month."""
    today = today or date.today()
    month = _month_key(today)
    carry = {}

    for sleeve, budget in sleeve_budgets.items():
        ms = state.get(month, {}).get(sleeve, {})
        undeployed = 0
        for t_name, t_pct in TRANCHE_PCTS.items():
            if t_name == "A":
                continue  # Tranche A doesn't carry (should have been fallback'd)
            if not ms.get(t_name, {}).get("deployed", False):
                undeployed += budget * t_pct
        if undeployed > 0:
            carry[sleeve] = round(undeployed, 2)
            log.info(f"Carry forward: {sleeve} Rs.{undeployed:,.0f} to next month")

    state["carry_forward"] = carry
    save_state(inputs_dir, state)
    return carry


# ── Summary ───────────────────────────────────────────────────────────────────

def get_monthly_summary(sleeve_budgets: dict, state: dict,
                        today: date = None) -> dict:
    month = _month_key(today or date.today())
    summary = {"month": month, "sleeves": {}}

    for sleeve, budget in sleeve_budgets.items():
        carry = state.get("carry_forward", {}).get(sleeve, 0)
        total_budget = budget + carry
        s_state = state.get(month, {}).get(sleeve, {})
        tranches = {}
        total_deployed = 0

        for t_name, t_pct in TRANCHE_PCTS.items():
            t = s_state.get(t_name, {})
            base = total_budget * t_pct
            actual = t.get("actual", 0) if t.get("deployed") else 0
            total_deployed += actual
            tranches[t_name] = {
                "pct": t_pct, "base": round(base, 2),
                "deployed": t.get("deployed", False),
                "actual": actual,
                "multiplier": t.get("multiplier", 1.0),
                "trigger": t.get("trigger", ""),
                "trigger_detail": t.get("trigger_detail", ""),
                "date": t.get("date", ""),
            }

        summary["sleeves"][sleeve] = {
            "budget": round(total_budget, 2),
            "carry_forward": carry,
            "total_deployed": round(total_deployed, 2),
            "remaining": sum(1 for t in tranches.values() if not t["deployed"]),
            "tranches": tranches,
        }

    summary["grand_total_deployed"] = round(
        sum(s["total_deployed"] for s in summary["sleeves"].values()), 2
    )
    return summary
