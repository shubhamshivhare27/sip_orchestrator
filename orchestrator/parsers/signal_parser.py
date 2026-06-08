"""
orchestrator/parsers/signal_parser.py
──────────────────────────────────────
Parses the Engine 2 Nifty 500 Signal Engine email HTML into a structured dict.
Written against the actual email_report.py source code — not guessed.

EMAIL STRUCTURE (from Engine 2's email_report.py build_html_body):
  Subject : "📊 Nifty 500 Signals — 2026-05-01 | 1 BUY | 0 SELL"
  Sender  : GMAIL_USER  →  RECIPIENT_EMAIL

  Summary block:
    BUY Signals N | SELL Signals N
    Stocks in Universe N | ETFs in Universe N
    Strategies Run: S2 ... | S4 ... | S5 ...

  URGENT block (if any):
    Holdings still in Upstox but removed from Google Sheet master list.
    These are the removed_from_sheet tickers from portfolio.get_removed_tickers().

  Universe Changes block:
    Added / Removed / Status changed tickers since last run.

  BUY signal table columns (buy_cols in email_report.py):
    ticker | strategy_name | signal_type | date |
    RSI14_daily | RSI14_weekly | RSI14_monthly |
    MACD_line_weekly | MACD_signal_weekly | SSF50_weekly | triggered_conditions

  SELL signal table columns (sell_cols in email_report.py):
    ticker | strategy_name | signal_type | date | triggered_conditions

  Footer:
    "Next signal run: 08 May 2026"

Strategy IDs mapped from signal_config.json:
  S1: Monthly EMA20 Breakout
  S2: Weekly EMA Pullback
  S3: Monthly SSF50 Breakout
  S4: Weekly SSF50 Breakout
  S5: Weekly ETF Breakout [Mod-1]  ← the one that fired INFRABEES on 27 Apr 2026
"""

import logging
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

log = logging.getLogger("signal_parser")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return " ".join(str(s).strip().split())

def _to_float(s) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s)))
    except (ValueError, TypeError):
        return None

def _extract_ymd(text: str) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None

def _extract_ticker(text: str) -> str | None:
    m = re.search(r"([A-Z0-9]+\.NS)", text)
    return m.group(1) if m else None

def _next_friday_from(date_str: str) -> str | None:
    """Compute the next Friday after the given YYYY-MM-DD date."""
    try:
        d    = datetime.strptime(date_str, "%Y-%m-%d")
        days = (4 - d.weekday()) % 7     # 4 = Friday
        if days == 0:
            days = 7
        return (d + timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return None


# ── Strategy name → ID map (from signal_config.json strategy IDs) ─────────────

_STRAT_MAP = {
    "monthly ema20":     "S1_monthly_ema20_breakout",
    "weekly ema pullback":"S2_weekly_ema_pullback",
    "weekly ema10":      "S2_weekly_ema_pullback",
    "monthly ssf50":     "S3_monthly_ssf50_breakout",
    "weekly ssf50":      "S4_weekly_ssf50_breakout",
    "weekly etf":        "S5_weekly_etf_breakout",
    "etf breakout":      "S5_weekly_etf_breakout",
    "mod-1":             "S5_weekly_etf_breakout",
}

def _strategy_id(name: str) -> str:
    nl = name.lower()
    for key, sid in _STRAT_MAP.items():
        if key in nl:
            return sid
    m = re.search(r"\b(s[1-5])\b", nl)
    if m:
        return {"s1":"S1_monthly_ema20_breakout","s2":"S2_weekly_ema_pullback",
                "s3":"S3_monthly_ssf50_breakout","s4":"S4_weekly_ssf50_breakout",
                "s5":"S5_weekly_etf_breakout"}.get(m.group(1).lower(), name)
    return name


# ── Summary block ─────────────────────────────────────────────────────────────

def _parse_summary(soup: BeautifulSoup, subject: str) -> dict:
    result = {
        "signal_date":        _extract_ymd(subject),
        "next_run_date":      None,
        "stocks_in_universe": 0,
        "etfs_in_universe":   0,
        "strategies_run":     [],
    }

    # next_run from subject date
    if result["signal_date"]:
        result["next_run_date"] = _next_friday_from(result["signal_date"])

    text  = soup.get_text(separator="\n")
    lines = [_clean(l) for l in text.split("\n") if _clean(l)]

    for line in lines:
        ll = line.lower()

        if "signal date" in ll:
            d = _extract_ymd(line)
            if d:
                result["signal_date"]   = d
                result["next_run_date"] = _next_friday_from(d)

        elif "next signal run" in ll or ("next" in ll and "run" in ll and "signal" in ll):
            d = _extract_ymd(line)
            if d:
                result["next_run_date"] = d
            else:
                m = re.search(r"(\d{2}\s+\w+\s+\d{4})", line)
                if m:
                    for fmt in ("%d %B %Y", "%d %b %Y"):
                        try:
                            result["next_run_date"] = datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue

        elif "stocks in universe" in ll:
            n = re.search(r"(\d+)", line)
            if n:
                result["stocks_in_universe"] = int(n.group(1))

        elif "etfs in universe" in ll:
            n = re.search(r"(\d+)", line)
            if n:
                result["etfs_in_universe"] = int(n.group(1))

        elif "strategies run" in ll or "strategies:" in ll:
            strats_part = re.sub(r".*strategies\s*(run)?\s*[:\-]?\s*", "", line, flags=re.IGNORECASE)
            strats = [_clean(s) for s in re.split(r"\s*\|\s*", strats_part) if _clean(s)]
            if strats:
                result["strategies_run"] = strats

    return result


# ── Signal table parser ───────────────────────────────────────────────────────

def _parse_signal_table(soup: BeautifulSoup, signal_type: str) -> list[dict]:
    """
    Parse BUY or SELL signal table from email_report.py HTML.
    BUY table has full RSI + MACD + SSF50 columns.
    SELL table has ticker + strategy + date + conditions only.
    """
    signals = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_cells = [_clean(c.get_text()).lower() for c in rows[0].find_all(["td","th"])]
        if not (any("ticker" in h for h in header_cells) and
                any("signal" in h for h in header_cells)):
            continue

        for row in rows[1:]:
            cells    = [_clean(c.get_text()) for c in row.find_all(["td","th"])]
            if len(cells) < 3:
                continue
            row_text = " ".join(cells)
            if signal_type.upper() not in row_text.upper():
                continue

            ticker = _extract_ticker(row_text)
            if not ticker:
                continue

            def _get(frags):
                for frag in frags:
                    for i, h in enumerate(header_cells):
                        if frag in h and i < len(cells):
                            return cells[i]
                return None

            strategy_raw = _get(["strategy_name","strategy"])     or ""
            date_raw     = _get(["date"])                          or ""
            rsi_w        = _get(["rsi14_weekly","rsiweekly"])      or ""
            rsi_d        = _get(["rsi14_daily","rsidaily"])        or ""
            rsi_m        = _get(["rsi14_monthly","rsimonthly"])    or ""
            macd_line    = _get(["macd_line_weekly","macdline"])   or ""
            macd_sig     = _get(["macd_signal_weekly","macdsig"])  or ""
            ssf50        = _get(["ssf50_weekly","ssf50"])          or ""
            cond_raw     = _get(["triggered_conditions","conditions","triggered"]) or ""

            conditions = [_clean(c) for c in re.split(r"\s*\|\s*|\s*,\s*", cond_raw) if _clean(c)]

            signals.append({
                "ticker":             ticker,
                "strategy_id":        _strategy_id(strategy_raw),
                "strategy_name":      _clean(strategy_raw),
                "signal_type":        signal_type.upper(),
                "date":               _extract_ymd(date_raw),
                "rsi_weekly":         _to_float(rsi_w),
                "rsi_daily":          _to_float(rsi_d),
                "rsi_monthly":        _to_float(rsi_m),
                "macd_line_weekly":   _to_float(macd_line),
                "macd_signal_weekly": _to_float(macd_sig),
                "ssf50_weekly":       _to_float(ssf50),
                "conditions":         conditions,
            })

    # Fallback: plain text scan if no table matched
    if not signals:
        signals = _text_fallback(soup, signal_type)

    return signals


def _text_fallback(soup: BeautifulSoup, signal_type: str) -> list[dict]:
    """Plain-text fallback when HTML table structure isn't matched."""
    signals    = []
    text       = soup.get_text(separator="\n")
    lines      = [_clean(l) for l in text.split("\n") if _clean(l)]
    in_section = False

    for line in lines:
        ll = line.lower()
        if f"{signal_type.lower()} signals" in ll:
            in_section = True; continue
        if in_section and "signals" in ll and signal_type.lower() not in ll:
            in_section = False
        if in_section:
            ticker = _extract_ticker(line)
            if ticker:
                signals.append({
                    "ticker": ticker, "strategy_id": "unknown",
                    "strategy_name": "", "signal_type": signal_type.upper(),
                    "date": _extract_ymd(line), "rsi_weekly": None,
                    "rsi_daily": None, "rsi_monthly": None,
                    "macd_line_weekly": None, "macd_signal_weekly": None,
                    "ssf50_weekly": None, "conditions": [],
                })
    return signals


# ── Urgent alerts parser ──────────────────────────────────────────────────────

def _parse_urgent_alerts(soup: BeautifulSoup) -> list[dict]:
    """
    Parse 'URGENT — Holdings Removed from Master Sheet' section.
    These are tickers from portfolio.get_removed_tickers() in Engine 2:
    held in Upstox but removed from the Google Sheet master list.
    Engine 2 still monitors them for exit signals.
    """
    alerts    = []
    text      = soup.get_text(separator="\n")
    lines     = [_clean(l) for l in text.split("\n") if _clean(l)]
    in_urgent = False

    for line in lines:
        ll = line.lower()
        if "urgent" in ll and ("removed" in ll or "master" in ll):
            in_urgent = True; continue
        if in_urgent:
            if any(kw in ll for kw in [
                "buy signal","sell signal","universe change",
                "next signal","generated by","live dashboard","📋"
            ]):
                in_urgent = False; continue
            ticker = _extract_ticker(line)
            if ticker:
                alerts.append({
                    "ticker": ticker,
                    "reason": "Removed from Engine 2 Google Sheet master list — exit signals still monitored",
                })

    return alerts


# ── Universe changes ──────────────────────────────────────────────────────────

def _parse_universe_changes(soup: BeautifulSoup) -> list[str]:
    changes    = []
    text       = soup.get_text(separator="\n")
    lines      = [_clean(l) for l in text.split("\n") if _clean(l)]
    in_section = False

    for line in lines:
        ll = line.lower()
        if "universe changes" in ll:
            in_section = True; continue
        if in_section:
            if "no changes" in ll:
                break
            if any(kw in ll for kw in ["buy signal","sell signal","next signal","📋","📈","📉"]):
                break
            if line and len(line) > 4:
                changes.append(line)

    return changes


# ── Master parser ─────────────────────────────────────────────────────────────

def parse_signal_email(email_data: dict) -> dict:
    """
    Parse Engine 2 email into structured dict.
    Input:  raw email dict from gmail_reader.fetch_latest_signal_email()
    Output: structured signal dict
    """
    subject = email_data.get("subject", "")
    soup    = email_data.get("soup") or BeautifulSoup(email_data.get("html",""), "html.parser")

    log.info(f"Parsing Engine 2 email: {subject[:80]}")

    summary          = _parse_summary(soup, subject)
    buy_signals      = _parse_signal_table(soup, "BUY")
    sell_signals     = _parse_signal_table(soup, "SELL")
    urgent_alerts    = _parse_urgent_alerts(soup)
    universe_changes = _parse_universe_changes(soup)

    result = {
        **summary,
        "buy_signals":          buy_signals,
        "sell_signals":         sell_signals,
        "urgent_alerts":        urgent_alerts,
        "universe_changes":     universe_changes,
        "source_email_subject": subject,
        "parsed_at":            datetime.now().isoformat(),
    }

    log.info(
        f"Engine 2 parsed ✓  date={result['signal_date']}  "
        f"buy={len(buy_signals)}  sell={len(sell_signals)}  "
        f"alerts={len(urgent_alerts)}"
    )
    return result
