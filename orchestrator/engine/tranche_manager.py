"""
orchestrator/engine/tranche_manager.py
───────────────────────────────────────
Tranche-based Dip-SIP deployment system.

FRAMEWORK (from ETF Dashboard v4):
  Monthly SIP split into 3 tranches:
    Tranche A: 50% of monthly SIP
    Tranche B: 30% of monthly SIP
    Tranche C: 20% of monthly SIP

  Each tranche deploys when a DIP TRIGGER fires:
    DEEP DIP:    RSI < 35 OR index down >5% from SMA50  → deploy at 2.0×
    MODERATE:    RSI 35-45 OR index down 3-5%            → deploy at 1.5×
    NORMAL:      RSI 45-65, no special conditions        → deploy at 1.0×
    OVERBOUGHT:  RSI > 70 OR price >5% above SMA50      → deploy at 0.5× or skip

  FALLBACK: If no dip fires by 3rd Thursday of month → deploy Tranche A at 1.0×

  Max 3 SIP purchases per ETF per month.

HOW IT WORKS IN THE ORCHESTRATOR:
  - Weekly sync (Saturday 8 AM) calls check_tranche_triggers()
  - If a trigger fires, it records the tranche deployment in data/inputs/tranche_state.json
  - Monthly run reads tranche state and adjusts allocations by the multiplier
  - Email notification sent when a tranche trigger fires

LIVE DATA USED:
  - RSI from Upstox/yfinance (same as live_scorer.py)
  - Nifty 50 price vs SMA50 (weekly return check)
  - India VIX (fear gauge)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("tranche_manager")


# ── Tranche config ────────────────────────────────────────────────────────────

TRANCHE_A_PCT = 0.50   # 50% of monthly SIP
TRANCHE_B_PCT = 0.30   # 30%
TRANCHE_C_PCT = 0.20   # 20%

MULTIPLIERS = {
    "DEEP_DIP":    2.0,   # RSI < 35 or index down >5%
    "MODERATE_DIP":1.5,   # RSI 35-45 or index down 3-5%
    "NORMAL":      1.0,   # RSI 45-65
    "OVERBOUGHT":  0.5,   # RSI > 70 or price >5% above SMA50
}

TRANCHE_STATE_FILE = "tranche_state.json"


@dataclass
class TrancheStatus:
    tranche:       str      # A, B, C
    pct_of_sip:    float    # 0.50, 0.30, 0.20
    deployed:      bool
    deploy_date:   Optional[str]
    trigger_type:  Optional[str]   # DEEP_DIP, MODERATE_DIP, NORMAL, OVERBOUGHT
    multiplier:    float
    amount_base:   float    # base amount before multiplier
    amount_actual: float    # actual deployed = base × multiplier


@dataclass
class DipCondition:
    rsi:              Optional[float]
    nifty_vs_sma50:   Optional[float]   # % deviation from SMA50
    weekly_return:    Optional[float]   # % return this week
    vix:              Optional[float]
    trigger_type:     str               # DEEP_DIP | MODERATE_DIP | NORMAL | OVERBOUGHT
    multiplier:       float
    reason:           str


# ── Dip condition checker ─────────────────────────────────────────────────────

def assess_dip_condition(
    rsi: float = None,
    nifty_vs_sma50: float = None,
    weekly_return: float = None,
    vix: float = None,
) -> DipCondition:
    """
    Assess current market conditions and determine which tranche trigger applies.
    """
    reasons = []

    # Check for DEEP DIP
    is_deep = False
    if rsi is not None and rsi < 35:
        is_deep = True
        reasons.append(f"RSI {rsi:.1f} < 35 (oversold)")
    if nifty_vs_sma50 is not None and nifty_vs_sma50 < -5:
        is_deep = True
        reasons.append(f"Nifty {nifty_vs_sma50:.1f}% below SMA50")
    if vix is not None and vix > 25:
        is_deep = True
        reasons.append(f"VIX {vix:.1f} > 25 (high fear)")

    if is_deep:
        return DipCondition(rsi=rsi, nifty_vs_sma50=nifty_vs_sma50,
                           weekly_return=weekly_return, vix=vix,
                           trigger_type="DEEP_DIP", multiplier=2.0,
                           reason="DEEP DIP — " + " | ".join(reasons))

    # Check for MODERATE DIP
    is_moderate = False
    if rsi is not None and 35 <= rsi < 45:
        is_moderate = True
        reasons.append(f"RSI {rsi:.1f} in 35-45 range")
    if nifty_vs_sma50 is not None and -5 <= nifty_vs_sma50 < -3:
        is_moderate = True
        reasons.append(f"Nifty {nifty_vs_sma50:.1f}% below SMA50")
    if weekly_return is not None and weekly_return < -3:
        is_moderate = True
        reasons.append(f"Weekly return {weekly_return:.1f}%")

    if is_moderate:
        return DipCondition(rsi=rsi, nifty_vs_sma50=nifty_vs_sma50,
                           weekly_return=weekly_return, vix=vix,
                           trigger_type="MODERATE_DIP", multiplier=1.5,
                           reason="MODERATE DIP — " + " | ".join(reasons))

    # Check for OVERBOUGHT
    is_overbought = False
    if rsi is not None and rsi > 70:
        is_overbought = True
        reasons.append(f"RSI {rsi:.1f} > 70 (overbought)")
    if nifty_vs_sma50 is not None and nifty_vs_sma50 > 5:
        is_overbought = True
        reasons.append(f"Nifty {nifty_vs_sma50:.1f}% above SMA50")

    if is_overbought:
        return DipCondition(rsi=rsi, nifty_vs_sma50=nifty_vs_sma50,
                           weekly_return=weekly_return, vix=vix,
                           trigger_type="OVERBOUGHT", multiplier=0.5,
                           reason="OVERBOUGHT — " + " | ".join(reasons) + " | Deploy at 0.5× or skip")

    # NORMAL conditions
    return DipCondition(rsi=rsi, nifty_vs_sma50=nifty_vs_sma50,
                       weekly_return=weekly_return, vix=vix,
                       trigger_type="NORMAL", multiplier=1.0,
                       reason=f"Normal conditions — RSI {rsi:.1f if rsi else 'N/A'}, deploy at 1.0×")


# ── Tranche state management ─────────────────────────────────────────────────

def _load_state(inputs_dir: Path) -> dict:
    path = inputs_dir / TRANCHE_STATE_FILE
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _save_state(inputs_dir: Path, state: dict):
    path = inputs_dir / TRANCHE_STATE_FILE
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def _current_month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def _third_thursday(year: int, month: int) -> date:
    """Calculate 3rd Thursday of the given month."""
    first_day = date(year, month, 1)
    # Find first Thursday
    days_until_thursday = (3 - first_day.weekday()) % 7
    first_thursday = first_day + timedelta(days=days_until_thursday)
    # 3rd Thursday = first + 14 days
    return first_thursday + timedelta(days=14)


# ── Main tranche functions ────────────────────────────────────────────────────

def get_tranche_status(sip_amount: float, inputs_dir: Path) -> list[TrancheStatus]:
    """
    Get current month's tranche deployment status.
    Returns list of 3 TrancheStatus objects (A, B, C).
    """
    state     = _load_state(inputs_dir)
    month_key = _current_month_key()
    month_state = state.get(month_key, {})

    tranches = []
    for name, pct in [("A", TRANCHE_A_PCT), ("B", TRANCHE_B_PCT), ("C", TRANCHE_C_PCT)]:
        t_state = month_state.get(name, {})
        base    = sip_amount * pct
        tranches.append(TrancheStatus(
            tranche       = name,
            pct_of_sip    = pct,
            deployed      = t_state.get("deployed", False),
            deploy_date   = t_state.get("deploy_date"),
            trigger_type  = t_state.get("trigger_type"),
            multiplier    = t_state.get("multiplier", 1.0),
            amount_base   = base,
            amount_actual = base * t_state.get("multiplier", 1.0) if t_state.get("deployed") else 0,
        ))

    return tranches


def deploy_tranche(
    tranche_name: str,
    sip_amount: float,
    dip: DipCondition,
    inputs_dir: Path,
) -> TrancheStatus:
    """
    Deploy a specific tranche (A, B, or C) with the given dip condition.
    Records deployment in tranche_state.json.
    """
    state     = _load_state(inputs_dir)
    month_key = _current_month_key()

    if month_key not in state:
        state[month_key] = {}

    pct_map = {"A": TRANCHE_A_PCT, "B": TRANCHE_B_PCT, "C": TRANCHE_C_PCT}
    pct     = pct_map.get(tranche_name, 0)
    base    = sip_amount * pct
    actual  = base * dip.multiplier

    state[month_key][tranche_name] = {
        "deployed":     True,
        "deploy_date":  datetime.now().strftime("%Y-%m-%d"),
        "trigger_type": dip.trigger_type,
        "multiplier":   dip.multiplier,
        "amount_base":  base,
        "amount_actual": actual,
        "reason":       dip.reason,
    }

    _save_state(inputs_dir, state)

    log.info(f"Tranche {tranche_name} deployed: ₹{actual:,.0f} ({dip.multiplier}× of ₹{base:,.0f}) — {dip.trigger_type}")

    return TrancheStatus(
        tranche=tranche_name, pct_of_sip=pct, deployed=True,
        deploy_date=datetime.now().strftime("%Y-%m-%d"),
        trigger_type=dip.trigger_type, multiplier=dip.multiplier,
        amount_base=base, amount_actual=actual,
    )


def check_and_deploy(
    sip_amount: float,
    rsi: float = None,
    nifty_vs_sma50: float = None,
    weekly_return: float = None,
    vix: float = None,
    inputs_dir: Path = None,
    today: date = None,
) -> dict:
    """
    Called by weekly sync (Saturday 8 AM).
    Checks dip conditions and deploys the next available tranche if triggered.

    Returns dict with deployment info for email notification.
    """
    if today is None:
        today = datetime.now().date()
    if inputs_dir is None:
        inputs_dir = Path("data/inputs")

    dip = assess_dip_condition(rsi, nifty_vs_sma50, weekly_return, vix)
    tranches = get_tranche_status(sip_amount, inputs_dir)

    # Find next undeployed tranche
    next_tranche = None
    for t in tranches:
        if not t.deployed:
            next_tranche = t
            break

    if next_tranche is None:
        return {
            "action": "ALL_DEPLOYED",
            "message": "All 3 tranches already deployed this month.",
            "dip_condition": dip.trigger_type,
        }

    # Check 3rd Thursday fallback
    third_thu = _third_thursday(today.year, today.month)
    is_past_fallback = today >= third_thu

    # Deploy if: dip trigger fires OR past 3rd Thursday (fallback for Tranche A)
    should_deploy = False
    deploy_reason = ""

    if dip.trigger_type in ("DEEP_DIP", "MODERATE_DIP"):
        should_deploy = True
        deploy_reason = f"Dip trigger fired: {dip.reason}"
    elif dip.trigger_type == "NORMAL" and next_tranche.tranche == "A" and is_past_fallback:
        should_deploy = True
        deploy_reason = f"3rd Thursday fallback ({third_thu.strftime('%d %b')}) — deploying Tranche A at 1.0×"
    elif dip.trigger_type == "OVERBOUGHT":
        should_deploy = False
        deploy_reason = f"Market overbought ({dip.reason}) — holding tranches"

    if should_deploy:
        deployed = deploy_tranche(next_tranche.tranche, sip_amount, dip, inputs_dir)
        return {
            "action":        "DEPLOYED",
            "tranche":       deployed.tranche,
            "amount_base":   deployed.amount_base,
            "amount_actual": deployed.amount_actual,
            "multiplier":    deployed.multiplier,
            "trigger_type":  deployed.trigger_type,
            "reason":        deploy_reason,
            "dip_condition": dip.reason,
            "deploy_date":   deployed.deploy_date,
        }
    else:
        return {
            "action":        "HOLD",
            "next_tranche":  next_tranche.tranche,
            "reason":        deploy_reason or f"No dip trigger. Conditions: {dip.reason}",
            "dip_condition": dip.reason,
            "fallback_date": third_thu.strftime("%d %b %Y"),
        }


def get_monthly_summary(sip_amount: float, inputs_dir: Path) -> dict:
    """Summary of all tranche deployments for the current month."""
    tranches = get_tranche_status(sip_amount, inputs_dir)
    total_deployed = sum(t.amount_actual for t in tranches if t.deployed)
    total_base     = sum(t.amount_base for t in tranches)

    return {
        "month":          _current_month_key(),
        "sip_amount":     sip_amount,
        "total_base":     total_base,
        "total_deployed": total_deployed,
        "effective_multiplier": round(total_deployed / total_base, 2) if total_base > 0 else 0,
        "tranches": [{
            "name":          t.tranche,
            "pct":           t.pct_of_sip,
            "deployed":      t.deployed,
            "deploy_date":   t.deploy_date,
            "trigger_type":  t.trigger_type,
            "multiplier":    t.multiplier,
            "amount_base":   t.amount_base,
            "amount_actual": t.amount_actual,
        } for t in tranches],
        "remaining_tranches": sum(1 for t in tranches if not t.deployed),
    }
