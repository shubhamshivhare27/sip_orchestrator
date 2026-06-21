"""
orchestrator/engine/tranche_manager.py — v5 (Priority 2 rebuild)
══════════════════════════════════════════════════════════════════════════════
Rebuilds tranche deployment around PER-ETF permanent structural rules
(state summary §7.2) instead of one universal RSI threshold for every ETF.

WHAT THIS MODULE OWNS
  1. Universal dip-severity classification (§7.1) — assess_dip()
  2. A(50%)/B(30%)/C(20%) tranche framework with a hard weekly-lock per sleeve
  3. The 13 per-ETF permanent rules (§7.2) — apply_etf_structural_rules()
  4. Engine 2 priority override with sleeve-borrowing (§7.5) — deploy_for_engine2()
  5. Thematic phase-rotation funding (§7.4) — deploy_for_rotation()
  6. 3rd-Thursday-style fallbacks, evaluated PER ETF on its own fallback date,
     not just once at the sleeve level
  7. Carry-forward: unused B/C tranches roll into next month's sleeve budget;
     MOMOMENTUM's skipped allocations accumulate until Golden Cross fires

INTEGRATION CONTRACT WITH main.py
  Functions below are called exactly as main.py's v4 pipeline already invokes
  them (load_state, deploy_for_rotation, deploy_for_engine2, DipCondition,
  assess_dip, deploy_for_dip, _third_thursday, get_monthly_summary) — EXCEPT
  for one necessary change: `_adjust_etf_multiplier(ticker, mult, config,
  live_scores)` is replaced by `apply_etf_structural_rules(...)`, because the
  old 4-argument signature has no way to (a) know today's date for fallback
  triggers, (b) know per-ETF purchase counts for MAFANG's 2-tranche cap, or
  (c) FORCE a deployment when the sleeve itself didn't trigger this run (e.g.
  GOLDBEES on its fixed 2nd Monday, or MID150BEES the moment Golden Cross
  fires). A 4-arg wrapper cannot do any of that — it can only ever scale an
  amount that's already non-zero. See the accompanying main.py patch.

KNOWN OPEN ITEM (carried over from the previous session, still unresolved):
  live_scorer.py was never attached to this conversation, so the exact
  indicator NAME strings/VALUE formats for Golden Cross, PE percentile, and
  52-week-high distance are best-guess fuzzy matches below (see _find_indicator
  helpers). "RSI Zone" is confirmed exact (used verbatim in main.py already).
  Verify the others once live_scorer.py is shared.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "tranche_state.json"

# ── §7.1 Universal tranche framework ────────────────────────────────────────
TRANCHE_SPLIT = {"A": 0.50, "B": 0.30, "C": 0.20}
TRANCHE_ORDER = ["A", "B", "C"]

ALL_SLEEVES = ["Core", "International", "Thematic", "Hedge"]

# ── §7.2 Per-ETF permanent structural rules (13 ETFs) ───────────────────────
# mode values: normal | conservative | conditional_aggressive | aggressive |
#              skip | conditional_accumulate | capped | low_priority |
#              reduced | fixed
ETF_RULES: dict[str, dict[str, Any]] = {
    "NIFTYIETF":  {"mode": "normal",                 "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "3rd_thursday"},
    "JUNIORBEES": {"mode": "conservative",            "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "3rd_thursday"},
    "MID150BEES": {"mode": "conditional_aggressive",  "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "1st_monday"},
    "QUAL30IETF": {"mode": "aggressive",              "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "1st_working_day"},
    "MOMOMENTUM": {"mode": "skip",                    "default_mult": 0.0,  "max_mult": 1.0,  "fallback": None},
    "MON100":     {"mode": "conditional_accumulate",  "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "3rd_thursday"},
    "MOUS500":    {"mode": "normal",                  "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "1st_monday_after_jobs"},
    "MAFANG":     {"mode": "capped",                  "default_mult": 0.75, "max_mult": 1.5,  "fallback": "3rd_thursday", "max_tranches": 2},
    "HSET":       {"mode": "low_priority",             "default_mult": 0.5,  "max_mult": 1.5,  "fallback": "last_thursday"},
    "GOLDBEES":   {"mode": "fixed",                   "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "2nd_monday"},
    "INFRABEES":  {"mode": "aggressive",              "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "1st_monday"},
    "MODEFENCE":  {"mode": "aggressive",              "default_mult": 1.0,  "max_mult": 2.0,  "fallback": "1st_monday"},
    "ITBEES":     {"mode": "reduced",                 "default_mult": 0.75, "max_mult": 1.5,  "fallback": "3rd_thursday"},
}


# ══════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════

def _default_sleeve_state() -> dict:
    # tranches_deployed: {"A": 1500.0, "B": 900.0} — letter -> amount actually
    # deployed for that tranche this month (not just which letters fired).
    # Added for the dashboard's Tranches tab (Priority 4) so it can show a
    # real per-tranche breakdown instead of just a list of used letters.
    return {"tranches_deployed": {}, "last_deploy_iso_week": None,
            "carry_forward_pct": 0.0, "deployed_inr_month": 0.0}


def _default_etf_state(today: date) -> dict:
    return {"month": today.strftime("%Y-%m"), "purchase_count_month": 0,
            "tranches_deployed": [], "carry_forward_inr": 0.0,
            "last_carry_accum_month": None}


def _default_state(today: date) -> dict:
    return {
        "current_month": today.strftime("%Y-%m"),
        "sleeves": {s: _default_sleeve_state() for s in ALL_SLEEVES},
        "etfs": {},
    }


def load_state(inputs_dir: Path) -> dict:
    path = Path(inputs_dir) / STATE_FILENAME
    today = date.today()
    if path.exists():
        try:
            with open(path) as f:
                state = json.load(f)
        except Exception as e:
            logger.warning("tranche_state.json unreadable (%s) — starting fresh", e)
            return _default_state(today)
        for s in ALL_SLEEVES:
            state.setdefault("sleeves", {}).setdefault(s, _default_sleeve_state())
        state.setdefault("etfs", {})
        # Migration: older state files stored sleeve tranches_deployed as a
        # flat list of letters (no $ amounts). Convert in place so dashboards
        # reading this don't break on existing tranche_state.json files —
        # amounts are unknown for already-deployed tranches, so they're
        # recorded as None (dashboard should show "deployed, amount unknown"
        # rather than crash or show a misleading ₹0).
        for s_state in state.get("sleeves", {}).values():
            td = s_state.get("tranches_deployed")
            if isinstance(td, list):
                s_state["tranches_deployed"] = {letter: None for letter in td}
        return _roll_month_if_needed(state, today)
    return _default_state(today)


def save_state(state: dict, inputs_dir: Path) -> None:
    path = Path(inputs_dir) / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _roll_month_if_needed(state: dict, today: date) -> dict:
    cur = today.strftime("%Y-%m")
    if state.get("current_month") == cur:
        return state
    logger.info("tranche_state: rolling over %s -> %s", state.get("current_month"), cur)
    for sleeve, s in state.get("sleeves", {}).items():
        deployed = set(s.get("tranches_deployed", []))
        unused_pct = sum(TRANCHE_SPLIT[t] for t in ("B", "C") if t not in deployed)
        s["carry_forward_pct"] = round(s.get("carry_forward_pct", 0.0) + unused_pct, 4)
        s["tranches_deployed"] = {}
        s["last_deploy_iso_week"] = None
        s["deployed_inr_month"] = 0.0
    for ticker, e in state.get("etfs", {}).items():
        e["purchase_count_month"] = 0
        e["tranches_deployed"] = []
        e["month"] = cur
        # carry_forward_inr (MOMOMENTUM-style) deliberately persists across the rollover
    state["current_month"] = cur
    return state


# ══════════════════════════════════════════════════════════════════════════
#  DATE HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """weekday: Mon=0..Sun=6. Returns the n-th occurrence of that weekday in the month."""
    cal = calendar.Calendar()
    days = [d for d in cal.itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    return days[n - 1]


def _last_weekday(year: int, month: int, weekday: int) -> date:
    cal = calendar.Calendar()
    days = [d for d in cal.itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    return days[-1]


def _third_thursday(year: int, month: int) -> date:
    return _nth_weekday(year, month, 3, 3)


def _second_monday(year: int, month: int) -> date:
    return _nth_weekday(year, month, 0, 2)


def _last_thursday(year: int, month: int) -> date:
    return _last_weekday(year, month, 3)


def _first_working_day(year: int, month: int) -> date:
    """First Mon-Fri of the month. Does NOT account for NSE market holidays — flagged."""
    cal = calendar.Calendar()
    for d in cal.itermonthdates(year, month):
        if d.month == month and d.weekday() < 5:
            return d
    raise ValueError("no working day found")


def _first_monday_after_us_jobs(year: int, month: int) -> date:
    """
    US Non-Farm Payrolls is released the first Friday of the month (BLS schedule).
    Approximation — does not account for the occasional BLS calendar shift
    (holiday weeks etc.). Flagged for verification.
    """
    first_friday = _nth_weekday(year, month, 4, 1)
    return first_friday + timedelta(days=3)  # Fri -> following Monday


FALLBACK_DATE_FN = {
    "3rd_thursday":           _third_thursday,
    "1st_monday":             lambda y, m: _nth_weekday(y, m, 0, 1),
    "1st_working_day":        _first_working_day,
    "2nd_monday":             _second_monday,
    "last_thursday":          _last_thursday,
    "1st_monday_after_jobs":  _first_monday_after_us_jobs,
}


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ══════════════════════════════════════════════════════════════════════════
#  §7.1 — DIP ASSESSMENT
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class DipCondition:
    trigger_type: str = "NONE"            # DEEP_DIP | MODERATE_DIP | NORMAL | OVERBOUGHT | NONE
    multiplier: float = 1.0
    reason: str = "No dip data available"
    rsi: float | None = None
    vix: float | None = None
    nifty_vs_sma50: float | None = None
    weekly_return: float | None = None


def assess_dip(rsi=None, vix=None, nifty_vs_sma50=None, weekly_return=None, config=None) -> DipCondition:
    """
    §7.1 universal dip table. Each available signal independently votes for a
    severity tier; the MOST SEVERE tier with at least one supporting signal
    wins (i.e. a single strong-dip signal is enough to trigger 2x — signals
    are OR'd, not required to all agree). This is a judgment call since the
    framework doesn't specify AND vs OR; flag if you want AND-based instead.
    """
    if rsi is None and vix is None and nifty_vs_sma50 is None and weekly_return is None:
        return DipCondition(trigger_type="NONE", multiplier=1.0, reason="No live indicator data available")

    votes: list[tuple[int, str, float, str]] = []

    if rsi is not None:
        if rsi < 35:
            votes.append((3, "DEEP_DIP", 2.0, f"RSI {rsi:.1f} < 35"))
        elif rsi < 45:
            votes.append((2, "MODERATE_DIP", 1.5, f"RSI {rsi:.1f} in 35-45"))
        elif rsi <= 65:
            votes.append((1, "NORMAL", 1.0, f"RSI {rsi:.1f} in 45-65"))
        elif rsi > 70:
            votes.append((0, "OVERBOUGHT", 0.5, f"RSI {rsi:.1f} > 70"))

    if weekly_return is not None:
        if weekly_return < -5:
            votes.append((3, "DEEP_DIP", 2.0, f"Weekly return {weekly_return:.1f}% < -5%"))
        elif weekly_return < -3:
            votes.append((2, "MODERATE_DIP", 1.5, f"Weekly return {weekly_return:.1f}% in -3..-5%"))
        elif abs(weekly_return) <= 1:
            votes.append((1, "NORMAL", 1.0, f"Weekly return {weekly_return:.1f}% flat"))
        elif weekly_return > 4:
            votes.append((0, "OVERBOUGHT", 0.5, f"Weekly return {weekly_return:.1f}% > +4%"))

    if vix is not None:
        if vix > 20:
            votes.append((3, "DEEP_DIP", 2.0, f"VIX {vix:.1f} > 20"))
        elif vix >= 16:
            votes.append((2, "MODERATE_DIP", 1.5, f"VIX {vix:.1f} in 16-20"))
        elif vix >= 12:
            votes.append((1, "NORMAL", 1.0, f"VIX {vix:.1f} in 12-16"))
        elif vix < 10:
            votes.append((0, "OVERBOUGHT", 0.5, f"VIX {vix:.1f} < 10"))

    if nifty_vs_sma50 is not None:
        if nifty_vs_sma50 < -4:
            votes.append((3, "DEEP_DIP", 2.0, f"Price {nifty_vs_sma50:.1f}% below SMA50"))
        elif nifty_vs_sma50 < -1:
            votes.append((2, "MODERATE_DIP", 1.5, f"Price {nifty_vs_sma50:.1f}% below SMA50"))
        elif abs(nifty_vs_sma50) <= 1:
            votes.append((1, "NORMAL", 1.0, "Price near SMA50"))
        elif nifty_vs_sma50 > 5:
            votes.append((0, "OVERBOUGHT", 0.5, f"Price {nifty_vs_sma50:.1f}% above SMA50"))

    if not votes:
        return DipCondition(trigger_type="NONE", multiplier=1.0,
                             reason="Indicators present but none in any defined zone",
                             rsi=rsi, vix=vix, nifty_vs_sma50=nifty_vs_sma50, weekly_return=weekly_return)

    votes.sort(key=lambda v: -v[0])
    rank, label, mult, why = votes[0]
    return DipCondition(trigger_type=label, multiplier=mult, reason=why,
                         rsi=rsi, vix=vix, nifty_vs_sma50=nifty_vs_sma50, weekly_return=weekly_return)


# ══════════════════════════════════════════════════════════════════════════
#  TRANCHE PROGRESSION HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _sleeve_locked_this_week(sleeve_state: dict, today: date) -> bool:
    return sleeve_state.get("last_deploy_iso_week") == _iso_week(today)


def _next_pending_tranche(sleeve_state: dict) -> str | None:
    deployed = set(sleeve_state.get("tranches_deployed", []))
    for t in TRANCHE_ORDER:
        if t not in deployed:
            return t
    return None


# ══════════════════════════════════════════════════════════════════════════
#  SLEEVE-LEVEL DEPLOYMENT TRIGGERS
# ══════════════════════════════════════════════════════════════════════════

def deploy_for_dip(sleeve_budgets: dict, dip: DipCondition, tranche_state: dict,
                    config: dict, inputs_dir: Path, today: date) -> list[dict]:
    """
    Sleeve-level dip-triggered deployment. Hedge is excluded — it runs on
    GOLDBEES's fixed schedule, not dip-timing (insurance, not returns).
    Cumulative spend per sleeve is capped at its (carry-forward-adjusted)
    monthly budget, so a DEEP_DIP 2x on Tranche A can still legitimately
    consume the whole month's budget in one shot (that's the framework's
    intent for a severe dip) but can no longer cause LATER tranches that
    same month to push total spend past 100% of budget — this was a
    previously-flagged open item, now fixed by tracking deployed_inr_month.
    """
    deployments = []
    for sleeve in (s for s in ALL_SLEEVES if s != "Hedge"):
        budget = sleeve_budgets.get(sleeve, 0)
        if budget <= 0:
            continue
        s_state = tranche_state["sleeves"].setdefault(sleeve, _default_sleeve_state())
        if _sleeve_locked_this_week(s_state, today):
            continue
        tranche = _next_pending_tranche(s_state)
        if tranche is None:
            continue

        carry = s_state.get("carry_forward_pct", 0.0)
        effective_budget = budget * (1 + carry)
        deployed_so_far = s_state.get("deployed_inr_month", 0.0)
        remaining = max(effective_budget - deployed_so_far, 0)
        raw_amount = effective_budget * TRANCHE_SPLIT[tranche] * dip.multiplier
        amount = round(min(raw_amount, remaining), 2)

        s_state["tranches_deployed"][tranche] = amount
        s_state["last_deploy_iso_week"] = _iso_week(today)
        s_state["deployed_inr_month"] = round(deployed_so_far + amount, 2)
        if tranche == "A" and carry > 0:
            s_state["carry_forward_pct"] = 0.0

        deployments.append({
            "sleeve": sleeve, "tranche": tranche, "actual": amount,
            "multiplier": dip.multiplier, "trigger": "DIP", "action": "DEPLOYED",
            "dip_type": dip.trigger_type, "reason": dip.reason,
        })
        logger.info("DIP deploy: %s Tranche %s Rs.%.0f (%.1fx) - %s",
                    sleeve, tranche, amount, dip.multiplier, dip.reason)

    save_state(tranche_state, Path(inputs_dir))
    return deployments


def deploy_for_engine2(ticker: str, sleeve: str, sleeve_budgets: dict, alloc_plan,
                        tranche_state: dict, config: dict, inputs_dir: Path, today: date) -> dict:
    """
    §7.5 — Engine 2 BUY has highest priority. Deploys immediately regardless
    of sleeve STOP/PAUSED status or weekly lock (a confirmed quant entry
    overrides portfolio-balance timing for one month). If the sleeve's own
    SIP budget is ~0 (overweight/STOP), borrows 50% from the largest
    UNDERWEIGHT sleeve instead, per the documented borrowing rule.
    """
    s_state = tranche_state["sleeves"].setdefault(sleeve, _default_sleeve_state())
    tranche = _next_pending_tranche(s_state) or "C"  # if A/B/C already used, still let Engine2 through on C
    own_budget = sleeve_budgets.get(sleeve, 0)

    borrowed_from = None
    borrowed_amount = 0.0
    deploy_budget = own_budget

    if own_budget <= 0:
        candidates = [
            (name, s.drift_pct) for name, s in alloc_plan.sleeves.items()
            if name != sleeve and s.drift_pct < 0
        ]
        if candidates:
            candidates.sort(key=lambda c: c[1])  # most negative drift = most underweight
            borrowed_from = candidates[0][0]
            lender_budget = sleeve_budgets.get(borrowed_from, 0)
            borrowed_amount = round(lender_budget * 0.50, 2)
            deploy_budget = borrowed_amount
            logger.info("Engine2 BUY on %s: sleeve %s is STOP - borrowing Rs.%.0f (50%%) from %s",
                        ticker, sleeve, borrowed_amount, borrowed_from)
        else:
            logger.warning("Engine2 BUY on %s: sleeve %s is STOP and no underweight sleeve to borrow from", ticker, sleeve)
            deploy_budget = 0

    amount = round(deploy_budget * TRANCHE_SPLIT[tranche], 2)
    s_state["tranches_deployed"][tranche] = round(s_state["tranches_deployed"].get(tranche, 0.0) + amount, 2)
    s_state["last_deploy_iso_week"] = _iso_week(today)
    s_state["deployed_inr_month"] = round(s_state.get("deployed_inr_month", 0.0) + amount, 2)
    save_state(tranche_state, Path(inputs_dir))

    return {
        "action": "DEPLOYED" if amount > 0 else "NO_BUDGET",
        "tranche": tranche, "multiplier": 1.0, "actual": amount, "trigger": "ENGINE2",
        "borrowed_from": borrowed_from, "borrowed_amount": borrowed_amount,
    }


def deploy_for_rotation(cycle_phase: str, config: dict, sleeve_budgets: dict,
                         tranche_state: dict, inputs_dir: Path, today: date) -> dict:
    """
    Thematic EXIT/ENTER phase change -> fund the new lineup immediately with
    the next pending Thematic tranche, so a rotation isn't left unfunded
    until the next dip/fallback trigger.
    """
    sleeve = "Thematic"
    s_state = tranche_state["sleeves"].setdefault(sleeve, _default_sleeve_state())
    budget = sleeve_budgets.get(sleeve, 0)
    if budget <= 0:
        return {"action": "NO_BUDGET", "sleeve": sleeve, "tranche": None, "multiplier": 1.0, "actual": 0, "trigger": "ROTATION"}

    if _sleeve_locked_this_week(s_state, today):
        return {"action": "WEEKLY_LOCK", "sleeve": sleeve, "tranche": None, "multiplier": 1.0, "actual": 0, "trigger": "ROTATION"}

    tranche = _next_pending_tranche(s_state)
    if tranche is None:
        return {"action": "ALL_TRANCHES_USED", "sleeve": sleeve, "tranche": "C", "multiplier": 1.0, "actual": 0, "trigger": "ROTATION"}

    deployed_so_far = s_state.get("deployed_inr_month", 0.0)
    remaining = max(budget - deployed_so_far, 0)
    amount = round(min(budget * TRANCHE_SPLIT[tranche], remaining), 2)

    s_state["tranches_deployed"][tranche] = amount
    s_state["last_deploy_iso_week"] = _iso_week(today)
    s_state["deployed_inr_month"] = round(deployed_so_far + amount, 2)
    save_state(tranche_state, Path(inputs_dir))

    return {"action": "DEPLOYED", "sleeve": sleeve, "tranche": tranche, "multiplier": 1.0,
            "actual": amount, "trigger": "ROTATION", "phase": cycle_phase}


def get_monthly_summary(sleeve_budgets: dict, tranche_state: dict, today: date) -> dict:
    tranche_state = _roll_month_if_needed(tranche_state, today)
    sleeves_out = {}
    grand_total = 0.0
    for sleeve in ALL_SLEEVES:
        budget = sleeve_budgets.get(sleeve, 0)
        s_state = tranche_state.get("sleeves", {}).get(sleeve, _default_sleeve_state())
        deployed = s_state.get("deployed_inr_month", 0.0)
        breakdown = dict(s_state.get("tranches_deployed", {}))
        sleeves_out[sleeve] = {
            "budget": budget,
            "total_deployed": deployed,
            "remaining": round(max(budget - deployed, 0), 2),
            "tranches_deployed": list(breakdown.keys()),
            "tranche_breakdown": breakdown,  # {"A": 1500.0, "B": None (carried over from old state), ...}
            "pct_deployed": round(deployed / budget * 100, 1) if budget > 0 else 0.0,
            "carry_forward_pct": s_state.get("carry_forward_pct", 0.0),
        }
        grand_total += deployed
    return {"sleeves": sleeves_out, "grand_total_deployed": round(grand_total, 2),
            "month": tranche_state.get("current_month")}


# ══════════════════════════════════════════════════════════════════════════
#  INDICATOR EXTRACTION (defensive — see KNOWN OPEN ITEM at top of file)
# ══════════════════════════════════════════════════════════════════════════

def _find_indicator(ls_obj, *name_keywords: str):
    if not ls_obj or not getattr(ls_obj, "indicators", None):
        return None
    for kw in name_keywords:
        for ind in ls_obj.indicators:
            if kw.lower() in str(getattr(ind, "name", "")).lower():
                return ind
    return None


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_golden_cross(ls_obj) -> bool | None:
    ind = _find_indicator(ls_obj, "golden", "death", "cross")
    if ind is None:
        return None
    val = str(getattr(ind, "value", "")).strip().upper()
    if val in ("GOLDEN", "GOLDEN CROSS", "BULLISH", "TRUE", "YES", "1"):
        return True
    if val in ("DEATH", "DEATH CROSS", "BEARISH", "FALSE", "NO", "0"):
        return False
    f = _safe_float(val)
    return f > 0 if f is not None else None


def _rsi_value(ls_obj) -> float | None:
    ind = _find_indicator(ls_obj, "rsi zone", "rsi")
    return _safe_float(getattr(ind, "value", None)) if ind else None


def _pe_percentile_low(ls_obj) -> bool | None:
    ind = _find_indicator(ls_obj, "pe ratio", "pe ")
    if ind is None:
        return None
    val = str(getattr(ind, "value", "")).upper()
    if any(w in val for w in ("LOW", "CHEAP", "UNDERVALUED")):
        return True
    if any(w in val for w in ("HIGH", "EXPENSIVE", "OVERVALUED")):
        return False
    f = _safe_float(val)
    return (f <= 25) if f is not None else None  # heuristic absolute PE threshold — verify vs real output


def _pct_below_52w_high(ls_obj) -> float | None:
    ind = _find_indicator(ls_obj, "support", "resistance", "52w", "52-week")
    if ind is None:
        return None
    return _safe_float(getattr(ind, "value", None))  # assumed signed %, negative = below 52W high


# ══════════════════════════════════════════════════════════════════════════
#  §7.2 — PER-ETF STRUCTURAL RULES (the actual Priority-2 deliverable)
# ══════════════════════════════════════════════════════════════════════════

def _result(amount: float, mult: float, tranche: str | None, note: str, skip: bool) -> dict:
    return {"deploy_inr": round(max(amount, 0), 2), "multiplier": mult, "tranche": tranche, "note": note, "skip": skip}


def _commit(etf_state: dict, amount: float, mult: float, tranche: str | None, note: str) -> dict:
    etf_state["purchase_count_month"] = etf_state.get("purchase_count_month", 0) + 1
    etf_state.setdefault("tranches_deployed", []).append(tranche)
    return _result(amount, mult, tranche, note, False)


def apply_etf_structural_rules(
    ticker: str,
    allocated_inr: float,
    sleeve_proportional_inr: float,
    sleeve_multiplier: float,
    deploy_tranche_letter: str | None,
    config: dict,
    live_scores: dict,
    tranche_state: dict,
    today: date,
) -> dict:
    """
    Applies the per-ETF rule (if one exists for `ticker`) on top of whatever
    the sleeve-level trigger (dip / rotation / Engine2) already computed.

    Returns {"deploy_inr", "multiplier", "tranche", "note", "skip"} — this
    REPLACES (not scales) the instrument's final deploy_now_inr for this run,
    because several rules (Golden Cross firing, fixed-date GOLDBEES, fallback
    days) must be able to trigger a purchase even when the sleeve itself
    produced zero this run — a pure multiplier-scaling function cannot do that.
    """
    rule = ETF_RULES.get(ticker)
    if rule is None:
        return _result(sleeve_proportional_inr, sleeve_multiplier, deploy_tranche_letter,
                        "", sleeve_proportional_inr <= 0)

    etf_state = tranche_state.setdefault("etfs", {}).setdefault(ticker, _default_etf_state(today))
    cur_month = today.strftime("%Y-%m")
    if etf_state.get("month") != cur_month:
        etf_state["purchase_count_month"] = 0
        etf_state["tranches_deployed"] = []
        etf_state["month"] = cur_month

    ls_obj = (live_scores or {}).get(ticker)
    mode = rule["mode"]
    default_mult = rule["default_mult"]
    max_mult = rule["max_mult"]
    max_tranches = rule.get("max_tranches", 3)

    if etf_state["purchase_count_month"] >= max_tranches:
        return _result(0, 0, None, f"{ticker}: monthly purchase cap ({max_tranches}) reached", True)

    # ── MOMOMENTUM — skip entirely until Golden Cross, then deploy ALL carry-forward ──
    if mode == "skip":
        golden = _is_golden_cross(ls_obj)
        if golden is True:
            total = round(etf_state.get("carry_forward_inr", 0.0) + allocated_inr, 2)
            etf_state["carry_forward_inr"] = 0.0
            return _commit(etf_state, total, 1.0, "A",
                            f"{ticker}: Golden Cross confirmed - deploying full carry-forward Rs.{total:,.0f}")
        if etf_state.get("last_carry_accum_month") != cur_month:
            etf_state["carry_forward_inr"] = round(etf_state.get("carry_forward_inr", 0.0) + allocated_inr, 2)
            etf_state["last_carry_accum_month"] = cur_month
        return _result(0, 0, None,
                        f"{ticker}: SKIP - no Golden Cross yet (carry-forward Rs.{etf_state['carry_forward_inr']:,.0f})", True)

    # ── GOLDBEES — fixed 2nd-Monday schedule, rare panic-dip override ──────
    if mode == "fixed":
        is_2nd_monday = today == _second_monday(today.year, today.month)
        panic = sleeve_multiplier >= 2.0  # stand-in for ">5% weekly gold drop" until a gold-specific signal exists
        if not is_2nd_monday and not panic:
            return _result(0, 0, None, f"{ticker}: fixed 2nd-Monday schedule - not due yet", True)
        mult = 2.0 if panic else 1.0
        amount = round(allocated_inr * mult, 2)
        return _commit(etf_state, amount, mult, "A",
                        f"{ticker}: {'panic-dip 2x' if panic else 'fixed 2nd Monday'} deploy")

    mult = default_mult
    force_now = False
    force_inr: float | None = None
    tranche_label = deploy_tranche_letter

    if mode == "normal":
        mult = max(0.0, min(sleeve_multiplier, max_mult)) if sleeve_proportional_inr > 0 else default_mult

    elif mode == "conservative":  # JUNIORBEES — 30-40% of normal, wait for real dips
        if sleeve_multiplier > 1.0:
            mult = min(sleeve_multiplier, max_mult)
        else:
            mult = default_mult * 0.35

    elif mode == "conditional_aggressive":  # MID150BEES — Golden Cross -> deploy A immediately
        if _is_golden_cross(ls_obj) is True and etf_state["purchase_count_month"] == 0:
            force_now, force_inr, tranche_label, mult = True, round(allocated_inr * TRANCHE_SPLIT["A"], 2), "A", 1.0
        else:
            mult = max(0.0, min(sleeve_multiplier, max_mult)) if sleeve_proportional_inr > 0 else default_mult

    elif mode == "aggressive" and ticker == "QUAL30IETF":  # low PE percentile -> deploy A+B (80%) now
        if _pe_percentile_low(ls_obj) is True and etf_state["purchase_count_month"] == 0:
            force_now, force_inr, tranche_label, mult = True, round(allocated_inr * 0.80, 2), "A+B", 1.0
        else:
            mult = max(0.0, min(sleeve_multiplier, max_mult)) if sleeve_proportional_inr > 0 else default_mult

    elif mode == "aggressive":  # INFRABEES, MODEFENCE — overweight conviction -> deploy A+B (75%)
        signal = str(getattr(ls_obj, "signal", "")).upper() if ls_obj else ""
        if signal in ("STRONG BUY", "BUY") and etf_state["purchase_count_month"] == 0:
            force_now, force_inr, tranche_label, mult = True, round(allocated_inr * 0.75, 2), "A+B", 1.0
        else:
            mult = max(0.0, min(sleeve_multiplier, max_mult)) if sleeve_proportional_inr > 0 else default_mult

    elif mode == "conditional_accumulate":  # MON100 — >15% below 52W high -> 1.5x accumulation
        dist = _pct_below_52w_high(ls_obj)
        accumulating = dist is not None and dist <= -15
        base = 1.5 if accumulating else 1.0
        mult = max(base, min(sleeve_multiplier, max_mult)) if sleeve_proportional_inr > 0 else base

    elif mode == "capped":  # MAFANG — 2 tranches only (60/40), hard cap 1.5x
        if etf_state["purchase_count_month"] >= 2:
            return _result(0, 0, None, f"{ticker}: 2-purchase cap reached for this month", True)
        if sleeve_proportional_inr > 0:
            split_pct = 0.60 if etf_state["purchase_count_month"] == 0 else 0.40
            mult = max(default_mult, min(sleeve_multiplier, max_mult))
            force_now, force_inr = True, round(allocated_inr * split_pct * mult, 2)
            tranche_label = "T1(60%)" if split_pct == 0.60 else "T2(40%)"

    elif mode == "low_priority":  # HSET — 0.5x default ALWAYS, upgrade to 1x only on RSI<30
        rsi = _rsi_value(ls_obj)
        mult = max_mult if (rsi is not None and rsi < 30) else default_mult  # ignores sleeve multiplier by design

    elif mode == "reduced":  # ITBEES — 0.75x base, 1.5x only on RSI<35 (genuine sector dip)
        rsi = _rsi_value(ls_obj)
        mult = max_mult if (rsi is not None and rsi < 35) else default_mult

    # ── Resolve final amount ────────────────────────────────────────────────
    if force_now:
        amount = force_inr or 0.0
    elif sleeve_proportional_inr > 0:
        ratio = mult / sleeve_multiplier if sleeve_multiplier > 0 else 0.0
        amount = round(sleeve_proportional_inr * ratio, 2)
    else:
        amount = 0.0
        fb_key = rule.get("fallback")
        if fb_key and etf_state["purchase_count_month"] == 0:
            fb_fn = FALLBACK_DATE_FN.get(fb_key)
            if fb_fn and today >= fb_fn(today.year, today.month):
                fb_pct = 0.60 if ticker == "MAFANG" else TRANCHE_SPLIT["A"]
                amount = round(allocated_inr * fb_pct * default_mult, 2)
                tranche_label = "A (fallback)"
                mult = default_mult

    if amount <= 0:
        return _result(0, mult, None, f"{ticker}: {mode} mode - no trigger this run", True)

    return _commit(etf_state, amount, mult, tranche_label, f"{ticker}: {mode} mode, {mult:.2f}x")


# ══════════════════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tmp = Path(tempfile.mkdtemp())
    print(f"\n=== Scratch state dir: {tmp} ===\n")

    print("--- assess_dip() severity table ---")
    for kwargs, label in [
        (dict(rsi=30, vix=22, nifty_vs_sma50=-5, weekly_return=-6), "DEEP_DIP expected"),
        (dict(rsi=40, vix=18), "MODERATE_DIP expected"),
        (dict(rsi=55, vix=14), "NORMAL expected"),
        (dict(rsi=75, vix=8), "OVERBOUGHT expected"),
        (dict(), "NONE expected (no data)"),
    ]:
        d = assess_dip(**kwargs)
        print(f"  {label:<26} -> {d.trigger_type:<13} {d.multiplier}x  ({d.reason})")

    print("\n--- deploy_for_dip(): weekly lock + cumulative budget cap ---")
    state = _default_state(date(2026, 6, 6))
    budgets = {"Core": 6000, "International": 2000, "Thematic": 1500, "Hedge": 500}
    deep = DipCondition(trigger_type="DEEP_DIP", multiplier=2.0, reason="test deep dip")
    d1 = deploy_for_dip(budgets, deep, state, {}, tmp, date(2026, 6, 6))
    for d in d1:
        print(f"  Week1: {d['sleeve']:<14} Tranche {d['tranche']} Rs.{d['actual']:,.0f}")
    d2 = deploy_for_dip(budgets, deep, state, {}, tmp, date(2026, 6, 6))  # same week -> should be empty (locked)
    print(f"  Same-week repeat call -> {len(d2)} deployments (expect 0, weekly lock)")
    d3 = deploy_for_dip(budgets, deep, state, {}, tmp, date(2026, 6, 13))  # next week, still DEEP_DIP
    for d in d3:
        print(f"  Week2: {d['sleeve']:<14} Tranche {d['tranche']} Rs.{d['actual']:,.0f}  (capped by remaining budget)")
    summary = get_monthly_summary(budgets, state, date(2026, 6, 13))
    for sleeve, s in summary["sleeves"].items():
        if s["budget"] > 0:
            print(f"  {sleeve:<14} deployed Rs.{s['total_deployed']:,.0f} / Rs.{s['budget']:,.0f} ({s['pct_deployed']}%)  <= must never exceed 100%")

    print("\n--- apply_etf_structural_rules(): per-ETF behaviours ---")
    state2 = _default_state(date(2026, 6, 6))

    class FakeInd:
        def __init__(self, name, value): self.name, self.value = name, value

    class FakeScore:
        def __init__(self, signal="WATCH", indicators=None): self.signal, self.indicators = signal, indicators or []

    live_scores = {
        "MOMOMENTUM": FakeScore(indicators=[FakeInd("Golden/Death Cross", "DEATH")]),
        "MAFANG":     FakeScore(),
        "HSET":       FakeScore(indicators=[FakeInd("RSI Zone", 25)]),
        "QUAL30IETF": FakeScore(indicators=[FakeInd("PE Ratio vs Historical", "LOW")]),
        "MID150BEES": FakeScore(indicators=[FakeInd("Golden/Death Cross", "GOLDEN")]),
        "ITBEES":     FakeScore(indicators=[FakeInd("RSI Zone", 60)]),
        "GOLDBEES":   FakeScore(),
    }

    r = apply_etf_structural_rules("MOMOMENTUM", 3000, 0, 0, None, {}, live_scores, state2, date(2026, 6, 6))
    print(f"  MOMOMENTUM (no Golden Cross): deploy=Rs.{r['deploy_inr']:.0f} skip={r['skip']}  note='{r['note']}'")
    live_scores["MOMOMENTUM"] = FakeScore(indicators=[FakeInd("Golden/Death Cross", "GOLDEN")])
    r = apply_etf_structural_rules("MOMOMENTUM", 3000, 0, 0, None, {}, live_scores, state2, date(2026, 6, 13))
    print(f"  MOMOMENTUM (Golden Cross fires): deploy=Rs.{r['deploy_inr']:.0f}  note='{r['note']}'")

    r1 = apply_etf_structural_rules("MAFANG", 2000, 600, 1.0, "A", {}, live_scores, state2, date(2026, 6, 6))
    print(f"  MAFANG purchase 1: deploy=Rs.{r1['deploy_inr']:.0f} tranche={r1['tranche']}")
    r2 = apply_etf_structural_rules("MAFANG", 2000, 600, 1.0, "B", {}, live_scores, state2, date(2026, 6, 13))
    print(f"  MAFANG purchase 2: deploy=Rs.{r2['deploy_inr']:.0f} tranche={r2['tranche']}")
    r3 = apply_etf_structural_rules("MAFANG", 2000, 600, 1.0, "C", {}, live_scores, state2, date(2026, 6, 20))
    print(f"  MAFANG purchase 3 (should be capped at 0): deploy=Rs.{r3['deploy_inr']:.0f} skip={r3['skip']}  note='{r3['note']}'")

    r = apply_etf_structural_rules("HSET", 1000, 300, 1.0, "A", {}, live_scores, state2, date(2026, 6, 6))
    print(f"  HSET (RSI 25 < 30, sleeve trigger fired): mult={r['multiplier']}x deploy=Rs.{r['deploy_inr']:.0f}  (expect upgraded to {ETF_RULES['HSET']['max_mult']}x)")

    r = apply_etf_structural_rules("QUAL30IETF", 1500, 0, 0, None, {}, live_scores, state2, date(2026, 6, 6))
    print(f"  QUAL30IETF (low PE, no sleeve trigger): deploy=Rs.{r['deploy_inr']:.0f}  (expect 80% of 1500 = 1200)")

    r = apply_etf_structural_rules("MID150BEES", 1200, 0, 0, None, {}, live_scores, state2, date(2026, 6, 6))
    print(f"  MID150BEES (Golden Cross, no sleeve trigger): deploy=Rs.{r['deploy_inr']:.0f}  (expect 50% of 1200 = 600)")

    state3 = _default_state(date(2026, 6, 18))
    r = apply_etf_structural_rules("GOLDBEES", 400, 0, 0, None, {}, live_scores, state3, date(2026, 6, 8))  # 2nd Monday Jun 2026
    print(f"  GOLDBEES on 2nd Monday: deploy=Rs.{r['deploy_inr']:.0f}")
    r = apply_etf_structural_rules("GOLDBEES", 400, 0, 0, None, {}, live_scores, state3, date(2026, 6, 9))  # day after
    print(f"  GOLDBEES day after (already purchased this month): deploy=Rs.{r['deploy_inr']:.0f} skip={r['skip']}")

    state4 = _default_state(date(2026, 6, 26))
    r = apply_etf_structural_rules("NIFTYIETF", 2000, 0, 0, None, {}, live_scores, state4, date(2026, 6, 26))
    third_thu_2026_06 = _third_thursday(2026, 6)
    print(f"  3rd Thursday this month is {third_thu_2026_06}; NIFTYIETF on 26 Jun (no sleeve trigger all month): deploy=Rs.{r['deploy_inr']:.0f}  (expect fallback fires)")

    print("\n=== All scenarios executed without error ===")
