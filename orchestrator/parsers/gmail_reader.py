"""
orchestrator/parsers/gmail_reader.py
─────────────────────────────────────
Reads emails from Gmail via IMAP.

EMAIL ACCOUNT SETUP:
  Engine 2 and Engine 3 SEND from:  shubhamshivhare27@gmail.com
  Emails are RECEIVED at:           shubhamshivhare554@gmail.com

  Therefore:
    IMAP login    → shubhamshivhare554@gmail.com  (GMAIL_USER env var)
    Search filter → FROM shubhamshivhare27@gmail.com  (sender_filter in config)

  GMAIL_USER = shubhamshivhare554@gmail.com   ← the RECEIVING account
  GMAIL_PASS = App Password for the 554 account (NOT the 27 account)

  Generate the App Password while logged into shubhamshivhare554@gmail.com:
  Google Account → Security → 2-Step Verification → App Passwords

HOW IT WORKS:
  1. Opens IMAP SSL connection to imap.gmail.com:993
  2. Logs in with GMAIL_USER (554 account) + GMAIL_PASS
  3. Searches inbox for emails FROM sender_filter (27 account) within lookback window
  4. Scans newest → oldest for subject keyword match
  5. Extracts HTML body, parses with BeautifulSoup
  6. Returns raw email dict for the parser to process
"""

import email
import imaplib
import logging
import os
from datetime import datetime, timedelta
from email.header import decode_header

from bs4 import BeautifulSoup

log = logging.getLogger("gmail_reader")


# ── Credentials ───────────────────────────────────────────────────────────────

def _creds() -> tuple[str, str]:
    """
    GMAIL_USER = shubhamshivhare554@gmail.com  (the RECEIVING account)
    GMAIL_PASS = App Password for the 554 account
    """
    user = os.environ.get("GMAIL_USER", "").strip()
    pwd  = os.environ.get("GMAIL_PASS", "").strip()
    if not user or not pwd:
        raise EnvironmentError(
            "GMAIL_USER and GMAIL_PASS environment variables must be set.\n"
            "GMAIL_USER must be the RECEIVING email account (shubhamshivhare554@gmail.com)\n"
            "GMAIL_PASS must be the App Password for that account.\n"
            "Generate it at: Google Account (554) → Security → App Passwords."
        )
    return user, pwd


# ── IMAP connection ───────────────────────────────────────────────────────────

def _connect() -> imaplib.IMAP4_SSL:
    user, pwd = _creds()
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(user, pwd)
    mail.select("inbox")
    log.info(f"Gmail IMAP connected as {user}")
    return mail


# ── Subject decoder ───────────────────────────────────────────────────────────

def _decode_subject(msg) -> str:
    raw    = msg.get("Subject", "")
    parts  = decode_header(raw)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += str(part)
    return result


# ── HTML body extractor ───────────────────────────────────────────────────────

def _extract_html(msg) -> str:
    """
    Extract HTML body from email.
    Engine 2 (email_report.py) and Engine 3 (report_sender.py) both send
    MIMEMultipart('alternative') with a text/html part.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/html" and
                    "attachment" not in str(part.get("Content-Disposition", ""))):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


# ── Core search and fetch ─────────────────────────────────────────────────────

def _fetch_email(subject_keywords: list[str], sender: str, lookback_days: int) -> dict | None:
    """
    Search Gmail inbox for most recent email matching ANY of the subject keywords
    from the given sender within the lookback window.

    Tries keywords in order. First match wins.
    This handles Engine 3 emails where the subject is:
      "[Macro] EARLY EXPANSION | Score: 0.766 | Stable | 04-May-2026"
    instead of "India Business Cycle Report" (which appears in the body, not subject).

    IMAP login uses GMAIL_USER (the 554 receiving account).
    Search filter uses sender (the 27 sending account).

    Returns dict: {subject, date, html, text, soup} or None.
    """
    try:
        mail = _connect()

        since = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        _, ids_raw = mail.search(None, f'(FROM "{sender}" SINCE "{since}")')
        ids = ids_raw[0].split()

        if not ids:
            log.warning(f"No emails from '{sender}' in last {lookback_days} days.")
            mail.logout()
            return None

        log.info(f"Found {len(ids)} emails from '{sender}' — scanning for keywords: {subject_keywords}...")

        # Scan newest → oldest, try all keywords
        for msg_id in reversed(ids):
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw_msg = data[0][1]
            msg     = email.message_from_bytes(raw_msg)
            subject = _decode_subject(msg)

            for keyword in subject_keywords:
                if keyword.lower() in subject.lower():
                    html = _extract_html(msg)
                    soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")
                    text = soup.get_text(separator="\n")
                    mail.logout()
                    log.info(f"Matched email (keyword='{keyword}'): '{subject}'")
                    return {
                        "subject": subject,
                        "date":    msg.get("Date", ""),
                        "html":    html,
                        "text":    text,
                        "soup":    soup,
                    }

        # Log all subjects found so user can debug
        log.warning(f"No email matched keywords {subject_keywords}. Subjects found:")
        for msg_id in reversed(ids):
            _, data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            log.warning(f"  - {_decode_subject(msg)}")
        mail.logout()
        return None

    except imaplib.IMAP4.error as e:
        log.error(f"IMAP error: {e}")
        log.error(
            "Check GMAIL_USER and GMAIL_PASS.\n"
            "GMAIL_USER must be: shubhamshivhare554@gmail.com (the RECEIVING account)\n"
            "GMAIL_PASS must be the App Password for the 554 account."
        )
        return None
    except Exception as e:
        log.error(f"Gmail fetch failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_latest_macro_email(config: dict) -> dict | None:
    """
    Fetch the latest Engine 3 India Business Cycle Report email.

    Engine 3 email subject format (from report_sender.py):
      "[Macro] EARLY EXPANSION | Score: 0.766 | Stable | 04-May-2026"

    Note: "India Business Cycle Report" appears in the BODY, not the subject.
    So we search for "[Macro]" as primary keyword, with phase names as fallbacks.
    """
    g = config["gmail"]
    # Build keyword list: primary keyword + alt keywords
    keywords = [g["macro_subject_keyword"]]
    keywords.extend(g.get("macro_subject_keywords_alt", []))
    return _fetch_email(
        subject_keywords=keywords,
        sender=g["sender_filter"],
        lookback_days=g["lookback_days"],
    )


def fetch_latest_signal_email(config: dict) -> dict | None:
    """
    Fetch the latest Engine 2 Nifty 500 Signal Engine email.

    Engine 2 email subject format (from email_report.py):
      "📊 Nifty 500 Signals — 2026-05-01 | 1 BUY | 0 SELL"
    """
    g = config["gmail"]
    return _fetch_email(
        subject_keywords=[g["signal_subject_keyword"]],
        sender=g["sender_filter"],
        lookback_days=g["lookback_days"],
    )
