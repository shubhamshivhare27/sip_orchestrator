"""scripts/sync_signals.py — Saturday 8 AM IST weekly sync."""
import json, logging, sys
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sync")

from orchestrator.parsers.gmail_reader  import fetch_latest_macro_email, fetch_latest_signal_email
from orchestrator.parsers.macro_parser  import parse_macro_email
from orchestrator.parsers.signal_parser import parse_signal_email
from orchestrator.bridge.upstox_client  import get_portfolio_snapshot, save_snapshot

CONFIG_PATH = ROOT / "config/allocation_config.json"
INPUTS_DIR  = ROOT / "data/inputs"
CACHE_DIR   = ROOT / "data/cache"
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

with open(CONFIG_PATH) as f:
    config = json.load(f)

log.info("Syncing Engine 3 macro email...")
raw = fetch_latest_macro_email(config)
if raw:
    data = parse_macro_email(raw)
    with open(INPUTS_DIR/"macro_signal.json","w") as f: json.dump(data,f,indent=2,default=str)
    log.info(f"  Macro: {data.get('phase')} | {data.get('score')}")

log.info("Syncing Engine 2 signal email...")
raw = fetch_latest_signal_email(config)
if raw:
    data = parse_signal_email(raw)
    with open(INPUTS_DIR/"signal_engine.json","w") as f: json.dump(data,f,indent=2,default=str)
    log.info(f"  Signals: {len(data.get('buy_signals',[]))} BUY | {len(data.get('urgent_alerts',[]))} alerts")

log.info("Syncing Upstox holdings...")
try:
    snap = get_portfolio_snapshot()
    save_snapshot(snap, CACHE_DIR)
    with open(INPUTS_DIR/"holdings.json","w") as f: json.dump(snap,f,indent=2,default=str)
    log.info(f"  Holdings: Rs.{snap.get('total_value',0):,.0f}")
except Exception as e:
    log.warning(f"  Holdings failed: {e}")

log.info("Weekly sync complete.")
