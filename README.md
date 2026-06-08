# Hybrid SIP Orchestrator v3

12-step pipeline that reads Engine 2 + Engine 3 outputs, fetches Upstox holdings,
scores ETFs with 12 live indicators, manages thematic cycle rotation, and deploys
SIP via a 3-tranche dip system.

## Pipeline (12 steps)
1. Fetch Engine 3 macro email (Gmail IMAP)
2. Fetch Engine 2 signal email (Gmail IMAP)
3. Fetch Upstox holdings — equity + mutual funds
4. Classify holdings into 4 sleeves (Core 60%, International 20%, Thematic 15%, Hedge 5%)
5. Compute sleeve drift → SIP split
6. Check thematic phase rotation (EXIT old / ENTER new cycle ETFs)
7. Run 12-indicator live scoring (110 points per ETF)
8. Assess dip conditions → tranche deployment (Tranche A 50% / B 30% / C 20%)
9. Score instruments (macro × signal × live)
10. Resolve buy dates (3 rules)
11. Compute exit actions (rebalance + urgent + rotation)
12. Write JSON + send email

## 4-Phase Business Cycle (Merrill Lynch India Model)
| Score | Phase | Outperform Sectors |
|---|---|---|
| 0.80-1.00 | STRONG RECOVERY | Financials, Realty, Auto, Infra |
| 0.60-0.80 | MID EXPANSION | IT, Metals, Energy, Infra (Capex) |
| 0.40-0.60 | LATE CYCLE | Energy, Metals, Healthcare, FMCG |
| 0.00-0.40 | CONTRACTION | FMCG, Healthcare, Pharma, IT |

## Schedules
- Engine 2 token refresh: Daily 7:30 AM IST → pushes to this repo
- Weekly sync: Saturday 8:00 AM IST → emails + holdings + tranche dip check
- Monthly run: 1st of month 3:00 PM IST → full 12-step pipeline
- Manual run: on-demand via GitHub Actions UI

## Dashboard
8 tabs: Execution Plan | Sleeve Status | Exit Actions | Macro Signal | Signal Engine | Live Scores | Thematic Rotation | Tranche Status

*Personal investment research. Not financial advice.*
