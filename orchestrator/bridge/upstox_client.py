"""
orchestrator/bridge/upstox_client.py (v4)
Fetches equity + MF holdings from Upstox. Classifies MFs into sleeves.
"""
import json, logging, os, requests
from datetime import datetime
from pathlib import Path

log = logging.getLogger("upstox_client")

def _token():
    t = os.environ.get("UPSTOX_TOKEN","").strip()
    if not t: raise EnvironmentError("UPSTOX_TOKEN not set.")
    return t

# ── MF classification ─────────────────────────────────────────────────────────
MF_SLEEVE_MAP = [
    ("GOLD",       "Hedge",    "GOLD"),
    ("SILVER",     "Hedge",    "SILVER"),
    ("LIQUID",     "Hedge",    "LIQUID"),
    ("TECHNOLOGY", "Thematic", "TECH"),
    ("DIGITAL",    "Thematic", "TECH"),
    ("SMALL CAP",  "Core",     "SMCAP"),
    ("SMALLCAP",   "Core",     "SMCAP"),
    ("MIDCAP",     "Core",     "MDCAP"),
    ("MID CAP",    "Core",     "MDCAP"),
    ("LARGE CAP",  "Core",     "LGCAP"),
    ("FLEXI CAP",  "Core",     "FLEXICAP"),
    ("FLEXICAP",   "Core",     "FLEXICAP"),
    ("ELSS",       "Core",     "ELSS"),
    ("TAX SAVER",  "Core",     "ELSS"),
    ("DEBT",       "Hedge",    "DEBT"),
    ("BOND",       "Hedge",    "BOND"),
]

def _classify_mf(fund_name: str) -> tuple:
    upper = fund_name.upper()
    for keyword, sleeve, tag in MF_SLEEVE_MAP:
        if keyword in upper:
            return sleeve, tag
    return "Core", "OTHER"

# ── Equity holdings ───────────────────────────────────────────────────────────
def fetch_equity_holdings():
    url = "https://api.upstox.com/v2/portfolio/long-term-holdings"
    resp = requests.get(url, headers={"Authorization":f"Bearer {_token()}","Accept":"application/json"}, timeout=30)
    if resp.status_code == 401:
        raise ValueError("UPSTOX_TOKEN expired (401).")
    resp.raise_for_status()
    holdings = []
    for item in resp.json().get("data",[]):
        sym = item.get("tradingsymbol","")
        ticker = f"{sym}.NS" if not sym.endswith(".NS") else sym
        qty = float(item.get("quantity",0))
        avg = float(item.get("average_price",0))
        ltp = float(item.get("last_price",0))
        if qty <= 0: continue
        holdings.append({
            "ticker":ticker,"isin":item.get("isin",""),"quantity":qty,
            "avg_cost_price":round(avg,2),"last_price":round(ltp,2),
            "current_value":round(qty*ltp,2),
            "pnl":round((ltp-avg)*qty,2),
            "pnl_pct":round((ltp-avg)/avg*100,2) if avg else 0.0,
            "exchange":item.get("exchange","NSE"),
            "company_name":item.get("company_name",sym),
            "asset_type":"EQUITY",
        })
    log.info(f"Equity holdings: {len(holdings)}")
    return holdings

# ── MF holdings ───────────────────────────────────────────────────────────────
def fetch_mf_holdings():
    url = "https://api.upstox.com/v2/mf/holdings"
    try:
        resp = requests.get(url, headers={"Authorization":f"Bearer {_token()}","Accept":"application/json"}, timeout=30)
        if resp.status_code in (401,403):
            log.warning(f"MF holdings: HTTP {resp.status_code}")
            return []
        if resp.status_code != 200:
            log.warning(f"MF holdings: HTTP {resp.status_code}")
            return []
        data = resp.json().get("data",[])
        if not data:
            log.info("MF holdings: empty"); return []

        holdings = []
        for item in data:
            fund_name = item.get("scheme_name") or item.get("fund_name") or item.get("trading_symbol") or item.get("tradingsymbol") or ""
            folio = str(item.get("folio") or item.get("folio_number") or "")
            qty = float(item.get("units") or item.get("quantity") or 0)
            avg = float(item.get("average_nav") or item.get("average_price") or item.get("avg_price") or 0)
            ltp = float(item.get("last_nav") or item.get("last_price") or item.get("nav") or 0)
            cur_val = float(item.get("current_value") or item.get("market_value") or 0)
            if qty <= 0: continue
            if cur_val <= 0 and ltp > 0: cur_val = qty * ltp
            pnl = cur_val - (qty * avg) if avg > 0 else 0
            pnl_pct = ((ltp - avg) / avg * 100) if avg > 0 else 0.0

            sleeve, tag = _classify_mf(fund_name)
            short = fund_name.split("-")[0].strip().replace(" ","")[:15]
            folio_short = folio[:6] if folio else "X"
            unique_ticker = f"{short}_{folio_short}.MF"

            holdings.append({
                "ticker":unique_ticker,"isin":item.get("isin",""),
                "quantity":qty,"avg_cost_price":round(avg,4),"last_price":round(ltp,4),
                "current_value":round(cur_val,2),"pnl":round(pnl,2),"pnl_pct":round(pnl_pct,2),
                "exchange":"MF","company_name":fund_name,"asset_type":"MUTUAL_FUND",
                "folio":folio,"sleeve":sleeve,"mf_tag":tag,
            })
        log.info(f"MF holdings: {len(holdings)}")
        return holdings
    except Exception as e:
        log.warning(f"MF holdings failed: {e}"); return []

# ── Combined ──────────────────────────────────────────────────────────────────
def fetch_holdings():
    equity = fetch_equity_holdings()
    mf = fetch_mf_holdings()
    all_h = equity + mf  # NO merge — each holding is unique
    log.info(f"Total holdings: {len(all_h)} (equity: {len(equity)}, MF: {len(mf)})")
    return all_h

def get_portfolio_snapshot():
    holdings = fetch_holdings()
    return {
        "as_of":datetime.now().isoformat(),
        "total_value":round(sum(h["current_value"] for h in holdings),2),
        "holdings":holdings,"holdings_count":len(holdings),
        "equity_count":sum(1 for h in holdings if h.get("asset_type")=="EQUITY"),
        "mf_count":sum(1 for h in holdings if h.get("asset_type")=="MUTUAL_FUND"),
    }

def save_snapshot(snapshot, cache_dir):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    path = Path(cache_dir) / f"holdings_{datetime.now().strftime('%Y%m%d')}.json"
    with open(path,"w") as f: json.dump(snapshot,f,indent=2,default=str)
    log.info(f"Holdings cached: {path}")

def load_snapshot(cache_dir):
    files = sorted(Path(cache_dir).glob("holdings_*.json"), reverse=True)
    if files:
        with open(files[0]) as f: return json.load(f)
    return None
