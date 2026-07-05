"""
orchestrator/engine/exit_advisor.py
─────────────────────────────────────
Two types of exit actions:

REBALANCE: Sleeve overweight > 5% → suggest units to exit.
  Reduces highest P&L% holdings first (tax-efficient harvesting order).
  Tax rates from Engine 2's portfolio_config.json: STCG 20%, LTCG 12.5%.

URGENT: Engine 2 urgent_alerts tickers (held in Upstox but removed
  from Google Sheet master list). Engine 2 still monitors exit signals.
  We surface them here so you see them alongside the execution plan.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

log = logging.getLogger("exit_advisor")


@dataclass
class ExitAction:
    ticker:         str
    sleeve:         str
    exit_type:      str      # REBALANCE | URGENT
    reason:         str
    units_held:     float
    units_to_exit:  float
    exit_value:     float
    avg_cost:       float
    current_price:  float
    pnl:            float
    pnl_pct:        float
    tax_note:       str
    drift_pct:      Optional[float]
    suggested_date: str
    date_rule:      str


def _tax_note(ticker: str) -> str:
    t = ticker.upper()
    if any(k in t for k in ["GOLD","SILVER","LIQUID","HNGSNG"]):
        return "Non-equity ETF — consult tax advisor for applicable slab"
    return "Equity ETF — STCG 20% if <1yr held | LTCG 12.5% above ₹1.25L if >1yr"


def _rebalance_exits(sleeve, drift_pct, holdings, total_value, exit_date, date_rule) -> list:
    excess    = (drift_pct / 100) * total_value
    sorted_h  = sorted(holdings, key=lambda h: h.get("pnl_pct", 0), reverse=True)
    exits     = []
    remaining = excess

    for h in sorted_h:
        if remaining <= 0: break
        price = h.get("last_price", 0)
        units = h.get("quantity", 0)
        avg   = h.get("avg_cost_price", 0)
        if price <= 0 or units <= 0: continue
        n = min(units, int(remaining / price))
        if n <= 0: continue
        pnl     = (price - avg) * n
        pnl_pct = (price - avg) / avg * 100 if avg else 0
        exits.append(ExitAction(
            ticker=h["ticker"], sleeve=sleeve, exit_type="REBALANCE",
            reason=f"Sleeve {sleeve} overweight +{drift_pct:.1f}% (threshold +5%). Reduce to target.",
            units_held=units, units_to_exit=n,
            exit_value=round(n * price, 2), avg_cost=round(avg, 2),
            current_price=round(price, 2), pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
            tax_note=_tax_note(h["ticker"]), drift_pct=drift_pct,
            suggested_date=exit_date, date_rule=date_rule,
        ))
        remaining -= n * price
    return exits


def _urgent_exits(alerts, holdings, exit_date, date_rule, config=None) -> list:
    # Build a set of all tickers that are intentionally held per allocation_config.
    # Engine 2 scans Nifty 500 stocks; its "urgent_alerts" flag means "this holding
    # is NOT in Engine 2's monitored universe." But international ETFs (MON100,
    # MOUS500, MAFANG, HSET) and other sleeve instruments were NEVER meant to be
    # in Engine 2's universe — they're permanent strategic holds defined in config.
    # Flagging them as URGENT exits every week is a false alarm. Rule: if a ticker
    # is explicitly listed under any sleeve in allocation_config.json, it is an
    # intentional hold and must never be surfaced as an URGENT exit.
    configured = set()
    if config:
        for sleeve_cfg in config.get("sleeves", {}).values():
            for t in sleeve_cfg.get("instruments", []):
                bare = t.upper().replace(".NS", "")
                configured.add(bare)
                configured.add(bare + ".NS")

    h_map = {h["ticker"].upper(): h for h in holdings}
    exits = []
    for alert in alerts:
        ticker_up = alert["ticker"].upper()
        bare_up   = ticker_up.replace(".NS", "")
        if bare_up in configured or ticker_up in configured:
            log.info(f"  URGENT suppressed: {alert['ticker']} is in allocation_config "
                     f"(intentional hold — not an Engine 2 exit candidate)")
            continue
        h = h_map.get(ticker_up)
        if not h: continue
        price = h.get("last_price", 0)
        units = h.get("quantity", 0)
        avg   = h.get("avg_cost_price", 0)
        pnl   = (price - avg) * units
        exits.append(ExitAction(
            ticker=alert["ticker"], sleeve=h.get("sleeve","Unknown"), exit_type="URGENT",
            reason=alert.get("reason","Removed from Engine 2 master sheet"),
            units_held=units, units_to_exit=units,
            exit_value=round(price * units, 2), avg_cost=round(avg, 2),
            current_price=round(price, 2), pnl=round(pnl, 2),
            pnl_pct=round((price - avg) / avg * 100 if avg else 0, 2),
            tax_note=_tax_note(alert["ticker"]), drift_pct=None,
            suggested_date=exit_date, date_rule=date_rule,
        ))
    return exits


def compute_exit_actions(alloc_plan, classified_holdings, signal_data, config, today=None) -> list:
    if today is None:
        today = datetime.now().date()

    from orchestrator.engine.buy_date_resolver import resolve_exit_date
    info      = resolve_exit_date(today, config)
    exit_date = info["date"]
    date_rule = info["rule"]
    stop_thr  = config["sleeve_rules"]["overweight_stop_threshold_pct"]

    all_exits = []
    for sleeve_name, s in alloc_plan.sleeves.items():
        if s.drift_pct > stop_thr:
            all_exits.extend(_rebalance_exits(sleeve_name, s.drift_pct, s.holdings,
                                              alloc_plan.total_portfolio_value, exit_date, date_rule))

    all_exits.extend(_urgent_exits(signal_data.get("urgent_alerts",[]),
                                   classified_holdings, exit_date, date_rule, config))

    all_exits.sort(key=lambda x: (x.exit_type != "REBALANCE", -x.exit_value))
    log.info(f"Exit advisor: {len(all_exits)} actions, total ₹{sum(e.exit_value for e in all_exits):,.0f}")
    return all_exits
