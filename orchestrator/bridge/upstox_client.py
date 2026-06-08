"""
orchestrator/bridge/upstox_client.py
──────────────────────────────────────
Fetches live holdings from Upstox API v2 — EQUITY + MUTUAL FUNDS.

EQUITY:  GET /v2/portfolio/long-term-holdings
MF:      GET /v2/mf/holdings

MF API response format (from Upstox docs, confirmed June 2026):
  {
    "instrument_key": "INF200K01T51",
    "folio": "3108290884",
    "fund": "SBI SMALL CAP FUND - DIRECT PLAN - GROWTH",
    "pnl": -703.68,
    "quantity": 110.0,
    "average_price": 181.80,
    "last_price": 175.43,
    "last_price_date": "2026-03-06",
    "pledged_quantity": 0
  }
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("upstox_client")

# ── MF Fund Name → Sleeve Classification ─────────────────────────────────────
# Maps keywords in the fund name to a sleeve + display ticker.
# Order matters — first match wins.
MF_SLEEVE_MAP = [
    # Gold → Hedge
    ("GOLD",                "Hedge",       "GOLDBEES.MF"),
    ("SILVER",              "Hedge",       "SILVERBEES.MF"),
    ("LIQUID",              "Hedge",       "LIQUIDBEES.MF"),
    # IT/Tech → Thematic
    ("TECHNOLOGY",          "Thematic",    "ITBEES.MF"),
    ("DIGITAL INDIA",       "Thematic",    "ITBEES.MF"),
    # Sector-specific → Thematic
    ("PHARMA",              "Thematic",    "PHARMABEES.MF"),
    ("HEALTHCARE",          "Thematic",    "PHARMABEES.MF"),
    ("INFRASTRUCTURE",      "Thematic",    "INFRABEES.MF"),
    ("BANKING",             "Thematic",    "BANKBEES.MF"),
    ("ENERGY",              "Thematic",    "ENERGYBEES.MF"),
    ("CONSUMPTION",         "Thematic",    "FMCGBEES.MF"),
    # International → International
    ("NASDAQ",              "International","MON100.MF"),
    ("S&P 500",             "International","MOUS500.MF"),
    ("US EQUITY",           "International","MOUS500.MF"),
    ("INTERNATIONAL",       "International","MON100.MF"),
    # Small/Mid cap → Core
    ("SMALL CAP",           "Core",        "SMALLCAP.MF"),
    ("MIDCAP",              "Core",        "MIDCAP.MF"),
    ("MID CAP",             "Core",        "MIDCAP.MF"),
    # Large cap / Flexi / Multi → Core
    ("LARGE CAP",           "Core",        "LARGECAP.MF"),
    ("FLEXI CAP",           "Core",        "FLEXICAP.MF"),
    ("MULTI CAP",           "Core",        "MULTICAP.MF"),
    ("LARGE & MID",         "Core",        "LARGECAP.MF"),
    ("INDEX",               "Core",        "INDEX.MF"),
    ("NIFTY 50",            "Core",        "NIFTYIETF.MF"),
    ("NIFTY50",             "Core",        "NIFTYIETF.MF"),
    # ELSS → Core (tax saver = diversified equity)
    ("ELSS",                "Core",        "ELSS.MF"),
    ("TAX SAVER",           "Core",        "ELSS.MF"),
    # Debt → Hedge
    ("DEBT",                "Hedge",       "DEBT.MF"),
    ("LONG DURATION",       "Hedge",       "DEBT.MF"),
    ("SHORT DURATION",      "Hedge",       "DEBT.MF"),
    ("MONEY MARKET",        "Hedge",       "LIQUIDBEES.MF"),
    ("OVERNIGHT",           "Hedge",       "LIQUIDBEES.MF"),
]


def _classify_mf(fund_name: str) -> tuple[str, str]:
    """Classify a MF fund name into (sleeve, display_ticker)."""
    upper = fund_name.upper()
    for keyword, sleeve, ticker in MF_SLEEVE_MAP:
        if keyword in upper:
            return sleeve, ticker
    return "Core", "OTHER.MF"  # default to Core


def _token() -> str:
    token = os.environ.get("UPSTOX_TOKEN", "").strip()
    if not token:
        raise EnvironmentError("UPSTOX_TOKEN not set.")
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
    }


# ── Equity Holdings ───────────────────────────────────────────────────────────

def fetch_equity_holdings() -> list[dict]:
    """GET /v2/portfolio/long-term-holdings — stocks + exchange-traded ETFs."""
    url = "https://api.upstox.com/v2/portfolio/long-term-holdings"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)
        if resp.status_code == 401:
            raise ValueError("UPSTOX_TOKEN expired (401). Engine 2 token push may not have run today.")
        if resp.status_code == 403:
            log.warning(f"Equity holdings 403 — may be IP restriction or token scope issue. Response: {resp.text[:200]}")
            return []
        resp.raise_for_status()

        holdings = []
        for item in resp.json().get("data", []):
            symbol = item.get("tradingsymbol", "")
            ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
            qty = float(item.get("quantity", 0))
            avg = float(item.get("average_price", 0))
            ltp = float(item.get("last_price", 0))
            if qty <= 0:
                continue
            holdings.append({
                "ticker":         ticker,
                "isin":           item.get("isin", ""),
                "quantity":       qty,
                "avg_cost_price": round(avg, 2),
                "last_price":     round(ltp, 2),
                "current_value":  round(qty * ltp, 2),
                "pnl":            round((ltp - avg) * qty, 2),
                "pnl_pct":        round((ltp - avg) / avg * 100, 2) if avg else 0.0,
                "exchange":       item.get("exchange", "NSE"),
                "company_name":   item.get("company_name", symbol),
                "asset_type":     "EQUITY",
            })
        log.info(f"Equity holdings: {len(holdings)}")
        return holdings
    except ValueError:
        raise
    except Exception as e:
        log.warning(f"Equity holdings failed: {e}")
        return []


# ── Mutual Fund Holdings ──────────────────────────────────────────────────────

def fetch_mf_holdings() -> list[dict]:
    """
    GET /v2/mf/holdings — mutual fund units including Gold Fund, ELSS, etc.

    Upstox MF API response fields (confirmed from docs June 2026):
      instrument_key, folio, fund, pnl, quantity, average_price,
      last_price, last_price_date, pledged_quantity
    """
    url = "https://api.upstox.com/v2/mf/holdings"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30)

        if resp.status_code == 401:
            log.warning("MF holdings: token expired (401).")
            return []
        if resp.status_code == 403:
            log.warning(f"MF holdings: 403 Forbidden — {resp.text[:200]}")
            return []
        if resp.status_code != 200:
            log.warning(f"MF holdings: HTTP {resp.status_code}")
            return []

        data = resp.json().get("data", [])
        if not data:
            log.info("MF holdings: empty (no mutual funds).")
            return []

        holdings = []
        for item in data:
            fund_name = item.get("fund", "")
            folio     = item.get("folio", "")
            qty       = float(item.get("quantity", 0))
            avg       = float(item.get("average_price", 0))
            ltp       = float(item.get("last_price", 0))
            pnl       = float(item.get("pnl", 0))

            if qty <= 0:
                continue

            cur_val = qty * ltp if ltp > 0 else qty * avg
            pnl_pct = ((ltp - avg) / avg * 100) if avg > 0 else 0.0

            # Classify into sleeve — use unique ticker per fund
            sleeve, sleeve_proxy = _classify_mf(fund_name)
            # Make ticker unique: use fund abbreviation + folio
            short_name = fund_name.split("-")[0].strip().replace(" ","")[:15]
            unique_ticker = f"{short_name}_{folio[:6]}.MF" if folio else f"{short_name}.MF"

            holdings.append({
                "ticker":         unique_ticker,
                "isin":           item.get("instrument_key", ""),
                "quantity":       qty,
                "avg_cost_price": round(avg, 4),
                "last_price":     round(ltp, 4),
                "current_value":  round(cur_val, 2),
                "pnl":            round(pnl, 2),
                "pnl_pct":        round(pnl_pct, 2),
                "exchange":       "MF",
                "company_name":   fund_name,
                "asset_type":     "MUTUAL_FUND",
                "folio":          folio,
                "sleeve":         sleeve,
                "sleeve_proxy":   sleeve_proxy,
            })

        log.info(f"MF holdings: {len(holdings)} funds")
        for h in holdings:
            log.info(f"  MF: {h['company_name'][:50]} → {h['sleeve']} sleeve ({h['ticker']}) Rs.{h['current_value']:,.0f}")
        return holdings

    except Exception as e:
        log.warning(f"MF holdings failed: {e}")
        return []


# ── Merged Holdings ───────────────────────────────────────────────────────────

def fetch_holdings() -> list[dict]:
    """Fetch ALL holdings — equity + mutual funds merged."""
    equity = fetch_equity_holdings()
    mf     = fetch_mf_holdings()
    all_h  = equity + mf
    log.info(f"Total holdings: {len(all_h)} (equity: {len(equity)}, MF: {len(mf)})")
    return all_h


def get_portfolio_snapshot() -> dict:
    holdings = fetch_holdings()
    return {
        "as_of":          datetime.now().isoformat(),
        "total_value":    round(sum(h["current_value"] for h in holdings), 2),
        "holdings":       holdings,
        "holdings_count": len(holdings),
        "equity_count":   sum(1 for h in holdings if h.get("asset_type") == "EQUITY"),
        "mf_count":       sum(1 for h in holdings if h.get("asset_type") == "MUTUAL_FUND"),
    }


def save_snapshot(snapshot: dict, cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"holdings_{datetime.now().strftime('%Y%m%d')}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    log.info(f"Holdings cached: {path}")


def load_snapshot(cache_dir: Path) -> dict | None:
    files = sorted(cache_dir.glob("holdings_*.json"), reverse=True)
    if files:
        with open(files[0]) as f:
            return json.load(f)
    return None
