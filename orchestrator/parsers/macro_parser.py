"""
orchestrator/parsers/macro_parser.py
─────────────────────────────────────
Parses the Engine 3 India Business Cycle Report email HTML into a
structured dict. Written against the actual report_sender.py and
sector_mapper.py source code — not guessed.

EMAIL STRUCTURE (from Engine 3's report_sender.py):
  Subject : "[Macro] EARLY EXPANSION | Score: 0.766 | Stable | 04-May-2026"
  Section A: Business Cycle Summary table (phase, score, momentum, confidence)
  Section B: 15-indicator scorecard table
  Section C: Key drivers, risks, watch items
  Section D: Sector stance (⬆ Overweight / ➡ Neutral / ⬇ Underweight)
  Section E: ETF table — Name|Ticker|Cycle Stance|Tag|Price|RSI(14)|4W Mom|MA Cross|Ann Vol

ETF Tag logic (from sector_mapper._tag_etf):
  composite = stance(0.40) + momentum(0.35) + tech(0.25)
  BUY >= 0.65 | WATCHLIST >= 0.40 | AVOID < 0.40

Stance values in ETF table match PHASE_ETF_STANCE dict keys: OW / N / UW
"""

import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

log = logging.getLogger("macro_parser")

VALID_PHASES = [
    "STRONG RECOVERY", "EARLY EXPANSION",
    "MID CYCLE", "LATE CYCLE", "CONTRACTION"
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return " ".join(str(s).strip().split())

def _to_float(s) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s)))
    except (ValueError, TypeError):
        return None

def _parse_date_from_subject(subject: str) -> str:
    """Extract date from '[Macro] EARLY EXPANSION | Score: 0.766 | Stable | 04-May-2026'"""
    m = re.search(r"(\d{2}-\w{3}-\d{4})", subject)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now().strftime("%Y-%m-%d")

def _phase_from_subject(subject: str) -> str | None:
    for phase in VALID_PHASES:
        if phase in subject.upper():
            return phase
    return None

def _score_from_subject(subject: str) -> float | None:
    m = re.search(r"Score:\s*([\d.]+)", subject)
    return float(m.group(1)) if m else None

def _momentum_from_subject(subject: str) -> str | None:
    for word in ["Rising", "Falling", "Stable", "First Run"]:
        if word.lower() in subject.lower():
            return word
    return None


# ── Section A: Business Cycle Summary ────────────────────────────────────────

def _parse_summary(soup: BeautifulSoup, subject: str) -> dict:
    result = {
        "phase":             _phase_from_subject(subject),
        "score":             _score_from_subject(subject),
        "momentum":          _momentum_from_subject(subject),
        "confidence":        None,
        "live_count":        15,
        "estimated_count":   0,
        "manual_count":      0,
        "rebalance_signal":  False,
        "historical_precedent": None,
    }

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [_clean(c.get_text()) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            key = cells[0].lower()
            val = " ".join(cells[1:])

            if "cycle phase" in key:
                for phase in VALID_PHASES:
                    if phase in val.upper():
                        result["phase"] = phase
                        break
            elif "composite score" in key:
                m = re.search(r"([\d.]+)", val)
                if m:
                    result["score"] = float(m.group(1))
            elif "momentum" in key and "score" not in key:
                for word in ["Rising", "Falling", "Stable", "First Run"]:
                    if word.lower() in val.lower():
                        result["momentum"] = word
                        break
            elif "confidence" in key:
                for level in ["High", "Medium", "Low"]:
                    if level.lower() in val.lower():
                        result["confidence"] = level
                        break
                m = re.search(r"Live:\s*(\d+)", val)
                if m:
                    result["live_count"] = int(m.group(1))
                m = re.search(r"Estimated:\s*(\d+)", val)
                if m:
                    result["estimated_count"] = int(m.group(1))
                m = re.search(r"Manual:\s*(\d+)", val)
                if m:
                    result["manual_count"] = int(m.group(1))
            elif "rebalance" in key:
                result["rebalance_signal"] = "yes" in val.lower()
            elif "historical" in key or "precedent" in key:
                result["historical_precedent"] = val

    return result


# ── Section B: 15-indicator scorecard ────────────────────────────────────────

def _parse_indicators(soup: BeautifulSoup) -> list[dict]:
    """
    Parse the 15-indicator scorecard table.
    Engine 3 report has rows: Indicator | Value | Source | Data Date | Status | Notes
    Grouped under: Leading (50%) | Coincident (33%) | Lagging (17%)
    """
    indicators  = []
    weight_group = "Leading"

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue
        # Check if this looks like an indicators table
        header_text = " ".join(
            c.get_text() for c in rows[0].find_all(["td","th"])
        ).lower()
        if "indicator" not in header_text and "value" not in header_text:
            continue

        for row in rows[1:]:
            cells    = [_clean(c.get_text()) for c in row.find_all(["td","th"])]
            if not cells:
                continue
            row_text = " ".join(cells).lower()

            # Detect group header rows
            if "leading indicators" in row_text:
                weight_group = "Leading"; continue
            if "coincident indicators" in row_text:
                weight_group = "Coincident"; continue
            if "lagging indicators" in row_text:
                weight_group = "Lagging"; continue
            if cells[0].lower() in ["indicator","name","metric"]:
                continue
            if len(cells[0]) < 4:
                continue

            value  = cells[1] if len(cells) > 1 else "—"
            full   = " ".join(cells)

            # Signal from emoji (Engine 3 uses 🟢 🟡 🔴)
            if   "🟢" in full or "bullish" in full.lower(): signal = "Bullish"
            elif "🔴" in full or "bearish" in full.lower(): signal = "Bearish"
            else:                                            signal = "Neutral"

            # Fetch status
            if "❌" in full or "manual" in full.lower():   status = "MANUAL_REQUIRED"
            elif "⚠" in full or "estimated" in full.lower(): status = "ESTIMATED"
            else:                                              status = "OK"

            indicators.append({
                "name":         cells[0],
                "value":        value,
                "signal":       signal,
                "group":        weight_group,
                "fetch_status": status,
                "notes":        cells[5] if len(cells) > 5 else "",
            })

    return indicators


# ── Section C: Macro interpretation ──────────────────────────────────────────

def _parse_interpretation(soup: BeautifulSoup) -> dict:
    result = {"key_drivers": [], "risks": [], "watch_items": []}
    text   = soup.get_text(separator="\n")
    lines  = [_clean(l) for l in text.split("\n") if _clean(l)]

    for line in lines:
        ll = line.lower()
        if "key driver" in ll:
            clean = re.sub(r"^.*?key driver\s*\d*\s*[:\-]?\s*", "", line, flags=re.IGNORECASE).strip()
            if clean and len(clean) > 10:
                result["key_drivers"].append(clean)
        elif "⚠" in line or (ll.startswith("risk") and len(line) > 15):
            clean = re.sub(r"^.*?risk\s*\d*\s*[:\-]?\s*", "", line, flags=re.IGNORECASE).strip()
            if clean and len(clean) > 10:
                result["risks"].append(clean)
        elif "👁" in line or (ll.startswith("watch") and len(line) > 15):
            clean = re.sub(r"^.*?watch\s*\d*\s*[:\-]?\s*", "", line, flags=re.IGNORECASE).strip()
            if clean and len(clean) > 10:
                result["watch_items"].append(clean)

    return result


# ── Section D: Sector stance ──────────────────────────────────────────────────

def _parse_sector_stance(soup: BeautifulSoup) -> dict[str, str]:
    """
    Parse the sector stance table from Engine 3's sector_mapper.py output.
    The email shows: ⬆ Overweight | ➡ Neutral | ⬇ Underweight with sector lists.
    """
    stance_map     = {}
    current_stance = None
    text           = soup.get_text(separator="\n")
    lines          = [_clean(l) for l in text.split("\n") if _clean(l)]

    for line in lines:
        ll = line.lower()

        if "⬆" in line or ("overweight" in ll and any(s in ll for s in ["nifty","bank","auto","it","energy","metal","infra","fmcg","pharma","realty","psu"])):
            current_stance = "Overweight"
            sectors_part = re.sub(r".*?(⬆|overweight)\s*", "", line, flags=re.IGNORECASE).strip()
        elif "➡" in line or ("neutral" in ll and any(s in ll for s in ["nifty","bank","auto","it","energy","metal","infra","fmcg","pharma"])):
            current_stance = "Neutral"
            sectors_part = re.sub(r".*?(➡|neutral)\s*", "", line, flags=re.IGNORECASE).strip()
        elif "⬇" in line or ("underweight" in ll and any(s in ll for s in ["nifty","bank","auto","it","energy","metal","infra","fmcg","pharma"])):
            current_stance = "Underweight"
            sectors_part = re.sub(r".*?(⬇|underweight)\s*", "", line, flags=re.IGNORECASE).strip()
        else:
            sectors_part = line

        if current_stance and sectors_part:
            for sector in re.split(r",\s*", sectors_part):
                sector = _clean(sector)
                if sector and len(sector) > 3 and sector.lower() not in ["overweight","neutral","underweight","stance","sectors"]:
                    stance_map[sector] = current_stance

    return stance_map


# ── Section E: ETF recommendations ───────────────────────────────────────────

def _parse_etf_tags(soup: BeautifulSoup) -> list[dict]:
    """
    Parse the ETF table from Engine 3's sector_mapper.print_sector_table output.
    Columns: Name | Ticker | Cycle Stance | Tag | Price | RSI(14) | 4W Mom | MA Cross | Ann Vol

    Stance values come from PHASE_ETF_STANCE: OW / N / UW
    Tag values from _tag_etf: BUY / WATCHLIST / AVOID
    """
    etfs = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_cells = [_clean(c.get_text()).lower() for c in rows[0].find_all(["td","th"])]

        # Must have ticker AND (tag or stance) columns
        has_ticker = any("ticker" in h for h in header_cells)
        has_tag    = any("tag" in h for h in header_cells)
        if not (has_ticker and has_tag):
            continue

        for row in rows[1:]:
            cells    = [_clean(c.get_text()) for c in row.find_all(["td","th"])]
            if len(cells) < 3:
                continue

            row_text = " ".join(cells)
            m        = re.search(r"([A-Z0-9]+\.NS)", row_text)
            if not m:
                continue
            ticker = m.group(1)

            def _get(frags):
                for frag in frags:
                    for i, h in enumerate(header_cells):
                        if frag in h and i < len(cells):
                            return cells[i]
                return None

            tag_raw    = _get(["tag"])           or ""
            stance_raw = _get(["stance","cycle"]) or ""
            price_raw  = _get(["price"])          or ""
            rsi_raw    = _get(["rsi"])             or ""
            mom_raw    = _get(["mom","4w"])        or ""
            ma_raw     = _get(["ma","cross"])      or ""
            vol_raw    = _get(["vol","ann"])        or ""

            tag = (
                "BUY"       if "buy"       in tag_raw.lower() else
                "AVOID"     if "avoid"     in tag_raw.lower() else
                "WATCHLIST" if "watch"     in tag_raw.lower() else
                "UNKNOWN"
            )
            # Map full stance text back to OW/N/UW for scorer
            stance = (
                "OW" if "over"  in stance_raw.lower() else
                "UW" if "under" in stance_raw.lower() else
                "N"
            )

            etfs.append({
                "ticker":    ticker,
                "tag":       tag,
                "stance":    stance,
                "price":     _to_float(price_raw.replace("₹","").replace(",","")),
                "rsi":       _to_float(rsi_raw),
                "mom_4w":    _to_float(mom_raw.replace("+","").replace("%","")),
                "ma_signal": _clean(ma_raw) or None,
                "ann_vol":   _to_float(vol_raw.replace("%","")),
            })

    return etfs


# ── Master parser ─────────────────────────────────────────────────────────────

def parse_macro_email(email_data: dict) -> dict:
    """
    Parse Engine 3 email into structured dict.
    Input:  raw email dict from gmail_reader.fetch_latest_macro_email()
    Output: structured macro signal dict
    """
    subject = email_data.get("subject", "")
    soup    = email_data.get("soup") or BeautifulSoup(email_data.get("html",""), "html.parser")

    log.info(f"Parsing Engine 3 email: {subject[:80]}")

    summary        = _parse_summary(soup, subject)
    indicators     = _parse_indicators(soup)
    interpretation = _parse_interpretation(soup)
    sector_stance  = _parse_sector_stance(soup)
    etf_tags       = _parse_etf_tags(soup)

    result = {
        **summary,
        "report_date":          _parse_date_from_subject(subject),
        "indicators":           indicators,
        "key_drivers":          interpretation["key_drivers"],
        "risks":                interpretation["risks"],
        "watch_items":          interpretation["watch_items"],
        "sector_stance":        sector_stance,
        "etf_tags":             etf_tags,
        "source_email_subject": subject,
        "parsed_at":            datetime.now().isoformat(),
    }

    log.info(
        f"Engine 3 parsed ✓  phase={result['phase']}  "
        f"score={result['score']}  etfs={len(etf_tags)}  "
        f"sectors={len(sector_stance)}  indicators={len(indicators)}"
    )
    return result
