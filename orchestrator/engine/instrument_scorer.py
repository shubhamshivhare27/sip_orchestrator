"""
orchestrator/engine/instrument_scorer.py
──────────────────────────────────────────
Cross-references Engine 3 ETF tags with Engine 2 buy signals.
Scores each eligible ETF and distributes sleeve ₹ budgets proportionally.

Scoring formula matches sector_mapper._tag_etf weights:
  composite = macro_stance(0.40) + signal_engine(0.35) + rsi(0.15) + momentum(0.10)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("instrument_scorer")


@dataclass
class ScoredInstrument:
    ticker:               str
    sleeve:               str
    tag:                  str
    stance:               str        # OW / N / UW from Engine 3
    price:                Optional[float]
    rsi:                  Optional[float]
    mom_4w:               Optional[float]
    ma_signal:            Optional[str]
    ann_vol:              Optional[float]
    has_engine2_signal:   bool
    engine2_strategy_id:  Optional[str]
    engine2_strategy:     Optional[str]
    engine2_conditions:   list = field(default_factory=list)
    engine2_date:         Optional[str] = None
    engine2_ssf50:        Optional[float] = None
    engine2_rsi_weekly:   Optional[float] = None
    macro_score:          int = 0
    signal_score:         int = 0
    rsi_score:            int = 0
    mom_score:            int = 0
    composite:            int = 0
    confidence:           str = "Low"
    allocated_inr:        float = 0.0
    deploy_now_inr:       float = 0.0
    deploy_tranche:       Optional[str] = None
    deploy_multiplier:    float = 1.0
    buy_date:             Optional[str] = None
    buy_date_rule:        Optional[str] = None
    buy_date_source:      Optional[str] = None


def _macro_score(stance: str, rules: dict) -> int:
    return int(rules["macro_stance"].get(stance, 0.2) * 100)

def _signal_score(has_signal: bool, tag: str, rules: dict) -> int:
    r = rules["signal_engine"]
    if has_signal:   return int(r["engine_confirmed"] * 100)
    if tag == "BUY": return int(r["macro_buy"] * 100)
    return int(r["watchlist"] * 100)

def _rsi_score(rsi: Optional[float], rules: dict) -> int:
    if rsi is None: return 50
    r = rules["rsi"]
    return int(r["healthy"] * 100) if r["healthy_min"] <= rsi <= r["healthy_max"] else int(r["unhealthy"] * 100)

def _mom_score(mom: Optional[float], rules: dict) -> int:
    if mom is None: return 50
    m = rules["momentum_4w"]
    if mom >= m["strong_threshold"]: return int(m["strong"] * 100)
    if mom >= 0:                     return int(m["positive"] * 100)
    return int(m["negative"] * 100)

def _composite(ms, ss, rs, ws, weights: dict) -> int:
    return int(
        ms * weights["macro_stance"]  +
        ss * weights["signal_engine"] +
        rs * weights["rsi_health"]    +
        ws * weights["momentum_4w"]
    )

def _confidence(score: int) -> str:
    if score >= 75: return "High"
    if score >= 55: return "Medium"
    return "Low"


def score_instruments(
    macro_data:  dict,
    signal_data: dict,
    alloc_plan,
    config:      dict,
) -> list[ScoredInstrument]:

    weights       = config["scoring_weights"]
    rules         = config["scoring_rules"]
    s_cfg         = config["sleeves"]
    min_alloc     = config["sleeve_rules"]["min_instrument_allocation_inr"]
    phase         = macro_data.get("phase", "UNKNOWN")
    phase_tw      = config.get("phase_tactical_weights", {}).get(phase, {})

    buy_map   = {s["ticker"].upper(): s for s in signal_data.get("buy_signals", [])}
    sell_map  = {s["ticker"].upper(): s for s in signal_data.get("sell_signals", [])}
    alert_map = {a["ticker"].upper(): a for a in signal_data.get("urgent_alerts", [])}

    ticker_sleeve = {}
    for sleeve, scfg in s_cfg.items():
        for t in scfg["instruments"]:
            ticker_sleeve[t.upper()] = sleeve

    scored = []
    for etf in macro_data.get("etf_tags", []):
        ticker = etf["ticker"].upper()
        tag    = etf.get("tag", "UNKNOWN")
        stance = etf.get("stance", "N")

        if tag == "AVOID":                  continue
        if ticker in sell_map:              continue    # active Engine 2 SELL signal
        if ticker in alert_map:             continue    # urgent alert → goes to exit advisor

        sleeve = ticker_sleeve.get(ticker, "Core")
        ss_obj = alloc_plan.sleeves.get(sleeve)
        if not ss_obj or ss_obj.sip_allocation <= 0:   continue

        sig2  = buy_map.get(ticker)
        ms = _macro_score(stance, rules)
        ss = _signal_score(sig2 is not None, tag, rules)
        rs = _rsi_score(etf.get("rsi"), rules)
        ws = _mom_score(etf.get("mom_4w"), rules)
        cs = _composite(ms, ss, rs, ws, weights)

        scored.append(ScoredInstrument(
            ticker=ticker, sleeve=sleeve, tag=tag, stance=stance,
            price=etf.get("price"), rsi=etf.get("rsi"),
            mom_4w=etf.get("mom_4w"), ma_signal=etf.get("ma_signal"),
            ann_vol=etf.get("ann_vol"),
            has_engine2_signal=sig2 is not None,
            engine2_strategy_id=sig2["strategy_id"]    if sig2 else None,
            engine2_strategy=sig2["strategy_name"]     if sig2 else None,
            engine2_conditions=sig2.get("conditions",[]) if sig2 else [],
            engine2_date=sig2.get("date")              if sig2 else None,
            engine2_ssf50=sig2.get("ssf50_weekly")     if sig2 else None,
            engine2_rsi_weekly=sig2.get("rsi_weekly")  if sig2 else None,
            macro_score=ms, signal_score=ss, rsi_score=rs, mom_score=ws,
            composite=cs, confidence=_confidence(cs),
        ))

    # Distribute sleeve budgets proportionally by composite score
    from itertools import groupby
    scored.sort(key=lambda x: x.sleeve)
    for sleeve, group in groupby(scored, key=lambda x: x.sleeve):
        items  = list(group)
        budget = alloc_plan.sleeves[sleeve].sip_allocation
        # Apply phase tactical weight bonus
        for item in items:
            tw = phase_tw.get(item.ticker, 0)
            if tw:
                item.composite = min(100, int(item.composite * (1 + tw / 100)))
        total = sum(i.composite for i in items)
        if total == 0: continue
        for item in items:
            item.allocated_inr = round((item.composite / total) * budget)

    scored = [i for i in scored if i.allocated_inr >= min_alloc]
    scored.sort(key=lambda x: x.composite, reverse=True)

    log.info(f"Scored {len(scored)} instruments | Engine2-confirmed: {sum(1 for i in scored if i.has_engine2_signal)}")
    return scored
