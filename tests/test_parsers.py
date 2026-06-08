"""tests/test_parsers.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from bs4 import BeautifulSoup
from orchestrator.parsers.macro_parser  import parse_macro_email
from orchestrator.parsers.signal_parser import parse_signal_email

MACRO_HTML = """<html><body>
<table>
  <tr><td>Cycle Phase</td><td>EARLY EXPANSION Strong bull</td></tr>
  <tr><td>Composite Score</td><td>0.7665 (76.6%)</td></tr>
  <tr><td>Score Momentum</td><td>Stable</td></tr>
  <tr><td>Confidence</td><td>High Live: 15 | Estimated: 0 | Manual: 0</td></tr>
  <tr><td>Rebalance Signal</td><td>No</td></tr>
  <tr><td>Historical Precedent</td><td>2014-15, 2021-22</td></tr>
</table>
<p>⬆ Overweight Nifty Bank, Nifty Infra, Nifty Auto</p>
<p>➡ Neutral Nifty IT</p>
<p>⬇ Underweight Nifty FMCG</p>
<table>
  <tr><th>Name</th><th>Ticker</th><th>Cycle Stance</th><th>Tag</th>
      <th>Price</th><th>RSI(14)</th><th>4W Mom</th><th>MA Cross</th><th>Ann Vol</th></tr>
  <tr><td>Bank</td><td>BANKBEES.NS</td><td>Overweight</td><td>BUY</td><td>567</td><td>41</td><td>+6.5%</td><td>Death Cross</td><td>26%</td></tr>
  <tr><td>Infra</td><td>INFRABEES.NS</td><td>Overweight</td><td>BUY</td><td>970</td><td>59</td><td>+8.7%</td><td>Death Cross</td><td>19%</td></tr>
  <tr><td>IT</td><td>ITBEES.NS</td><td>Neutral</td><td>AVOID</td><td>32</td><td>22</td><td>-3.2%</td><td>Death Cross</td><td>31%</td></tr>
</table>
</body></html>"""

SIGNAL_HTML = """<html><body>
<p>Signal Date: 2026-05-01</p>
<p>Stocks in Universe 94 ETFs in Universe 18</p>
<p>Strategies Run S2 Weekly EMA Pullback | S4 Weekly SSF50 Breakout | S5 ETF Weekly</p>
<h2>URGENT — Holdings Removed from Master Sheet</h2>
<p>JUNIORBEES.NS</p><p>MON100.NS</p><p>WELSPUNLIV.NS</p>
<h2>BUY Signals (1)</h2>
<table>
  <tr><th>ticker</th><th>strategy_name</th><th>signal_type</th><th>date</th>
      <th>RSI14_weekly</th><th>SSF50_weekly</th><th>triggered_conditions</th></tr>
  <tr><td>INFRABEES.NS</td><td>Weekly ETF Breakout [Mod-1]</td><td>BUY</td>
      <td>2026-04-27</td><td>54.19</td><td>958.51</td>
      <td>price_crossed_above_SSF50_weekly | RSI14_above_RSI14_MA</td></tr>
</table>
<h2>SELL Signals (0)</h2><p>No SELL signals.</p>
<p>Next signal run: 08 May 2026</p>
</body></html>"""

def _macro():
    return parse_macro_email({"subject":"[Macro] EARLY EXPANSION | Score: 0.766 | Stable | 04-May-2026",
                               "html":MACRO_HTML,"soup":BeautifulSoup(MACRO_HTML,"html.parser")})
def _signal():
    return parse_signal_email({"subject":"Nifty 500 Signals — 2026-05-01 | 1 BUY | 0 SELL",
                                "html":SIGNAL_HTML,"soup":BeautifulSoup(SIGNAL_HTML,"html.parser")})

def test_macro_phase():        assert _macro()["phase"] == "EARLY EXPANSION"
def test_macro_score():        assert 0.70 < _macro()["score"] < 0.80
def test_macro_momentum():     assert _macro()["momentum"] == "Stable"
def test_macro_no_rebalance(): assert _macro()["rebalance_signal"] == False
def test_macro_etf_tickers():
    tickers = [e["ticker"] for e in _macro()["etf_tags"]]
    assert "BANKBEES.NS" in tickers and "INFRABEES.NS" in tickers
def test_signal_buy_count():   assert len(_signal()["buy_signals"]) == 1
def test_signal_buy_ticker():  assert _signal()["buy_signals"][0]["ticker"] == "INFRABEES.NS"
def test_signal_no_sell():     assert len(_signal()["sell_signals"]) == 0
def test_signal_alerts():
    tickers = [a["ticker"] for a in _signal()["urgent_alerts"]]
    assert "JUNIORBEES.NS" in tickers
def test_signal_strategy_id():
    sid = _signal()["buy_signals"][0]["strategy_id"]
    assert "S5" in sid or "etf" in sid.lower()

if __name__ == "__main__":
    for t in [test_macro_phase,test_macro_score,test_macro_momentum,test_macro_no_rebalance,
              test_macro_etf_tickers,test_signal_buy_count,test_signal_buy_ticker,
              test_signal_no_sell,test_signal_alerts,test_signal_strategy_id]:
        try: t(); print(f"  PASS  {t.__name__}")
        except Exception as e: print(f"  FAIL  {t.__name__} -> {e}")
