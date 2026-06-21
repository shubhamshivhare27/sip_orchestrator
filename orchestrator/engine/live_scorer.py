"""
orchestrator/engine/live_scorer.py
───────────────────────────────────
12-indicator live scoring per ETF. Data: Upstox first, yfinance fallback.

SCORING (110 pts total):
  TREND (50):     SSF200(20) + SSF50(15) + Golden/Death Cross(15)
  MOMENTUM (24):  RSI Zone(12) + RSI×RSI_MA(12)
  STRUCTURE (18): Support/Resistance(10) + Fibonacci(8)
  MACRO (18):     PE(5) + VIX(4) + FII(4) + Volume(3) + OBV(2)

Signals: >=80% STRONG BUY | 65-79% BUY | 50-64% PARTIAL | 35-49% WATCH | <35% AVOID
"""
import logging, os, time
import numpy as np, pandas as pd, requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("live_scorer")

# ── Ticker alias map (Priority 3 fix) ──────────────────────────────────────────
# These internal shorthand tickers do NOT correspond to any real NSE trading
# symbol — confirmed against NSE/Yahoo Finance/TradingView listings. Both
# Upstox and yfinance were correctly returning nothing for them; this isn't
# an instrument-key-format or exchange-segment issue (there's no separate
# "NSE_ETF" segment in Upstox's API — ETFs live under NSE_EQ same as equities,
# confirmed via Upstox's own instrument search docs). The fix is simply using
# the right symbol. Internal ticker strings stay unchanged everywhere else in
# the system (instrument_scorer, tranche_manager, SIP_Strategy.xlsx, etc.) —
# this alias is applied ONLY at this data-fetch boundary, confirmed safe to
# scope here since nothing downstream depends on these literal strings
# matching the exchange symbol.
TICKER_ALIAS = {
    "HSET":       "HNGSNGBEES",  # Nippon India ETF Hang Seng BeES
    "FMCGBEES":   "FMCGIETF",    # ICICI Prudential Nifty FMCG ETF
    "METALBEES":  "METALIETF",   # ICICI Prudential Nifty Metal ETF
    "ENERGYBEES": "MOENERGY",    # Motilal Oswal Nifty Energy ETF
}

def _resolve_symbol(ticker):
    return TICKER_ALIAS.get(ticker, ticker)

# ── Data fetchers ─────────────────────────────────────────────────────────────
def _fetch_upstox(ticker, days=400):
    token = os.environ.get("UPSTOX_TOKEN","").strip()
    if not token: return None
    sym = _resolve_symbol(ticker).replace(".NS","")
    key = f"NSE_EQ%7C{sym}"
    to_d = datetime.today().strftime("%Y-%m-%d")
    fr_d = (datetime.today()-timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(f"https://api.upstox.com/v2/historical-candle/{key}/day/{to_d}/{fr_d}",
            headers={"Authorization":f"Bearer {token}","Accept":"application/json"},timeout=30)
        if r.status_code!=200: return None
        candles = r.json().get("data",{}).get("candles",[])
        if not candles or len(candles)<50: return None
        df = pd.DataFrame(candles,columns=["date","open","high","low","close","volume","oi"])
        df["date"]=pd.to_datetime(df["date"]).dt.normalize()
        df=df[["date","open","high","low","close","volume"]].sort_values("date").reset_index(drop=True)
        for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        df = df.dropna(subset=["close"])
        # Pre-market guard: a 4 AM run can occasionally pick up an incomplete
        # same-day candle (zero volume) before the exchange has actually
        # traded — drop it and fall back to the last complete session.
        if len(df) and df["volume"].iloc[-1] == 0:
            df = df.iloc[:-1]
        return df
    except: return None

def _fetch_yf(ticker, days=400):
    try:
        import yfinance as yf
        yf_symbol = f"{_resolve_symbol(ticker).replace('.NS','')}.NS"
        tk=yf.Ticker(yf_symbol)
        df=tk.history(start=(datetime.today()-timedelta(days=days)).strftime("%Y-%m-%d"),
            end=datetime.today().strftime("%Y-%m-%d"),interval="1d",auto_adjust=True,actions=False)
        if df is None or df.empty or len(df)<50: return None
        df=df.reset_index().rename(columns={"Date":"date","Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
        df["date"]=pd.to_datetime(df["date"]).dt.normalize()
        df = df[["date","open","high","low","close","volume"]].sort_values("date").reset_index(drop=True)
        if len(df) and df["volume"].iloc[-1] == 0:
            df = df.iloc[:-1]
        return df
    except: return None

def fetch_daily(ticker, days=400):
    df = _fetch_upstox(ticker, days)
    if df is not None and len(df)>=50: return df
    df = _fetch_yf(ticker, days)
    if df is not None and len(df)>=50: return df
    log.warning(f"{ticker}: no data"); return None

def fetch_india_vix():
    try:
        import yfinance as yf
        h=yf.Ticker("^INDIAVIX").history(period="5d")
        if h is not None and not h.empty: return float(h["Close"].iloc[-1])
    except: pass
    return None

def fetch_fii_flow():
    try:
        import yfinance as yf
        h=yf.Ticker("^NSEI").history(period="1mo")
        if h is not None and not h.empty and len(h)>5:
            ret=(h["Close"].iloc[-1]/h["Close"].iloc[0]-1)*100
            if ret>3: return 5000
            elif ret>0: return 2000
            elif ret>-3: return -1000
            else: return -5000
    except: pass
    return None

def fetch_nifty_pe():
    try:
        import yfinance as yf
        info=yf.Ticker("^NSEI").info
        pe=info.get("trailingPE") or info.get("forwardPE")
        if pe: return float(pe)
    except: pass
    return None

# ── Technical indicators ──────────────────────────────────────────────────────
def _ssf(s, p):
    a=np.exp(-1.414*np.pi/p); b=2*a*np.cos(1.414*np.pi/p)
    c2=b; c3=-a*a; c1=1-c2-c3
    r=s.copy().astype(float)
    for i in range(2,len(r)):
        r.iloc[i]=c1*(s.iloc[i]+s.iloc[i-1])/2+c2*r.iloc[i-1]+c3*r.iloc[i-2]
    return r

def _sma(s,p): return s.rolling(window=p,min_periods=p).mean()

def _rsi(s,p=14):
    d=s.diff(); g=d.clip(lower=0); l=(-d).clip(lower=0)
    ag=g.rolling(p,min_periods=p).mean(); al=l.rolling(p,min_periods=p).mean()
    return 100-100/(1+ag/al)

def _obv(c,v): return (v*np.sign(c.diff())).cumsum()

def _fib(hi,lo):
    d=hi-lo
    return {0.236:hi-0.236*d, 0.382:hi-0.382*d, 0.5:hi-0.5*d, 0.618:hi-0.618*d}

# ── Score dataclasses ─────────────────────────────────────────────────────────
@dataclass
class IndScore:
    name:str; category:str; max_points:int; points:int; value:str; detail:str

@dataclass
class ETFScore:
    ticker:str; total_points:int=0; max_points:int=110; pct:float=0.0
    signal:str="WATCH"; indicators:list=field(default_factory=list)
    price:Optional[float]=None; error:Optional[str]=None

# ── Main scoring function ─────────────────────────────────────────────────────
def score_etf(ticker, df, vix=None, fii=None, pe=None):
    if df is None or len(df)<200:
        return ETFScore(ticker=ticker,error="Insufficient data")
    close=df["close"]; high=df["high"]; low=df["low"]; vol=df["volume"]
    price=float(close.iloc[-1])
    inds=[]

    # 1. SSF200 (20pts)
    s200=_ssf(close,200); s200n=float(s200.iloc[-1]); s200p=float(s200.iloc[-5])
    if price>s200n and s200n>s200p: p,d=20,f"Above rising SSF200 ({s200n:.1f})"
    elif price>s200n: p,d=14,f"Above flat SSF200 ({s200n:.1f})"
    elif s200n>s200p: p,d=8,f"Below rising SSF200 ({s200n:.1f})"
    else: p,d=3,f"Below falling SSF200 ({s200n:.1f})"
    inds.append(IndScore("SSF200","Trend",20,p,f"{s200n:.1f}",d))

    # 2. SSF50 (15pts)
    s50=_ssf(close,50); s50n=float(s50.iloc[-1]); s50p=float(s50.iloc[-5])
    if price>s50n and s50n>s50p: p,d=15,f"Above rising SSF50 ({s50n:.1f})"
    elif price>s50n: p,d=10,f"Above flat SSF50 ({s50n:.1f})"
    elif s50n>s50p: p,d=6,f"Below rising SSF50 ({s50n:.1f})"
    else: p,d=2,f"Below falling SSF50 ({s50n:.1f})"
    inds.append(IndScore("SSF50","Trend",15,p,f"{s50n:.1f}",d))

    # 3. Golden/Death Cross (15pts)
    ma50=_sma(close,50); ma200=_sma(close,200)
    ma50n=float(ma50.iloc[-1]); ma200n=float(ma200.iloc[-1])
    ma50p=float(ma50.iloc[-6]) if len(ma50)>5 else ma50n
    ma200p=float(ma200.iloc[-6]) if len(ma200)>5 else ma200n
    if ma50n>ma200n:
        if ma50p<=ma200p: p,d=15,"Fresh Golden Cross"
        else: p,d=12,f"Golden Cross active (SMA50={ma50n:.0f}>SMA200={ma200n:.0f})"
    else:
        if ma50p>=ma200p: p,d=2,"Fresh Death Cross"
        else: p,d=5,f"Death Cross active (SMA50={ma50n:.0f}<SMA200={ma200n:.0f})"
    inds.append(IndScore("Golden/Death Cross","Trend",15,p,"Golden" if ma50n>ma200n else "Death",d))

    # 4. RSI Zone (12pts)
    rsi=_rsi(close,14); rn=float(rsi.iloc[-1])
    if 40<=rn<=60: p,d=12,f"RSI {rn:.1f} neutral healthy"
    elif 30<=rn<40: p,d=10,f"RSI {rn:.1f} approaching oversold"
    elif 60<rn<=70: p,d=9,f"RSI {rn:.1f} bullish"
    elif rn<30: p,d=7,f"RSI {rn:.1f} oversold contrarian"
    else: p,d=4,f"RSI {rn:.1f} overbought"
    inds.append(IndScore("RSI Zone","Momentum",12,p,f"{rn:.1f}",d))

    # 5. RSI x RSI_MA (12pts)
    rma=_sma(rsi,14); rmn=float(rma.iloc[-1]) if not pd.isna(rma.iloc[-1]) else 50
    prev_rsi=float(rsi.iloc[-2]) if len(rsi)>1 else rn
    prev_rma=float(rma.iloc[-2]) if len(rma)>1 and not pd.isna(rma.iloc[-2]) else rmn
    if rn>rmn and prev_rsi<=prev_rma: p,d=12,f"Fresh RSI bullish cross ({rn:.1f}>{rmn:.1f})"
    elif rn>rmn: p,d=9,f"RSI above RSI_MA ({rn:.1f}>{rmn:.1f})"
    elif rn<rmn and prev_rsi>=prev_rma: p,d=3,"Fresh RSI bearish cross"
    else: p,d=5,f"RSI below RSI_MA ({rn:.1f}<{rmn:.1f})"
    inds.append(IndScore("RSI×RSI_MA","Momentum",12,p,f"{rn:.1f}/{rmn:.1f}",d))

    # 6. Support/Resistance (10pts)
    h20=float(high.tail(20).max()); l52=float(low.tail(252).min()); h52=float(high.tail(252).max())
    rng=h52-l52
    pos=((price-l52)/rng*100) if rng>0 else 50
    if price>=h20*0.98: p,d=10,f"Near 20D high breakout zone"
    elif pos<30: p,d=8,f"Near 52W support ({pos:.0f}%)"
    elif pos<50: p,d=6,f"Lower half ({pos:.0f}%)"
    elif pos<70: p,d=5,f"Mid range ({pos:.0f}%)"
    else: p,d=3,f"Upper range ({pos:.0f}%)"
    inds.append(IndScore("Support/Resistance","Structure",10,p,f"{pos:.0f}%",d))

    # 7. Fibonacci (8pts)
    sh=float(high.tail(60).max()); sl=float(low.tail(60).min())
    fb=_fib(sh,sl)
    if price>=fb[0.236]: p,d=8,"Above 23.6% Fib"
    elif price>=fb[0.382]: p,d=7,"23.6-38.2% Fib pullback"
    elif price>=fb[0.5]: p,d=5,"At 50% Fib level"
    elif price>=fb[0.618]: p,d=4,"50-61.8% deep correction"
    else: p,d=2,"Below 61.8% Fib — trend broken"
    inds.append(IndScore("Fibonacci","Structure",8,p,f"{price:.1f}",d))

    # 8. PE ratio (5pts)
    if pe:
        if pe<18: p,d=5,f"PE {pe:.1f} undervalued"
        elif pe<22: p,d=4,f"PE {pe:.1f} near avg"
        elif pe<26: p,d=3,f"PE {pe:.1f} above avg"
        else: p,d=1,f"PE {pe:.1f} expensive"
    else: p,d=3,"PE N/A"
    inds.append(IndScore("PE Ratio","Macro",5,p,f"{pe:.1f}" if pe else "N/A",d))

    # 9. VIX (4pts)
    if vix:
        if vix<13: p,d=4,f"VIX {vix:.1f} calm"
        elif vix<18: p,d=3,f"VIX {vix:.1f} normal"
        elif vix<25: p,d=2,f"VIX {vix:.1f} elevated"
        else: p,d=1,f"VIX {vix:.1f} panic"
    else: p,d=2,"VIX N/A"
    inds.append(IndScore("India VIX","Macro",4,p,f"{vix:.1f}" if vix else "N/A",d))

    # 10. FII flow (4pts)
    if fii:
        if fii>3000: p,d=4,f"FII strong buying ~{fii:.0f}Cr"
        elif fii>0: p,d=3,f"FII mild buying ~{fii:.0f}Cr"
        elif fii>-3000: p,d=2,f"FII mild selling ~{fii:.0f}Cr"
        else: p,d=1,f"FII heavy selling ~{fii:.0f}Cr"
    else: p,d=2,"FII N/A"
    inds.append(IndScore("FII Flow","Macro",4,p,f"{fii:.0f}Cr" if fii else "N/A",d))

    # 11. Volume vs 20D (3pts)
    va=float(vol.tail(20).mean()); vn=float(vol.iloc[-1])
    vr=vn/va if va>0 else 1
    if vr>1.5: p,d=3,f"Vol {vr:.1f}x high conviction"
    elif vr>0.8: p,d=2,f"Vol {vr:.1f}x normal"
    else: p,d=1,f"Vol {vr:.1f}x weak"
    inds.append(IndScore("Volume","Macro",3,p,f"{vr:.1f}x",d))

    # 12. OBV (2pts)
    obv=_obv(close,vol); obv_ma=_sma(obv,20)
    on=float(obv.iloc[-1]); om=float(obv_ma.iloc[-1]) if not pd.isna(obv_ma.iloc[-1]) else on
    if on>om: p,d=2,"OBV accumulation"
    else: p,d=0,"OBV distribution"
    inds.append(IndScore("OBV","Macro",2,p,"Accum" if on>om else "Distrib",d))

    total=sum(i.points for i in inds)
    pct=round(total/110*100,1)
    if pct>=80: sig="STRONG BUY"
    elif pct>=65: sig="BUY"
    elif pct>=50: sig="PARTIAL"
    elif pct>=35: sig="WATCH"
    else: sig="AVOID"
    return ETFScore(ticker=ticker,total_points=total,pct=pct,signal=sig,indicators=inds,price=price)

# ── Mutual-fund (NAV-priced) holdings: MOUS500 ─────────────────────────────────
# MOUS500 (Motilal Oswal S&P 500 Index Fund, Direct Growth, ISIN INF247L01AG2)
# is held as mutual fund units, not an exchange-traded security — confirmed
# with the user. It has no candle/OHLCV data (no open/high/low/volume, only a
# daily NAV print), so it can't go through score_etf() or the Upstox/yfinance
# candle fetchers above. NAV history comes from mfapi.in (a free, no-auth
# wrapper around AMFI's official daily NAV data — see mfapi.in/docs).
#
# Scope reduction: Structure (Support/Resistance, Fibonacci) and Volume/OBV
# don't apply — NAV genuinely carries none of that information, this isn't a
# shortcut. PE/VIX/FII are also excluded here since they describe Indian
# market conditions and aren't a meaningful entry-timing signal for a global
# S&P 500 tracker. The score is rescaled to the 5 indicators that DO carry
# through: SSF200(20) + SSF50(15) + Golden/Death Cross(15) + RSI Zone(12) +
# RSI×RSI_MA(12) = 74 points max, same 80/65/50/35% signal thresholds.
MF_NAV_SCHEME_MAP = {
    # ticker -> mfapi.in numeric scheme code (preferred, set once you've
    # looked it up) OR a scheme-name search query (resolved at runtime,
    # slower and slightly less precise — picks the first Direct+Growth match).
    # Override either via env var without touching this file.
    "MOUS500": os.environ.get("MOUS500_MF_SCHEME_CODE")
               or os.environ.get("MOUS500_MF_SCHEME_QUERY", "Motilal Oswal S&P 500 Index Fund Direct Growth"),
}
MF_NAV_TICKERS = set(MF_NAV_SCHEME_MAP.keys())

def _fetch_mf_nav(ticker, days=400):
    query_or_code = MF_NAV_SCHEME_MAP.get(ticker)
    if not query_or_code: return None
    try:
        scheme_code = query_or_code if str(query_or_code).isdigit() else None
        if scheme_code is None:
            r = requests.get("https://api.mfapi.in/mf/search", params={"q": query_or_code}, timeout=15)
            if r.status_code != 200: return None
            results = r.json()
            if not results: return None
            match = next((x for x in results
                          if "direct" in x["schemeName"].lower() and "growth" in x["schemeName"].lower()),
                         results[0])
            scheme_code = str(match["schemeCode"])
            log.info(f"  {ticker}: resolved to mfapi.in scheme {scheme_code} ({match['schemeName']})")
        r2 = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=20)
        if r2.status_code != 200: return None
        records = r2.json().get("data", [])
        if not records or len(records) < 200: return None
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors="coerce")
        df["close"] = pd.to_numeric(df["nav"], errors="coerce")
        return df.dropna(subset=["date","close"]).sort_values("date").tail(days).reset_index(drop=True)
    except Exception as e:
        log.warning(f"{ticker}: mfapi.in fetch failed ({e})")
        return None

def score_mf_nav(ticker, nav_df):
    if nav_df is None or len(nav_df) < 200:
        return ETFScore(ticker=ticker, error="Insufficient NAV history")
    close = nav_df["close"]; price = float(close.iloc[-1])
    inds = []

    s200=_ssf(close,200); s200n=float(s200.iloc[-1]); s200p=float(s200.iloc[-5])
    if price>s200n and s200n>s200p: p,d=20,f"Above rising SSF200 ({s200n:.2f})"
    elif price>s200n: p,d=14,f"Above flat SSF200 ({s200n:.2f})"
    elif s200n>s200p: p,d=8,f"Below rising SSF200 ({s200n:.2f})"
    else: p,d=3,f"Below falling SSF200 ({s200n:.2f})"
    inds.append(IndScore("SSF200","Trend",20,p,f"{s200n:.2f}",d))

    s50=_ssf(close,50); s50n=float(s50.iloc[-1]); s50p=float(s50.iloc[-5])
    if price>s50n and s50n>s50p: p,d=15,f"Above rising SSF50 ({s50n:.2f})"
    elif price>s50n: p,d=10,f"Above flat SSF50 ({s50n:.2f})"
    elif s50n>s50p: p,d=6,f"Below rising SSF50 ({s50n:.2f})"
    else: p,d=2,f"Below falling SSF50 ({s50n:.2f})"
    inds.append(IndScore("SSF50","Trend",15,p,f"{s50n:.2f}",d))

    ma50=_sma(close,50); ma200=_sma(close,200)
    ma50n=float(ma50.iloc[-1]); ma200n=float(ma200.iloc[-1])
    ma50p=float(ma50.iloc[-6]) if len(ma50)>5 else ma50n
    ma200p=float(ma200.iloc[-6]) if len(ma200)>5 else ma200n
    if ma50n>ma200n:
        if ma50p<=ma200p: p,d=15,"Fresh Golden Cross"
        else: p,d=12,f"Golden Cross active (SMA50={ma50n:.2f}>SMA200={ma200n:.2f})"
    else:
        if ma50p>=ma200p: p,d=2,"Fresh Death Cross"
        else: p,d=5,f"Death Cross active (SMA50={ma50n:.2f}<SMA200={ma200n:.2f})"
    inds.append(IndScore("Golden/Death Cross","Trend",15,p,"Golden" if ma50n>ma200n else "Death",d))

    rsi=_rsi(close,14); rn=float(rsi.iloc[-1])
    if 40<=rn<=60: p,d=12,f"RSI {rn:.1f} neutral healthy"
    elif 30<=rn<40: p,d=10,f"RSI {rn:.1f} approaching oversold"
    elif 60<rn<=70: p,d=9,f"RSI {rn:.1f} bullish"
    elif rn<30: p,d=7,f"RSI {rn:.1f} oversold contrarian"
    else: p,d=4,f"RSI {rn:.1f} overbought"
    inds.append(IndScore("RSI Zone","Momentum",12,p,f"{rn:.1f}",d))

    rma=_sma(rsi,14); rmn=float(rma.iloc[-1]) if not pd.isna(rma.iloc[-1]) else 50
    prev_rsi=float(rsi.iloc[-2]) if len(rsi)>1 else rn
    prev_rma=float(rma.iloc[-2]) if len(rma)>1 and not pd.isna(rma.iloc[-2]) else rmn
    if rn>rmn and prev_rsi<=prev_rma: p,d=12,f"Fresh RSI bullish cross ({rn:.1f}>{rmn:.1f})"
    elif rn>rmn: p,d=9,f"RSI above RSI_MA ({rn:.1f}>{rmn:.1f})"
    elif rn<rmn and prev_rsi>=prev_rma: p,d=3,"Fresh RSI bearish cross"
    else: p,d=5,f"RSI below RSI_MA ({rn:.1f}<{rmn:.1f})"
    inds.append(IndScore("RSI×RSI_MA","Momentum",12,p,f"{rn:.1f}/{rmn:.1f}",d))

    total=sum(i.points for i in inds)
    max_pts=sum(i.max_points for i in inds)  # 74, not 110 — see note above
    pct=round(total/max_pts*100,1)
    if pct>=80: sig="STRONG BUY"
    elif pct>=65: sig="BUY"
    elif pct>=50: sig="PARTIAL"
    elif pct>=35: sig="WATCH"
    else: sig="AVOID"
    return ETFScore(ticker=ticker,total_points=total,max_points=max_pts,pct=pct,signal=sig,indicators=inds,price=price)

def score_all_etfs(tickers):
    log.info(f"Scoring {len(tickers)} ETFs (12-indicator system)...")
    vix=fetch_india_vix(); fii=fetch_fii_flow(); pe=fetch_nifty_pe()
    log.info(f"  Macro: VIX={vix} FII={fii} PE={pe}")
    scores={}
    for i,t in enumerate(tickers,1):
        log.info(f"  [{i}/{len(tickers)}] {t}...")
        if t in MF_NAV_TICKERS:
            nav_df = _fetch_mf_nav(t, 400)
            scores[t] = score_mf_nav(t, nav_df)
        else:
            df=fetch_daily(t,400)
            scores[t]=score_etf(t,df,vix,fii,pe)
        log.info(f"    {scores[t].total_points}/{scores[t].max_points} ({scores[t].pct}%) = {scores[t].signal}")
        time.sleep(0.2)
    return scores

def scores_to_dict(scores):
    return [{
        "ticker":s.ticker,"price":s.price,"total_points":s.total_points,
        "max_points":s.max_points,"pct":s.pct,"signal":s.signal,"error":s.error,
        "indicators":[{"name":i.name,"category":i.category,"max_points":i.max_points,
            "points":i.points,"value":i.value,"detail":i.detail} for i in s.indicators],
    } for s in scores.values()]
