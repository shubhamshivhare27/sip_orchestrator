# Hybrid SIP Orchestrator

Reads outputs from Engine 2 and Engine 3 via Gmail IMAP.
Fetches live holdings from Upstox. Produces a monthly execution plan.

## How it works

Every 1st of the month at 3:00 PM IST, GitHub Actions:
1. Reads Engine 3 macro email → parses phase, score, ETF tags
2. Reads Engine 2 signal email → parses BUY/SELL signals, urgent alerts
3. Fetches Upstox holdings via API (using Engine 2's daily token)
4. Classifies holdings into 4 sleeves
5. Computes sleeve drift vs targets → splits SIP across sleeves
6. Scores eligible ETFs (macro × signal × RSI × momentum)
7. Assigns buy dates using 3 rules
8. Identifies exit actions → writes execution_plan.json → dashboard updates

## Email timing (observed delivery, not workflow trigger)
- Engine 3 (Macro): Mondays 11 AM – 1 PM IST
- Engine 2 (Signals): Fridays 10 PM – 11 PM IST

## Workflow schedule
- weekly_signal_sync.yml: Saturday 8:00 AM IST (catches Friday night email)
- monthly_sip_run.yml:    1st of month, 3:00 PM IST (Engine 3 email landed by then)
- manual_run.yml:         On-demand via GitHub Actions UI

## SIP amount
Enter it once in the Streamlit dashboard → saved to data/inputs/sip_config.json
→ persists until you change it → GitHub Actions reads the same file automatically.

## Secrets required
| Secret | Source |
|--------|--------|
| GMAIL_USER | Same account used by Engine 2 + Engine 3 |
| GMAIL_PASS | Gmail App Password (not account password) |
| UPSTOX_TOKEN | Pushed daily by Engine 2's token refresh |
| UPSTOX_TOKEN_EXPIRY | Pushed daily by Engine 2's token refresh |

## Setup
See Implementation Guide for full step-by-step.

Quick start (local):
```bash
pip install -r requirements.txt
export GMAIL_USER="your@gmail.com"
export GMAIL_PASS="app-password"
export UPSTOX_TOKEN="token"
python orchestrator/main.py --sip 50000
# or dry run (cached data):
python orchestrator/main.py --dry-run
```

*Personal investment research. Not financial advice.*
