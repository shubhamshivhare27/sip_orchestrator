# Hybrid SIP Orchestrator v4

13-step pipeline: Sleeve-first allocation → Engine 2 priority override → Per-sleeve tranche deployment → Thematic cycle rotation.

## Key Features
- **Sleeve-first**: SIP always splits 60/20/15/5 (drift-adjusted) before any tranche logic
- **Engine 2 priority**: BUY signals override even PAUSED sleeves; borrows from underweight
- **Per-sleeve tranches**: Each sleeve gets A(50%)/B(30%)/C(20%), max 1 deployment per week
- **Per-ETF rules**: GOLDBEES fixed, MOMOMENTUM skip mode, HSET 0.5× default
- **12-indicator scoring**: 110 points per ETF from live Upstox/yfinance data
- **Thematic rotation**: Engine 3 phase change → EXIT old ETFs, ENTER new cycle ETFs
- **Carry forward**: Unused B/C tranches roll to next month

## 4-Phase Business Cycle
| Score | Phase | Outperform |
|---|---|---|
| 0.80-1.00 | STRONG RECOVERY | Financials, Realty, Auto, Infra |
| 0.60-0.80 | MID EXPANSION | IT, Metals, Energy, Infra |
| 0.40-0.60 | LATE CYCLE | Energy, Metals, Healthcare, FMCG |
| 0.00-0.40 | CONTRACTION | FMCG, Healthcare, Pharma, IT |

## Trigger Priority
1. Engine 2 BUY signal → deploy immediately (override PAUSED)
2. Engine 3 phase change → deploy Thematic tranche
3. Market dip (RSI/VIX) → deploy per-sleeve tranches
4. 3rd Thursday fallback → Tranche A at 1×

*Personal investment research. Not financial advice.*
