"""tests/test_engine.py"""
import sys
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator.engine.allocation_engine import classify_holdings, compute_portfolio_weights, compute_sip_allocation
from orchestrator.engine.buy_date_resolver import _monthly_sip_date, _next_trading_day, resolve_exit_date

CFG = {
    "sleeves": {
        "Core":     {"label":"Core","target_pct":60,"instruments":["SETFNIF50.NS"]},
        "Tactical": {"label":"Tactical","target_pct":25,"instruments":["BANKBEES.NS","INFRABEES.NS"]},
        "Thematic": {"label":"Thematic","target_pct":10,"instruments":["MON100.NS"]},
        "Hedge":    {"label":"Hedge","target_pct":5, "instruments":["GOLDBEES.NS","LIQUIDBEES.NS"]},
    },
    "sleeve_rules": {
        "underweight_aggressive_threshold_pct":3,"overweight_stop_threshold_pct":5,
        "underweight_budget_share":0.65,"cycle_boost_pct":4,
        "cycle_boost_phases":["EARLY EXPANSION"],"cycle_boost_sleeve":"Tactical",
        "min_instrument_allocation_inr":500,
    },
    "nse_holidays": ["2026-05-01"],
}

HOLDINGS = [
    {"ticker":"SETFNIF50.NS","quantity":120,"avg_cost_price":245,"last_price":257,"current_value":30840},
    {"ticker":"BANKBEES.NS", "quantity":80, "avg_cost_price":540,"last_price":567,"current_value":45360},
    {"ticker":"GOLDBEES.NS", "quantity":200,"avg_cost_price":115,"last_price":123,"current_value":24600},
    {"ticker":"MON100.NS",   "quantity":30, "avg_cost_price":85, "last_price":78, "current_value":2340},
]

def test_classify():
    c = classify_holdings(HOLDINGS, CFG)
    m = {h["ticker"]:h["sleeve"] for h in c}
    assert m["SETFNIF50.NS"]=="Core" and m["BANKBEES.NS"]=="Tactical" and m["GOLDBEES.NS"]=="Hedge"

def test_weights_sum():
    c = classify_holdings(HOLDINGS, CFG)
    w = compute_portfolio_weights(c)
    assert abs(sum(v["weight_pct"] for v in w["by_sleeve"].values()) - 100) < 0.5

def test_overweight_paused():
    c = classify_holdings(HOLDINGS, CFG)
    w = compute_portfolio_weights(c)
    p = compute_sip_allocation(50000, w, CFG, "EARLY EXPANSION")
    assert p.sleeves["Hedge"].sip_allocation == 0  # Hedge ~24% >> 5% threshold

def test_sip_not_exceed():
    c = classify_holdings(HOLDINGS, CFG)
    w = compute_portfolio_weights(c)
    p = compute_sip_allocation(50000, w, CFG, "EARLY EXPANSION")
    assert p.total_allocated <= 55000

def test_monthly_before_15():
    d = _monthly_sip_date(date(2026,5,8), 15, set())
    assert d.month==5 and d.day<=15

def test_monthly_after_15():
    d = _monthly_sip_date(date(2026,5,20), 15, set())
    assert d.month==6

def test_next_trading_skips_weekend():
    r = _next_trading_day(date(2026,5,9), set())  # Saturday
    assert r.weekday() < 5

def test_exit_date():
    r = resolve_exit_date(date(2026,5,8), CFG)
    assert r["type"] == "NEXT_TRADING_DAY"

if __name__ == "__main__":
    for t in [test_classify,test_weights_sum,test_overweight_paused,test_sip_not_exceed,
              test_monthly_before_15,test_monthly_after_15,test_next_trading_skips_weekend,test_exit_date]:
        try: t(); print(f"  PASS  {t.__name__}")
        except Exception as e: print(f"  FAIL  {t.__name__} -> {e}")
