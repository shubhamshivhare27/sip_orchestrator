"""
orchestrator/engine/phase_rotation.py
──────────────────────────────────────
Handles thematic sleeve rotation when the business cycle phase changes.

HOW IT WORKS:
  1. Reads current phase from Engine 3 email (e.g. "MID EXPANSION")
  2. Reads previous phase from last execution plan (stored in data/inputs/last_phase.json)
  3. If phase changed: generates EXIT signals for old-phase ETFs and ENTRY signals for new-phase ETFs
  4. Total thematic allocation stays at 15%
  5. No hardcoded hold periods — purely driven by Engine 3's weekly phase output

EXAMPLE:
  Previous phase: STRONG RECOVERY → active: BANKBEES, AUTOBEES, INFRABEES
  Current phase:  MID EXPANSION   → active: ITBEES, METALBEES, MODEFENCE

  Generated signals:
    EXIT:  BANKBEES (was 6%, no longer in active list)
    EXIT:  AUTOBEES (was 5%, no longer in active list)
    KEEP:  (none overlap in this case)
    ENTER: ITBEES (new 6%)
    ENTER: METALBEES (new 5%)
    ENTER: MODEFENCE (new 4%)
    EXIT:  INFRABEES (was 4%, not in new list)
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("phase_rotation")


@dataclass
class RotationSignal:
    ticker:     str
    action:     str     # EXIT | ENTER | KEEP
    old_weight: float   # previous allocation %
    new_weight: float   # new allocation %
    reason:     str
    phase_from: str
    phase_to:   str


LAST_PHASE_FILE = "last_phase.json"


def _load_last_phase(inputs_dir: Path) -> dict:
    path = inputs_dir / LAST_PHASE_FILE
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _save_last_phase(inputs_dir: Path, phase: str, active_etfs: list[str], weights: dict):
    path = inputs_dir / LAST_PHASE_FILE
    with open(path, "w") as f:
        json.dump({
            "phase": phase,
            "active_etfs": active_etfs,
            "weights": weights,
            "updated_at": datetime.now().isoformat(),
        }, f, indent=2)


def compute_rotation_signals(
    current_phase: str,
    config: dict,
    inputs_dir: Path,
) -> list[RotationSignal]:
    """
    Compare current phase vs last saved phase.
    Generate EXIT/ENTER/KEEP signals for thematic sleeve ETFs.
    """
    thematic = config["sleeves"].get("Thematic", {})
    rotation = thematic.get("phase_rotation", {})

    if current_phase not in rotation:
        log.warning(f"Phase '{current_phase}' not in phase_rotation config. No rotation signals.")
        return []

    new_config    = rotation[current_phase]
    new_active    = set(new_config["active_etfs"])
    new_weights   = new_config["weights"]

    # Load previous phase
    last = _load_last_phase(inputs_dir)
    old_phase   = last.get("phase", "")
    old_active  = set(last.get("active_etfs", []))
    old_weights = last.get("weights", {})

    # No previous phase saved — first run
    if not old_phase:
        log.info(f"First run — setting thematic phase to {current_phase}")
        _save_last_phase(inputs_dir, current_phase, list(new_active), new_weights)
        return [
            RotationSignal(
                ticker=t, action="ENTER",
                old_weight=0, new_weight=new_weights.get(t, 0),
                reason=f"Initial thematic setup for {current_phase}",
                phase_from="NONE", phase_to=current_phase,
            )
            for t in new_active
        ]

    # Same phase — no rotation needed
    if old_phase == current_phase:
        log.info(f"Phase unchanged ({current_phase}) — no thematic rotation needed.")
        return []

    # PHASE CHANGED — generate rotation signals
    log.info(f"PHASE CHANGE: {old_phase} → {current_phase}")
    signals = []

    # ETFs to EXIT (were active, no longer active)
    for t in old_active - new_active:
        signals.append(RotationSignal(
            ticker=t, action="EXIT",
            old_weight=old_weights.get(t, 0), new_weight=0,
            reason=f"Phase changed {old_phase} → {current_phase}. {t.replace('.NS','')} no longer in active thematic list.",
            phase_from=old_phase, phase_to=current_phase,
        ))

    # ETFs to ENTER (not previously active, now active)
    for t in new_active - old_active:
        signals.append(RotationSignal(
            ticker=t, action="ENTER",
            old_weight=0, new_weight=new_weights.get(t, 0),
            reason=f"Phase changed {old_phase} → {current_phase}. {t.replace('.NS','')} is now overweight in {current_phase}.",
            phase_from=old_phase, phase_to=current_phase,
        ))

    # ETFs to KEEP (active in both, possibly different weight)
    for t in old_active & new_active:
        old_w = old_weights.get(t, 0)
        new_w = new_weights.get(t, 0)
        signals.append(RotationSignal(
            ticker=t, action="KEEP",
            old_weight=old_w, new_weight=new_w,
            reason=f"Active in both {old_phase} and {current_phase}. Weight {'unchanged' if old_w == new_w else f'changed {old_w}% → {new_w}%'}.",
            phase_from=old_phase, phase_to=current_phase,
        ))

    # Save new phase
    _save_last_phase(inputs_dir, current_phase, list(new_active), new_weights)

    log.info(
        f"Rotation: {sum(1 for s in signals if s.action=='EXIT')} EXIT, "
        f"{sum(1 for s in signals if s.action=='ENTER')} ENTER, "
        f"{sum(1 for s in signals if s.action=='KEEP')} KEEP"
    )
    return signals


def signals_to_dict(signals: list[RotationSignal]) -> list[dict]:
    return [{
        "ticker":     s.ticker,
        "action":     s.action,
        "old_weight": s.old_weight,
        "new_weight": s.new_weight,
        "reason":     s.reason,
        "phase_from": s.phase_from,
        "phase_to":   s.phase_to,
    } for s in signals]
