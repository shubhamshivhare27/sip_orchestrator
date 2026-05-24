"""
orchestrator/parsers/gmail_reader.py
─────────────────────────────────────
Reads emails from Gmail via IMAP using the same GMAIL_USER + GMAIL_PASS
credentials that Engine 2 (email_report.py) and Engine 3 (report_sender.py)
already use for SMTP sending.

HOW IT WORKS:
  1. Opens an IMAP SSL connection to imap.gmail.com:993
  2. Logs in with GMAIL_USER + GMAIL_PASS (Gmail App Password)
  3. Searches inbox for emails FROM the sender within lookback window
  4. Scans from newest → oldest for subject keyword match
  5. Extracts HTML body, parses with BeautifulSoup
  6. Returns raw email dict for the relevant parser to process

Why IMAP and not Gmail API?
  Engine 2 and Engine 3 already use GMAIL_USER + GMAIL_PASS for SMTP.
  Using IMAP with the same credentials means zero new auth setup.
  No OAuth2 client IDs, no refresh tokens, no Google Cloud Console.
  One pair of credentials works for both sending (engines) and reading (orchestrator).

GMAIL_PASS must be a Gmail App Password (16 chars, no spaces).
How to get one: Google Account → Security → 2-Step Verification → App Passwords.
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
    user = os.environ.get("GMAIL_USER", "").strip()
    pwd  = os.environ.get("GMAIL_PASS", "").strip()
    if not user or not pwd:
        raise EnvironmentError(
            "GMAIL_USER and GMAIL_PASS environment variables must be set.\n"
            "These are the same credentials used by Engine 2 and Engine 3 for SMTP.\n"
            "GMAIL_PASS must be a Gmail App Password — not your account password.\n"
            "Create one at: Google Account → Security → 2-Step Verification → App Passwords."
        )
    return user, pwd


# ── IMAP connection ───────────────────────────────────────────────────────────

def _connect() -> imaplib.IMAP4_SSL:
    user, pwd = _creds()
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(user, pwd)
    mail.select("inbox")
    log.info("Gmail IMAP connected.")
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
    Extract HTML body from email message.
    Engine 2 (email_report.py) and Engine 3 (report_sender.py) both send
    MIMEMultipart('alternative') with a text/html part — this extracts it.
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

def _fetch_email(subject_keyword: str, sender: str, lookback_days: int) -> dict | None:
    """
    Search Gmail inbox for most recent email matching keyword + sender.
    Scans newest → oldest within the lookback window.

    Returns dict: {subject, date, html, text, soup} or None if not found.
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

        log.info(f"Found {len(ids)} emails from '{sender}' — scanning for '{subject_keyword}'...")

        for msg_id in reversed(ids):     # newest first
            _, data = mail.fetch(msg_id, "(RFC822)")
            raw_msg = data[0][1]
            msg     = email.message_from_bytes(raw_msg)
            subject = _decode_subject(msg)

            if subject_keyword.lower() in subject.lower():
                html = _extract_html(msg)
                soup = BeautifulSoup(html, "html.parser") if html else BeautifulSoup("", "html.parser")
                text = soup.get_text(separator="\n")
                mail.logout()
                log.info(f"Matched email: '{subject}'")
                return {
                    "subject": subject,
                    "date":    msg.get("Date", ""),
                    "html":    html,
                    "text":    text,
                    "soup":    soup,
                }

        log.warning(f"No email with subject keyword '{subject_keyword}' found.")
        mail.logout()
        return None

    except imaplib.IMAP4.error as e:
        log.error(f"IMAP error: {e}")
        log.error("Check GMAIL_USER and GMAIL_PASS. GMAIL_PASS must be an App Password.")
        return None
    except Exception as e:
        log.error(f"Gmail fetch failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_latest_macro_email(config: dict) -> dict | None:
    """
    Fetch the latest Engine 3 India Business Cycle Report email.
    Engine 3 sends from GMAIL_USER → RECIPIENT_EMAIL every Monday.
    Observed delivery: 11 AM – 1 PM IST.
    Orchestrator monthly run is at 3 PM IST Monday — safe gap.
    """
    g = config["gmail"]
    return _fetch_email(
        subject_keyword=g["macro_subject_keyword"],
        sender=g["sender_filter"],          # ← changed from "sender"
        lookback_days=g["lookback_days"],
    )


def fetch_latest_signal_email(config: dict) -> dict | None:
    """
    Fetch the latest Engine 2 Nifty 500 Signal Engine email.
    Engine 2 sends from GMAIL_USER → RECIPIENT_EMAIL every Friday.
    Observed delivery: 10 PM – 11 PM IST Friday.
    Orchestrator weekly sync is Saturday 8 AM IST — safe gap.
    """
    g = config["gmail"]
    return _fetch_email(
        subject_keyword=g["signal_subject_keyword"],
        sender=g["sender_filter"],          # ← changed from "sender"
        lookback_days=g["lookback_days"],
    )
