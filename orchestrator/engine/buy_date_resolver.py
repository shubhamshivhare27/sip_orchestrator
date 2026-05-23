"""
orchestrator/engine/buy_date_resolver.py
──────────────────────────────────────────
Determines exact buy date for each instrument using 3 rules.

Rule 1 — Engine 2 signal confirmed:
  Buy on the NEXT Friday (Engine 2's next_run_date from email footer).
  We wait for the next official Friday run to confirm no reversal.
  If next_run_date is in the email, use it. If not, compute next Friday.

Rule 2 — Macro BUY/WATCHLIST, no Engine 2 signal:
  Buy on 15th of current month.
  If today > 15th → next month's 15th.
  If 15th is NSE holiday or weekend → previous trading day.

Rule 3 — Rebalance exit / Urgent alert:
  Buy on next available trading day (skips NSE holidays + weekends).

NSE holidays from config + Engine 2's market_calendar.py list.
"""

import logging
from datetime import date, timedelta, datetime

log = logging.getLogger("buy_date_resolver")


def _holidays(config: dict) -> set:
    from datetime import date as d_type
    result = set()
    for s in config.get("nse_holidays", []):
        try:
            result.add(datetime.strptime(s, "%Y-%m-%d").date())
        except ValueError:
            pass
    return result


def _is_trading_day(d: date, holidays: set) -> bool:
    return d.weekday() < 5 and d not in holidays


def _next_trading_day(from_date: date, holidays: set) -> date:
    d = from_date + timedelta(days=1)
    while not _is_trading_day(d, holidays):
        d += timedelta(days=1)
    return d


def _next_friday(from_date: date, holidays: set) -> date:
    days = (4 - from_date.weekday()) % 7
    if days == 0:
        days = 7
    candidate = from_date + timedelta(days=days)
    # If Friday is a holiday, use Thursday (same as Engine 2's market_calendar.py)
    while not _is_trading_day(candidate, holidays):
        candidate -= timedelta(days=1)
    return candidate


def _monthly_sip_date(today: date, sip_day: int, holidays: set) -> date:
    """
    15th of current month, or next month if today > 15th.
    Adjusts backwards if 15th falls on a non-trading day.
    """
    candidate = date(today.year, today.month, sip_day)
    if today > candidate:
        if today.month == 12:
            candidate = date(today.year + 1, 1, sip_day)
        else:
            candidate = date(today.year, today.month + 1, sip_day)
    while not _is_trading_day(candidate, holidays):
        candidate -= timedelta(days=1)
    return candidate


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _fmt(d: date) -> str:
    return d.strftime("%d %b %Y")


def resolve_buy_date(instrument, signal_data: dict, config: dict, today: date = None) -> dict:
    if today is None:
        today = datetime.now().date()
    holidays = _holidays(config)
    sip_day  = config["buy_date_rules"].get("monthly_sip_day", 15)

    # ── Rule 1: Engine 2 signal confirmed ────────────────────────────────────
    if instrument.has_engine2_signal:
        next_run_date = _parse_date(signal_data.get("next_run_date"))
        if next_run_date:
            buy_date = next_run_date if _is_trading_day(next_run_date, holidays) else _next_trading_day(next_run_date - timedelta(days=1), holidays)
        else:
            buy_date = _next_friday(today, holidays)

        conds = " | ".join(instrument.engine2_conditions[:2]) if instrument.engine2_conditions else ""
        return {
            "date":   _fmt(buy_date),
            "rule":   (
                f"Next Engine 2 signal run (Friday cadence). "
                f"Signal triggered on {instrument.engine2_date or 'last run'} — "
                f"waiting for next official Friday run to confirm no reversal before executing."
            ),
            "source": f"{instrument.engine2_strategy or 'Engine 2'}" + (f" — {conds}" if conds else ""),
            "type":   "ENGINE2_RUN",
        }

    # ── Rule 2: Macro BUY or WATCHLIST → monthly SIP date ───────────────────
    if instrument.tag in ("BUY", "WATCHLIST"):
        buy_date = _monthly_sip_date(today, sip_day, holidays)
        stance_full = {"OW":"Overweight","N":"Neutral","UW":"Underweight"}.get(instrument.stance, instrument.stance)
        return {
            "date":   _fmt(buy_date),
            "rule":   (
                f"Standard monthly SIP date ({sip_day}th of month). "
                f"If today is past {sip_day}th, uses next month's {sip_day}th. "
                f"If {sip_day}th is NSE holiday or weekend, uses previous trading day."
            ),
            "source": f"Engine 3 — tag: {instrument.tag}, stance: {stance_full}",
            "type":   "MONTHLY_SIP",
        }

    # ── Fallback ─────────────────────────────────────────────────────────────
    buy_date = _next_trading_day(today, holidays)
    return {
        "date":   _fmt(buy_date),
        "rule":   "Next available trading day (fallback).",
        "source": f"Tag: {instrument.tag}",
        "type":   "NEXT_TRADING_DAY",
    }


def resolve_exit_date(today: date = None, config: dict = None) -> dict:
    if today is None:
        today = datetime.now().date()
    holidays = _holidays(config or {})
    buy_date = _next_trading_day(today, holidays)
    return {
        "date":   _fmt(buy_date),
        "rule":   "Earliest next trading day. Executed promptly to prevent drift compounding. Skips NSE holidays and weekends.",
        "source": "Sleeve rebalance / urgent alert trigger",
        "type":   "NEXT_TRADING_DAY",
    }


def attach_buy_dates(instruments: list, signal_data: dict, config: dict, today: date = None) -> list:
    if today is None:
        today = datetime.now().date()
    for inst in instruments:
        info = resolve_buy_date(inst, signal_data, config, today)
        inst.buy_date        = info["date"]
        inst.buy_date_rule   = info["rule"]
        inst.buy_date_source = info["source"]
    return instruments
