"""
instrument_scorer.py  — v5
══════════════════════════════════════════════════════════════════════════════
Decides WHICH instruments appear in the execution plan and HOW MUCH budget
each one gets.

SLEEVE ROUTING LOGIC (Key Design Decision #1):
  • Core / International / Hedge  → driven by LIVE 12-indicator scores ONLY
      – Include every ETF in the sleeve that has sleeve budget AND score ≥ WATCH
      – Budget split is proportional to score rank within the sleeve
  • Thematic                       → driven by Engine 3 phase rotation ONLY
      – Active ETFs are the 3 phase ETFs from phase_rotation output
      – Their weights come from allocation_config (ETF 1: 6%, ETF 2: 5%, ETF 3: 4%)

SCORE THRESHOLDS (12-indicator, 110 pts max):
  STRONG BUY  ≥ 88  (≥80%)
  BUY         ≥ 72  (≥65%)
  PARTIAL     ≥ 55  (≥50%)
  WATCH       ≥ 39  (≥35%)   ← MINIMUM to appear in execution plan
  AVOID       <  39 (<35%)   ← excluded regardless of budget

ENGINE 3 OVERRIDE (Key Design Decision #2 from state summary):
  Engine 2 BUY signals have HIGHEST priority — they can override a PAUSED
  sleeve by borrowing 50% from the largest underweight sleeve.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Score thresholds ────────────────────────────────────────────────────────
SCORE_MAX = 110
STRONG_BUY_PCT = 0.80   # ≥ 88 pts
BUY_PCT        = 0.65   # ≥ 72 pts
PARTIAL_PCT    = 0.50   # ≥ 55 pts
WATCH_PCT      = 0.35   # ≥ 39 pts  — minimum to enter execution plan

STRONG_BUY_THRESHOLD = SCORE_MAX * STRONG_BUY_PCT   # 88
BUY_THRESHOLD        = SCORE_MAX * BUY_PCT           # 71.5
PARTIAL_THRESHOLD    = SCORE_MAX * PARTIAL_PCT       # 55
WATCH_THRESHOLD      = SCORE_MAX * WATCH_PCT         # 38.5


def _score_label(score: float) -> str:
    """Convert numeric score to signal label."""
    if score >= STRONG_BUY_THRESHOLD:
        return "STRONG BUY"
    elif score >= BUY_THRESHOLD:
        return "BUY"
    elif score >= PARTIAL_THRESHOLD:
        return "PARTIAL"
    elif score >= WATCH_THRESHOLD:
        return "WATCH"
    else:
        return "AVOID"


def _proportional_split(etfs_with_scores: list[dict], total_budget: float) -> list[dict]:
    """
    Split total_budget proportionally by score among the given ETFs.
    ETFs with equal scores get equal budget.
    Returns the same list with 'allocated_budget' field added.
    """
    if not etfs_with_scores or total_budget <= 0:
        return etfs_with_scores

    total_score = sum(e["score"] for e in etfs_with_scores)
    if total_score == 0:
        # Equal split if all scores are somehow 0
        per_etf = total_budget / len(etfs_with_scores)
        for e in etfs_with_scores:
            e["allocated_budget"] = round(per_etf, 2)
        return etfs_with_scores

    for e in etfs_with_scores:
        e["allocated_budget"] = round((e["score"] / total_score) * total_budget, 2)

    return etfs_with_scores


def _fixed_weight_split(etfs_with_weights: list[dict], portfolio_value: float) -> list[dict]:
    """
    For Thematic ETFs that have a target weight in allocation_config
    (ETF1=6%, ETF2=5%, ETF3=4%), compute the ₹ budget directly from
    portfolio_value × target_weight.  Returned with 'allocated_budget'.
    """
    for e in etfs_with_weights:
        weight = e.get("target_weight", 0.05)  # default 5% if missing
        e["allocated_budget"] = round(portfolio_value * weight, 2)
    return etfs_with_weights


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def score_instruments(
    sleeve_budgets: dict[str, float],
    live_scores: dict[str, float],
    phase_etfs: list[dict],
    hedge_etfs: list[dict],
    config: dict[str, Any],
    engine2_signals: dict[str, str] | None = None,
    sleeve_status: dict[str, str] | None = None,
    portfolio_value: float = 0.0,
) -> list[dict]:
    """
    Build the ranked execution plan for this run.

    Parameters
    ----------
    sleeve_budgets      : {"Core": 5000, "International": 2000, "Thematic": 1500, "Hedge": 500}
                          Monthly investable budget per sleeve (₹).
    live_scores         : {"NIFTYIETF": 82.5, "MON100": 67.0, ...}
                          Scores out of 110 from live_scorer.py.
    phase_etfs          : [{"ticker": "ITBEES", "rank": 1, "target_weight": 0.06}, ...]
                          Engine 3 phase rotation output — only used for Thematic.
    hedge_etfs          : [{"ticker": "GOLDBEES", "target_weight": 0.04}, ...]
                          Fixed hedge ETFs from config (GOLDBEES, LIQUIDBEES).
    config              : Full allocation_config dict.
    engine2_signals     : {"NIFTYIETF": "BUY", "MON100": "SELL", ...}  optional
    sleeve_status       : {"Core": "BOOST", "Hedge": "STOP", ...}  optional
    portfolio_value     : Total portfolio ₹ value (for weight-based Thematic calc).

    Returns
    -------
    List of instrument dicts, each containing:
        ticker, sleeve, score, signal, allocated_budget,
        engine2_signal (optional), priority_override (bool),
        deploy_fields dict (multiplier info from tranche_manager lives elsewhere)
    """

    engine2_signals = engine2_signals or {}
    sleeve_status   = sleeve_status or {}

    execution_plan: list[dict] = []

    # ──────────────────────────────────────────────────────────────────────
    # 1. CORE SLEEVE — live scores only
    # ──────────────────────────────────────────────────────────────────────
    core_etfs_cfg = config.get("core_etfs", [
        "NIFTYIETF", "JUNIORBEES", "MID150BEES", "QUAL30IETF", "MOMOMENTUM"
    ])
    core_budget = sleeve_budgets.get("Core", 0)

    if core_budget > 0 and sleeve_status.get("Core") != "STOP":
        core_candidates = _build_candidates(
            tickers=core_etfs_cfg,
            sleeve="Core",
            live_scores=live_scores,
            engine2_signals=engine2_signals,
            min_score=WATCH_THRESHOLD,
        )
        if core_candidates:
            core_candidates = _proportional_split(core_candidates, core_budget)
            execution_plan.extend(core_candidates)
            logger.info(
                "Core sleeve: %d ETFs eligible (budget ₹%s)",
                len(core_candidates), core_budget
            )
        else:
            logger.warning("Core sleeve: no ETF scored ≥ WATCH — no Core instruments in plan")
    elif sleeve_status.get("Core") == "STOP":
        logger.info("Core sleeve STOP — skipping (except Engine2 override below)")

    # ──────────────────────────────────────────────────────────────────────
    # 2. INTERNATIONAL SLEEVE — live scores only
    # ──────────────────────────────────────────────────────────────────────
    intl_etfs_cfg = config.get("international_etfs", [
        "MON100", "MOUS500", "MAFANG", "HSET"
    ])
    intl_budget = sleeve_budgets.get("International", 0)

    if intl_budget > 0 and sleeve_status.get("International") != "STOP":
        intl_candidates = _build_candidates(
            tickers=intl_etfs_cfg,
            sleeve="International",
            live_scores=live_scores,
            engine2_signals=engine2_signals,
            min_score=WATCH_THRESHOLD,
        )
        if intl_candidates:
            intl_candidates = _proportional_split(intl_candidates, intl_budget)
            execution_plan.extend(intl_candidates)
            logger.info(
                "International sleeve: %d ETFs eligible (budget ₹%s)",
                len(intl_candidates), intl_budget
            )
        else:
            logger.warning("International sleeve: no ETF scored ≥ WATCH")
    elif sleeve_status.get("International") == "STOP":
        logger.info("International sleeve STOP — skipping")

    # ──────────────────────────────────────────────────────────────────────
    # 3. THEMATIC SLEEVE — Engine 3 phase rotation only
    #    phase_etfs already contains the 3 active ETFs for current phase.
    #    We assign fixed weights from config, NOT live-score-proportional.
    # ──────────────────────────────────────────────────────────────────────
    thematic_budget = sleeve_budgets.get("Thematic", 0)

    if thematic_budget > 0 and sleeve_status.get("Thematic") != "STOP" and phase_etfs:
        thematic_candidates = []
        for etf_info in phase_etfs:
            ticker = etf_info.get("ticker", "")
            if not ticker:
                continue
            score = live_scores.get(ticker, 0)
            e2_signal = engine2_signals.get(ticker, "")
            thematic_candidates.append({
                "ticker": ticker,
                "sleeve": "Thematic",
                "score": score,
                "signal": _score_label(score),
                "engine2_signal": e2_signal,
                "target_weight": etf_info.get("target_weight", 0.05),
                "phase_rank": etf_info.get("rank", 99),
                "priority_override": False,
                "source": "engine3_phase_rotation",
            })

        if portfolio_value > 0:
            thematic_candidates = _fixed_weight_split(thematic_candidates, portfolio_value)
        else:
            # Fallback: split thematic_budget equally across 3 ETFs
            per_etf = thematic_budget / max(len(thematic_candidates), 1)
            for e in thematic_candidates:
                e["allocated_budget"] = round(per_etf, 2)

        execution_plan.extend(thematic_candidates)
        logger.info(
            "Thematic sleeve: %d ETFs from Engine3 phase rotation",
            len(thematic_candidates)
        )
    elif not phase_etfs:
        logger.warning("Thematic sleeve: no phase_etfs from Engine 3 — skipping")

    # ──────────────────────────────────────────────────────────────────────
    # 4. HEDGE SLEEVE — fixed schedule (GOLDBEES=2nd Monday, LIQUIDBEES)
    #    Include regardless of live score (insurance, not returns).
    #    But respect STOP status.
    # ──────────────────────────────────────────────────────────────────────
    hedge_budget = sleeve_budgets.get("Hedge", 0)

    if hedge_budget > 0 and sleeve_status.get("Hedge") != "STOP" and hedge_etfs:
        hedge_candidates = []
        total_hedge_weight = sum(e.get("target_weight", 0.025) for e in hedge_etfs)
        for etf_info in hedge_etfs:
            ticker = etf_info.get("ticker", "")
            if not ticker:
                continue
            score = live_scores.get(ticker, 0)
            weight = etf_info.get("target_weight", 0.025)
            # Budget proportional to target weight (not live score — it's insurance)
            alloc = round((weight / max(total_hedge_weight, 0.001)) * hedge_budget, 2)
            hedge_candidates.append({
                "ticker": ticker,
                "sleeve": "Hedge",
                "score": score,
                "signal": _score_label(score),
                "engine2_signal": engine2_signals.get(ticker, ""),
                "allocated_budget": alloc,
                "priority_override": False,
                "source": "fixed_schedule",
            })
        execution_plan.extend(hedge_candidates)
        logger.info("Hedge sleeve: %d ETFs (fixed schedule)", len(hedge_candidates))

    # ──────────────────────────────────────────────────────────────────────
    # 5. ENGINE 2 PRIORITY OVERRIDE
    #    If Engine 2 fires BUY for an ETF whose sleeve is PAUSED/STOP,
    #    add it back with priority_override=True and borrow from largest
    #    underweight sleeve.  (Actual borrowing is handled in main.py /
    #    tranche_manager — we just flag here.)
    # ──────────────────────────────────────────────────────────────────────
    already_in_plan = {e["ticker"] for e in execution_plan}
    ALL_ETF_SLEEVES = {
        **{t: "Core" for t in ["NIFTYIETF", "JUNIORBEES", "MID150BEES", "QUAL30IETF", "MOMOMENTUM"]},
        **{t: "International" for t in ["MON100", "MOUS500", "MAFANG", "HSET"]},
        **{t: "Thematic" for t in ["BANKBEES", "AUTOBEES", "INFRABEES", "ITBEES",
                                    "METALBEES", "MODEFENCE", "ENERGYBEES",
                                    "PHARMABEES", "FMCGBEES"]},
        **{t: "Hedge" for t in ["GOLDBEES", "LIQUIDBEES"]},
    }

    for ticker, e2sig in engine2_signals.items():
        if e2sig != "BUY":
            continue
        if ticker in already_in_plan:
            # Already in plan — mark the Engine 2 signal but no need to add
            for e in execution_plan:
                if e["ticker"] == ticker:
                    e["engine2_signal"] = "BUY"
                    e["engine2_boost"] = True
            continue

        sleeve = ALL_ETF_SLEEVES.get(ticker, "Unknown")
        sleeve_st = sleeve_status.get(sleeve, "")
        score = live_scores.get(ticker, 0)

        if sleeve_st in ("STOP", "PAUSED"):
            logger.info(
                "Engine2 BUY override: %s (sleeve %s is %s) — adding with priority_override",
                ticker, sleeve, sleeve_st
            )
            execution_plan.append({
                "ticker": ticker,
                "sleeve": sleeve,
                "score": score,
                "signal": _score_label(score),
                "engine2_signal": "BUY",
                "engine2_boost": True,
                "allocated_budget": 0,   # tranche_manager sets actual budget via borrowing
                "priority_override": True,
                "source": "engine2_override",
            })

    # ──────────────────────────────────────────────────────────────────────
    # 6. SORT — within each sleeve, highest score first
    #           priority_override items always first in their sleeve
    # ──────────────────────────────────────────────────────────────────────
    SLEEVE_ORDER = {"Core": 0, "International": 1, "Thematic": 2, "Hedge": 3, "Unknown": 4}
    execution_plan.sort(
        key=lambda x: (
            SLEEVE_ORDER.get(x["sleeve"], 99),
            0 if x.get("priority_override") else 1,
            -x.get("score", 0),
        )
    )

    logger.info(
        "instrument_scorer: %d instruments in execution plan",
        len(execution_plan)
    )
    _log_plan_summary(execution_plan)

    return execution_plan


# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _build_candidates(
    tickers: list[str],
    sleeve: str,
    live_scores: dict[str, float],
    engine2_signals: dict[str, str],
    min_score: float,
) -> list[dict]:
    """
    Filter tickers to those scoring ≥ min_score, return as candidate list.
    ETFs with AVOID score are excluded unless Engine 2 fires BUY for them.
    """
    candidates = []
    for ticker in tickers:
        score = live_scores.get(ticker, 0)
        e2_signal = engine2_signals.get(ticker, "")
        label = _score_label(score)

        # Include if score ≥ WATCH, OR if Engine 2 fires BUY (override avoid)
        if score >= min_score or e2_signal == "BUY":
            if score < min_score and e2_signal == "BUY":
                logger.info(
                    "%s scored AVOID (%s pts) but Engine2 BUY — including anyway",
                    ticker, score
                )
                # Give it at least WATCH score so it gets some budget
                effective_score = min_score
            else:
                effective_score = score

            candidates.append({
                "ticker": ticker,
                "sleeve": sleeve,
                "score": effective_score,
                "raw_score": score,
                "signal": label,
                "engine2_signal": e2_signal,
                "priority_override": False,
                "source": "live_score",
            })
        else:
            logger.debug("%s EXCLUDED — score %.1f < WATCH threshold (%.1f)", ticker, score, min_score)

    # Sort descending by score within sleeve
    candidates.sort(key=lambda x: -x["score"])
    return candidates


def _log_plan_summary(plan: list[dict]) -> None:
    """Log a clean table of the execution plan for debugging."""
    if not plan:
        logger.warning("Execution plan is EMPTY")
        return

    header = f"{'Ticker':<14} {'Sleeve':<14} {'Score':>6} {'Signal':<12} {'Budget':>10} {'E2':>5}"
    logger.info("── Execution Plan ──────────────────────────────────────────")
    logger.info(header)
    for e in plan:
        row = (
            f"{e['ticker']:<14} "
            f"{e['sleeve']:<14} "
            f"{e.get('score', 0):>6.1f} "
            f"{e.get('signal', '—'):<12} "
            f"₹{e.get('allocated_budget', 0):>9,.0f} "
            f"{e.get('engine2_signal', '—'):>5}"
        )
        logger.info(row)
    logger.info("────────────────────────────────────────────────────────────")


# ══════════════════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    # Simulate what main.py would pass in
    _sleeve_budgets = {
        "Core": 6000,
        "International": 2000,
        "Thematic": 1500,
        "Hedge": 500,
    }

    # Simulated live scores (out of 110)
    _live_scores = {
        "NIFTYIETF":   82.0,   # STRONG BUY
        "JUNIORBEES":  68.0,   # BUY
        "MID150BEES":  55.0,   # PARTIAL
        "QUAL30IETF":  72.0,   # BUY
        "MOMOMENTUM":  30.0,   # AVOID — will be skipped (no Golden Cross)
        "MON100":      70.0,   # BUY
        "MOUS500":     60.0,   # PARTIAL
        "MAFANG":      45.0,   # WATCH
        "HSET":        20.0,   # AVOID — score too low
        "ITBEES":      75.0,   # BUY (current phase)
        "METALBEES":   65.0,   # BUY (current phase)
        "MODEFENCE":   58.0,   # PARTIAL (current phase)
        "GOLDBEES":    50.0,   # (Hedge — score not used for inclusion)
    }

    _phase_etfs = [
        {"ticker": "ITBEES",    "rank": 1, "target_weight": 0.06},
        {"ticker": "METALBEES", "rank": 2, "target_weight": 0.05},
        {"ticker": "MODEFENCE", "rank": 3, "target_weight": 0.04},
    ]

    _hedge_etfs = [
        {"ticker": "GOLDBEES",   "target_weight": 0.04},
        {"ticker": "LIQUIDBEES", "target_weight": 0.01},
    ]

    _config = {
        "core_etfs": ["NIFTYIETF", "JUNIORBEES", "MID150BEES", "QUAL30IETF", "MOMOMENTUM"],
        "international_etfs": ["MON100", "MOUS500", "MAFANG", "HSET"],
    }

    _engine2_signals = {
        "JUNIORBEES": "BUY",   # Engine2 confirms Core BUY
        "MON100":     "BUY",   # Engine2 confirms International BUY
    }

    _sleeve_status = {
        "Core":          "BOOST",
        "International": "BOOST",
        "Thematic":      "BOOST",
        "Hedge":         "STOP",    # Hedge STOP — GOLDBEES skipped
    }

    plan = score_instruments(
        sleeve_budgets=_sleeve_budgets,
        live_scores=_live_scores,
        phase_etfs=_phase_etfs,
        hedge_etfs=_hedge_etfs,
        config=_config,
        engine2_signals=_engine2_signals,
        sleeve_status=_sleeve_status,
        portfolio_value=1_540_000,
    )

    print(f"\n✅  {len(plan)} instruments in execution plan")
    for item in plan:
        print(
            f"  {item['ticker']:<14} {item['sleeve']:<14} "
            f"score={item.get('score', 0):.1f}  signal={item.get('signal'):<11} "
            f"budget=₹{item.get('allocated_budget', 0):,.0f}"
            f"{'  ← E2 BUY' if item.get('engine2_signal') == 'BUY' else ''}"
            f"{'  ⚡ OVERRIDE' if item.get('priority_override') else ''}"
        )
