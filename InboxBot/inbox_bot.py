#!/usr/bin/env python3
"""
Outlook Inbox Bot (local, autonomous) — Anthropic Claude + ConnectWise triage (FULL REWRITE)

What it does (your rules, fixed):
- Scans Inbox from last N days (default 20), prioritizing UNREAD first
- NON-NOISE definition (only these get drafts):
    1) Internal sender: *@jeffcofibres.com
    2) Ticket status=New in subject (always draft)
    3) Known customer: someone you've previously emailed (from Sent Items recipients)
       BUT: known-customer is filtered by hard-noise checks (noreply/alerts/keywords/etc.)
- HARD-NOISE always wins (prevents drafting to alerts even if customer cache is polluted):
    - message classes like meeting/notifications
    - noreply-ish sender local parts
    - automated sender regex / automated subject keywords
    - common alert/report/order/shipping/invoice/newsletter keywords
    - optional blocked domains list
- Noise mail gets routed to: Automated Alerts (if automated-like) else Filed
- Non-noise mail:
    - drafts an intelligent reply using Anthropic Claude into PERSONAL Drafts (does not send)
    - ConnectWise ticket emails are triaged (classified + personalized response)
    - moves the original to To Reply (if unread) or Waiting (if read)

Logs:
- logs/actions.jsonl
- logs/llm_calls.jsonl
Cache:
- cache/customers.json  (last-seen timestamps for recipients you emailed)
- cache/last_customer_refresh.json

Requirements:
- Windows + Outlook Desktop configured
- pip install pywin32 requests anthropic python-dotenv
- ANTHROPIC_API_KEY environment variable set
"""

import calendar
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pythoncom  # type: ignore
import win32com.client  # type: ignore

try:
    import requests
except Exception:
    requests = None
try:
    import msvcrt  # Windows-only file locking
except Exception:
    msvcrt = None
try:
    import pyodbc  # type: ignore
except Exception:
    pyodbc = None
try:
    import anthropic as _anthropic_module  # type: ignore
except ImportError:
    _anthropic_module = None
try:
    from cw_triage import triage_ticket as cw_triage_ticket
except ImportError:
    cw_triage_ticket = None
try:
    from dotenv import load_dotenv  # type: ignore
    # Load .env from MorningTaskBot project for DB credentials
    _env_path = os.path.join(os.path.dirname(__file__), "..", "MorningTaskBot", ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
    else:
        # Try sibling directory
        _env_path2 = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(_env_path2):
            load_dotenv(_env_path2)
except Exception:
    pass


# ----------------------------
# Outlook constants
# ----------------------------
OL_FOLDER_INBOX = 6
OL_FOLDER_DRAFTS = 16
OL_FOLDER_SENTMAIL = 5
OL_MAILITEM_CLASS = 43

# Exchange sender SMTP
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_TRANSPORT_MESSAGE_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001E"


# ----------------------------
# Paths
# ----------------------------
BASE_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(BASE_DIR, "logs")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
LOCK_FILE = os.path.join(BASE_DIR, ".inbox_bot.lock")

ACTIONS_LOG = os.path.join(LOG_DIR, "actions.jsonl")
LLM_LOG = os.path.join(LOG_DIR, "llm_calls.jsonl")
CUSTOMERS_CACHE = os.path.join(CACHE_DIR, "customers.json")
LAST_REFRESH_FILE = os.path.join(CACHE_DIR, "last_customer_refresh.json")


# ----------------------------
# Config
# ----------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "mailbox_name": "",  # "" = default mailbox
    "inbox_folder": "Inbox",
    "loop_seconds": 60,
    "max_items_per_pass": 100,
    "lookback_days": 20,
    "unread_only": True,  # Only process unread messages (recommended)
    "create_folders_if_missing": True,
    "debug": True,
    "reload_config_each_pass": True,
    "single_instance_lock": True,
    "error_backoff_seconds": 15,
    "max_backoff_seconds": 300,

    "internal_domain": "jeffcofibres.com",

    "folders": {
        "automated_alerts": "Automated Alerts",
        "filed": "Filed",
        "to_reply": "To Reply",
        "waiting": "Waiting"
    },

    "rules": {
        "ticket_new_subject_regex": r"Ticket#\d+/status=New",
        "ticket_resolved_subject_regex": r"Ticket (resolved|closed)|status=Resolved|status=Closed",

        # Automated / no-reply routing to Alerts
        "automated_sender_regex": r"(no-?reply|donotreply|do-?not-?reply)",
        "automated_subject_keywords": [
            "No Reply", "Auto-Reply", "Do Not Reply", "noreply",
            "system notification", "notification", "alert",
        ],

        # Hard-noise subject keywords (prevents drafting)
        "hard_noise_subject_keywords": [
            "open_order", "open order",
            "shipment", "shipping", "delivery",
            "proof of delivery", "advance shipping notice", "asn",
            "transaction status", "status update",
            "invoice", "invoice notification", "statement", "remittance",
            "newsletter", "unsubscribe", "marketing", "promo", "promotion",
            "report", "standard reports", "qc2", "priorities",
        ],

        # Hard-noise body keywords (prevents drafting)
        "hard_noise_body_keywords": [
            "this is an automated message",
            "this is an automatically generated message",
            "do not reply",
            "do-not-reply",
            "please do not reply",
            "no reply",
        ],

        # Headers that strongly indicate non-human / bulk / system mail
        "non_human_header_keywords": [
            "auto-submitted: auto-replied",
            "auto-submitted: auto-generated",
            "x-autoreply",
            "x-autorespond",
            "x-auto-response-suppress",
            "precedence: bulk",
            "precedence: list",
            "list-id:",
            "list-unsubscribe:",
            "x-list-unsubscribe:",
            "x-mailchimp",
            "x-campaign",
        ],

        # Sender local-part markers that are hard-noise (prevents drafting)
        "hard_noise_sender_locals": [
            "no-reply", "noreply", "donotreply", "do-not-reply",
            "mailer-daemon", "postmaster", "bounce",
            "alerts", "notification", "notify", "notifications",
        ],

        # Optional domains that should NEVER be treated as customers (prevents drafting)
        "blocked_domains": [
            "saatva.com",
            "lenovo.com",
            "squarespace.com",
            "divenewsletter.com",
        ],
        # Specific senders that should NEVER get a draft (case-insensitive exact match)
        "blocked_senders": [],
        # Subjects that should NEVER get a draft (case-insensitive exact match)
        "blocked_subjects": [],

        # Sorting behavior for non-noise
        # Note: With unread_only=True (default), only unread messages are processed
        "draft_for_unread_non_noise_to_to_reply": True,   # unread non-noise -> To Reply
        "draft_for_read_non_noise_to_waiting": True,      # read non-noise -> Waiting (legacy, unused when unread_only=True)

        "max_body_chars_to_model": 8000,

        # Anthropic Claude API
        "anthropic_model": "claude-sonnet-4-20250514",
        "anthropic_max_tokens": 1024,

        # ConnectWise ticket triage
        "cw_triage_enabled": True,

        # Customer cache refresh from Sent Items
        "customer_cache_lookback_days": 365,
        "customer_cache_refresh_minutes": 30,
        "customer_cache_max_sent_scan": 3000,

        # Bot delegation — inbox bot delegates to SQL bot for data lookups
        "delegation_enabled": True,
        "delegation_intent_keywords": {
            "lead_time": [
                "lead time", "leadtime", "how long", "when can",
                "delivery date", "delivery time", "ship date",
                "eta", "estimated time", "turnaround",
                "how soon", "when will", "time frame", "timeframe",
            ],
            "order_status": [
                "order status", "order update", "where is my order",
                "tracking", "shipment status", "order number",
                "po status", "purchase order status",
            ],
            "inventory": [
                "in stock", "inventory", "available", "availability",
                "do you have", "stock level", "on hand",
            ],
        },
        # Database connection (reads from environment variables)
        "db_host": "",
        "db_user": "",
        "db_password": "",
        "db_name": "",
    }
}


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(dst)
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Wrote default config to {path}. Edit it if needed.")
        return DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return DEFAULT_CONFIG
    return _deep_merge(DEFAULT_CONFIG, data)


# ----------------------------
# Logging
# ----------------------------
def now_local() -> datetime:
    return datetime.now()


def log_jsonl(path: str, obj: Dict[str, Any]) -> None:
    try:
        rec = dict(obj)
        rec["ts"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_exception(event: str, exc: Exception) -> None:
    log_jsonl(ACTIONS_LOG, {"event": event, "error": str(exc)})


# ----------------------------
# Time helpers
# ----------------------------
def local_tzinfo():
    return datetime.now().astimezone().tzinfo


def dt_to_ts(d) -> float:
    """COM-safe datetime -> unix timestamp."""
    if d is None:
        return 0.0
    try:
        tz = getattr(d, "tzinfo", None)
        if tz is None:
            d2 = d.replace(tzinfo=local_tzinfo())
        else:
            d2 = d
        return float(d2.timestamp())
    except Exception:
        try:
            tz = getattr(d, "tzinfo", None)
            if tz is not None:
                d_utc_naive = d.astimezone(timezone.utc).replace(tzinfo=None)
                return float(calendar.timegm(d_utc_naive.timetuple()) + d_utc_naive.microsecond / 1e6)
            d_loc = d.replace(tzinfo=local_tzinfo()).astimezone(timezone.utc).replace(tzinfo=None)
            return float(calendar.timegm(d_loc.timetuple()) + d_loc.microsecond / 1e6)
        except Exception:
            return 0.0


def item_time_ts(item) -> float:
    for attr in ("ReceivedTime", "CreationTime", "SentOn"):
        try:
            ts = dt_to_ts(getattr(item, attr, None))
            if ts > 0:
                return ts
        except Exception:
            continue
    return 0.0


# ----------------------------
# String helpers
# ----------------------------
def safe_lower(s: str) -> str:
    return (s or "").strip().lower()


def domain_of(email: str) -> str:
    e = safe_lower(email)
    return e.split("@", 1)[1] if "@" in e else ""


def get_subject(item) -> str:
    try:
        return (item.Subject or "").strip()
    except Exception:
        return ""


# ----------------------------
# Outlook mailbox/folder helpers
# ----------------------------
def find_mailbox_root(ns, mailbox_name: str):
    if not mailbox_name:
        inbox = ns.GetDefaultFolder(OL_FOLDER_INBOX)
        return inbox.Parent

    target = safe_lower(mailbox_name)
    for i in range(1, ns.Folders.Count + 1):
        f = ns.Folders.Item(i)
        if safe_lower(f.Name) == target:
            return f
    try:
        return ns.Folders.Item(mailbox_name)
    except Exception:
        return None


def get_or_create_subfolder(parent, name: str, create: bool):
    for i in range(1, parent.Folders.Count + 1):
        f = parent.Folders.Item(i)
        if f.Name == name:
            return f
    if create:
        try:
            return parent.Folders.Add(name)
        except Exception:
            return None
    return None


# ----------------------------
# Sender extraction (Exchange-safe)
# ----------------------------
def get_sender_smtp(item) -> str:
    try:
        sender_type = safe_lower(getattr(item, "SenderEmailType", ""))
        sender_addr = safe_lower(getattr(item, "SenderEmailAddress", ""))
        if sender_type == "ex":
            # Method 1: PR_SMTP_ADDRESS on the item itself
            try:
                pa = item.PropertyAccessor
                smtp = safe_lower(pa.GetProperty(PR_SMTP_ADDRESS))
                if smtp and "@" in smtp:
                    return smtp
            except Exception:
                pass
            # Method 2: Sender.GetExchangeUser().PrimarySmtpAddress
            try:
                sender = item.Sender
                if sender is not None:
                    exu = sender.GetExchangeUser()
                    if exu is not None:
                        smtp = safe_lower(getattr(exu, "PrimarySmtpAddress", ""))
                        if smtp and "@" in smtp:
                            return smtp
            except Exception:
                pass
            # Method 3: Sender.AddressEntry properties
            try:
                sender = item.Sender
                if sender is not None:
                    try:
                        pa2 = sender.PropertyAccessor
                        smtp = safe_lower(pa2.GetProperty(PR_SMTP_ADDRESS))
                        if smtp and "@" in smtp:
                            return smtp
                    except Exception:
                        pass
                    addr = safe_lower(getattr(sender, "Address", ""))
                    if addr and "@" in addr:
                        return addr
            except Exception:
                pass
        return sender_addr
    except Exception:
        return ""


# ----------------------------
# Body extraction
# ----------------------------
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_P_END_RE = re.compile(r"</p\s*>", re.IGNORECASE)


def extract_plain_text(item, max_chars: int) -> str:
    try:
        body = (item.Body or "").strip()
        if body:
            return body[:max_chars]
    except Exception:
        pass

    try:
        html = item.HTMLBody or ""
        if not html.strip():
            return ""
        text = _BR_RE.sub("\n", html)
        text = _P_END_RE.sub("\n\n", text)
        text = _HTML_TAG_RE.sub("", text)
        return text.strip()[:max_chars]
    except Exception:
        return ""


# ----------------------------
# Customer cache (previous correspondence)
# cache format: { "email@domain.com": last_seen_ts_float }
# ----------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def load_customers_cache() -> Dict[str, float]:
    if not os.path.exists(CUSTOMERS_CACHE):
        return {}
    try:
        with open(CUSTOMERS_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        out: Dict[str, float] = {}
        for k, v in data.items():
            out[safe_lower(k)] = float(v)
        return out
    except Exception:
        return {}


def save_customers_cache(cache: Dict[str, float]) -> None:
    try:
        with open(CUSTOMERS_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def load_last_refresh_ts() -> float:
    try:
        if not os.path.exists(LAST_REFRESH_FILE):
            return 0.0
        with open(LAST_REFRESH_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return float(d.get("ts", 0.0))
    except Exception:
        return 0.0


def save_last_refresh_ts(ts: float) -> None:
    try:
        with open(LAST_REFRESH_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": float(ts)}, f)
    except Exception:
        pass


def extract_recipient_emails_from_sent_item(sent_item) -> List[str]:
    emails: List[str] = []

    # Try Recipients collection
    try:
        recips = getattr(sent_item, "Recipients", None)
        if recips is not None:
            for i in range(1, recips.Count + 1):
                try:
                    r = recips.Item(i)
                    addr = ""
                    try:
                        ae = r.AddressEntry
                        if ae is not None:
                            try:
                                pa = ae.PropertyAccessor
                                addr = pa.GetProperty(PR_SMTP_ADDRESS)
                            except Exception:
                                pass
                            if not addr:
                                try:
                                    addr = ae.Address
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    if not addr:
                        try:
                            addr = r.Address
                        except Exception:
                            addr = ""

                    for m in _EMAIL_RE.findall(addr or ""):
                        emails.append(safe_lower(m))
                except Exception:
                    continue
    except Exception:
        pass

    # Fallback: parse To/CC fields
    for field in ("To", "CC"):
        try:
            s = getattr(sent_item, field, "") or ""
            for m in _EMAIL_RE.findall(s):
                emails.append(safe_lower(m))
        except Exception:
            continue

    return sorted(set(e for e in emails if "@" in e))


def refresh_customers_from_sent(ns, mailbox_root, cfg_rules, debug: bool) -> None:
    cache = load_customers_cache()
    lookback_days = int(cfg_rules.get("customer_cache_lookback_days", 365))
    max_scan = int(cfg_rules.get("customer_cache_max_sent_scan", 3000))
    cutoff_ts = dt_to_ts(now_local() - timedelta(days=lookback_days))

    try:
        sent_folder = mailbox_root.Folders.Item("Sent Items")
    except Exception:
        sent_folder = ns.GetDefaultFolder(OL_FOLDER_SENTMAIL)

    items = sent_folder.Items
    try:
        items.Sort("[SentOn]", True)
    except Exception:
        pass

    n = min(items.Count, max_scan)
    updated = 0

    for i in range(1, n + 1):
        try:
            it = items.Item(i)
            if getattr(it, "Class", None) != OL_MAILITEM_CLASS:
                continue
            ts = dt_to_ts(getattr(it, "SentOn", None))
            if ts > 0 and ts < cutoff_ts:
                continue

            recips = extract_recipient_emails_from_sent_item(it)
            for e in recips:
                if ts > cache.get(e, 0.0):
                    cache[e] = ts
                    updated += 1
        except Exception:
            continue

    if updated:
        save_customers_cache(cache)
        log_jsonl(ACTIONS_LOG, {"event": "customer_cache_refresh", "updated": updated, "cache_size": len(cache)})
        if debug:
            print(f"DEBUG: customer cache updated={updated} size={len(cache)}")


# ----------------------------
# HARD NOISE / NON-NOISE logic
# ----------------------------
def is_messageclass_noise(item) -> bool:
    try:
        mc = safe_lower(getattr(item, "MessageClass", ""))
    except Exception:
        return False
    return mc.startswith("ipm.schedule") or mc.startswith("ipm.note.rules") or mc.startswith("report.")


def looks_like_auto_address(email: str, hard_locals: List[str]) -> bool:
    s = safe_lower(email)
    local = s.split("@", 1)[0] if "@" in s else s
    return any(marker in local for marker in hard_locals)


def subject_has_keywords(subject: str, keywords: List[str]) -> bool:
    s = (subject or "").lower()
    return any((k or "").lower() in s for k in (keywords or []))

def get_transport_headers(item) -> str:
    try:
        pa = getattr(item, "PropertyAccessor", None)
        if pa is None:
            return ""
        headers = pa.GetProperty(PR_TRANSPORT_MESSAGE_HEADERS)
        return headers or ""
    except Exception:
        return ""


def headers_indicate_non_human(item, rules: Dict[str, Any]) -> bool:
    headers = get_transport_headers(item)
    if not headers:
        return False
    h = headers.lower()
    for kw in rules.get("non_human_header_keywords", []):
        if (kw or "").lower() in h:
            return True
    return False


def body_has_keywords(item, rules: Dict[str, Any], max_chars: int = 4000) -> bool:
    keywords = rules.get("hard_noise_body_keywords", [])
    if not keywords:
        return False
    body = extract_plain_text(item, max_chars)
    s = (body or "").lower()
    return any((k or "").lower() in s for k in keywords)


def is_automated_like(sender_email: str, subject: str, rules: Dict[str, Any]) -> bool:
    try:
        sender_re = re.compile(rules.get("automated_sender_regex", ""), re.IGNORECASE)
    except Exception:
        sender_re = re.compile(r"$^")
    if sender_re.search(sender_email or ""):
        return True
    return subject_has_keywords(subject, rules.get("automated_subject_keywords", []))


def hard_noise(sender: str, subject: str, item, rules: Dict[str, Any]) -> bool:
    if is_messageclass_noise(item):
        return True
    if headers_indicate_non_human(item, rules):
        return True
    if looks_like_auto_address(sender, rules.get("hard_noise_sender_locals", [])):
        return True
    if is_automated_like(sender, subject, rules):
        return True
    if subject_has_keywords(subject, rules.get("hard_noise_subject_keywords", [])):
        return True
    if body_has_keywords(item, rules):
        return True
    # optional: if "unsubscribe" anywhere
    if "unsubscribe" in (subject or "").lower():
        return True
    # optional: blocked domains
    bd = set(safe_lower(d) for d in rules.get("blocked_domains", []))
    if domain_of(sender) in bd:
        return True
    return False


def is_known_customer(sender_email: str, cache: Dict[str, float], rules: Dict[str, Any]) -> bool:
    s = safe_lower(sender_email)
    if s not in cache:
        return False
    # If sender looks auto or domain blocked, do NOT treat as customer
    if looks_like_auto_address(s, rules.get("hard_noise_sender_locals", [])):
        return False
    bd = set(safe_lower(d) for d in rules.get("blocked_domains", []))
    if domain_of(s) in bd:
        return False
    return True


def is_non_noise(
    sender_email: str,
    subject: str,
    internal_domain: str,
    customer_cache: Dict[str, float],
    ticket_re: re.Pattern,
    rules: Dict[str, Any],
    item,
) -> bool:
    s = safe_lower(sender_email)
    internal = s.endswith("@" + internal_domain)
    ticket_new = bool(ticket_re.search(subject or ""))
    blocked_senders = set(safe_lower(x) for x in rules.get("blocked_senders", []))
    blocked_subjects = set((x or "").lower() for x in rules.get("blocked_subjects", []))
    if s in blocked_senders:
        return False
    if (subject or "").lower() in blocked_subjects:
        return False

    # Internal no-reply/automated senders should still be treated as noise
    if internal and looks_like_auto_address(s, rules.get("hard_noise_sender_locals", [])):
        return False

    # Hard-noise wins unless internal or ticket-new
    if hard_noise(sender_email, subject, item, rules) and not internal and not ticket_new:
        return False

    if internal:
        return True
    if ticket_new:
        return True
    if is_known_customer(sender_email, customer_cache, rules):
        return True
    return False


# ----------------------------
# Draft creation (HTML-safe)
# ----------------------------
def _plain_to_html(s: str) -> str:
    s = (s or "")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
    return s


def get_conversation_context(item, max_items: int = 3) -> str:
    """Extract recent conversation history from the thread."""
    try:
        conv = item.GetConversation()
        if conv is None:
            return ""

        table = conv.GetTable()
        table.Sort("[ReceivedTime]", True)  # Most recent first

        context_parts = []
        count = 0

        while not table.EndOfTable and count < max_items:
            try:
                row = table.GetNextRow()
                conv_item = row.GetValues("EntryID")
                if not conv_item:
                    continue

                # Get the actual item
                try:
                    msg = item.Application.Session.GetItemFromID(conv_item)
                    if msg.Class != OL_MAILITEM_CLASS:
                        continue

                    sender = get_sender_smtp(msg)
                    subj = get_subject(msg)
                    body = extract_plain_text(msg, 1000)

                    if body:
                        context_parts.append(f"--- Previous from {sender} ---\n{body[:500]}\n")
                        count += 1
                except Exception:
                    continue
            except Exception:
                break

        if context_parts:
            return "CONVERSATION HISTORY:\n" + "\n".join(reversed(context_parts)) + "\n---\n"
        return ""
    except Exception:
        return ""


def make_reply_draft(item, draft_text: str, drafts_folder, debug: bool = False) -> Tuple[bool, str]:
    """Create a reply draft in the Drafts folder. Returns (success, error_message)."""
    draft_text = (draft_text or "").strip()
    if not draft_text:
        if debug:
            print("DEBUG: draft_text empty; skipping draft.")
        return False, "Empty draft text"

    prefix_plain = draft_text.rstrip() + "\r\n\r\n"
    prefix_html = f"<p>{_plain_to_html(draft_text)}</p><br>"

    try:
        reply = item.Reply()

        try:
            existing_html = getattr(reply, "HTMLBody", "") or ""
            if existing_html:
                reply.HTMLBody = prefix_html + existing_html
            else:
                existing_body = getattr(reply, "Body", "") or ""
                reply.Body = prefix_plain + existing_body
        except Exception:
            existing_body = getattr(reply, "Body", "") or ""
            reply.Body = prefix_plain + existing_body

        # Save first
        reply.Save()

        # Verify the draft was created
        try:
            draft_subject = getattr(reply, "Subject", "")
            draft_entry_id = getattr(reply, "EntryID", "")
            if not draft_entry_id:
                return False, "No EntryID after save"
        except Exception as e:
            return False, f"Could not verify draft: {e}"

        # Ensure in correct Drafts folder
        try:
            current_folder_entry = getattr(reply.Parent, "EntryID", None)
            target_folder_entry = getattr(drafts_folder, "EntryID", None)
            same_folder = current_folder_entry == target_folder_entry
        except Exception:
            same_folder = False

        if not same_folder:
            try:
                reply = reply.Move(drafts_folder)
                if debug:
                    print(f"DEBUG: draft moved to '{getattr(drafts_folder, 'Name', 'Drafts')}'")
            except Exception as e:
                error_msg = f"Draft created but move failed: {e}"
                if debug:
                    print(f"DEBUG: {error_msg}")
                # Draft still exists, just in wrong folder - consider this partial success
                log_jsonl(ACTIONS_LOG, {
                    "event": "draft_move_failed",
                    "subject": draft_subject,
                    "error": str(e)
                })
                return True, error_msg

        # Final verification
        try:
            parent_name = getattr(reply.Parent, "Name", "")
            if debug:
                print(f"DEBUG: draft saved in '{parent_name}' subj='{draft_subject}'")

            # Verify it's really in Drafts
            if "draft" not in parent_name.lower():
                return True, f"Draft in unexpected folder: {parent_name}"

        except Exception as e:
            if debug:
                print(f"DEBUG: verification warning: {e}")

        return True, ""

    except Exception as e:
        error_msg = f"make_reply_draft failed: {e}"
        if debug:
            print(f"DEBUG: {error_msg}")
        return False, error_msg


def acquire_single_instance_lock(enabled: bool, debug: bool) -> Optional[object]:
    if not enabled or msvcrt is None:
        return None
    try:
        f = open(LOCK_FILE, "a+")
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f
    except Exception as e:
        if debug:
            print(f"DEBUG: could not acquire lock: {e}")
        return None


def connect_outlook(debug: bool):
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
        return outlook, ns
    except Exception as e:
        if debug:
            print(f"DEBUG: Outlook connect failed: {e}")
        log_exception("outlook_connect_error", e)
        return None, None


# ----------------------------
# Anthropic Claude LLM
# ----------------------------
def anthropic_chat(
    system: str,
    messages: List[Dict[str, str]],
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 1024,
) -> str:
    """Call Anthropic Claude API. Returns the text response."""
    if _anthropic_module is None:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")

    client = _anthropic_module.Anthropic(api_key=api_key)

    # Anthropic API uses a separate system parameter; filter system messages out
    api_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            continue
        api_messages.append({"role": role, "content": msg["content"]})

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=api_messages,
    )

    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)

    return "\n".join(text_parts).strip()


def analyze_sender_type(sender: str, subject: str, body: str, is_internal: bool, is_ticket: bool) -> Dict[str, Any]:
    """Analyze sender characteristics to personalize response."""
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    body_lower = body.lower()

    analysis = {
        "is_internal": is_internal,
        "is_ticket": is_ticket,
        "is_urgent": False,
        "is_question": False,
        "is_request": False,
        "tone": "professional",
        "context": []
    }

    # Detect urgency
    urgent_keywords = ["urgent", "asap", "emergency", "critical", "immediately", "help!", "down", "not working", "broken"]
    if any(kw in subject_lower or kw in body_lower[:500] for kw in urgent_keywords):
        analysis["is_urgent"] = True
        analysis["context"].append("This appears urgent")

    # Detect question
    if "?" in body or any(q in body_lower for q in ["can you", "could you", "would you", "how do", "what is", "where is", "when will"]):
        analysis["is_question"] = True
        analysis["context"].append("Contains questions")

    # Detect request/action needed
    action_keywords = ["please", "need", "require", "request", "can you", "could you", "would you"]
    if any(kw in body_lower for kw in action_keywords):
        analysis["is_request"] = True
        analysis["context"].append("Action requested")

    # Detect follow-up/continuation
    followup_keywords = ["following up", "per our", "as discussed", "as mentioned", "per your"]
    if any(kw in body_lower for kw in followup_keywords):
        analysis["context"].append("Follow-up message")

    # Set tone based on internal/external
    if is_internal:
        analysis["tone"] = "friendly and collaborative"
    else:
        analysis["tone"] = "professional and helpful"

    return analysis


def build_enhanced_prompt(
    subject: str,
    sender: str,
    body: str,
    is_ticket_new: bool,
    is_internal: bool,
    conversation_history: str = ""
) -> str:
    """Build an enhanced, context-aware prompt for the LLM."""

    sender_analysis = analyze_sender_type(sender, subject, body, is_internal, is_ticket_new)

    # Build context-aware instructions
    if is_ticket_new:
        instructions = (
            "You are an IT support professional replying to a NEW support ticket.\n\n"
            "Your reply should:\n"
            "1. Acknowledge receipt promptly\n"
            "2. Show you understand the issue by briefly summarizing it\n"
            "3. Ask 2–5 targeted diagnostic questions to gather necessary information\n"
            "4. If appropriate, suggest 1–3 immediate troubleshooting steps they can try\n"
            "5. Set clear expectations for next steps and timeline\n"
            "6. Be empathetic - they're reporting a problem\n\n"
        )
    elif is_internal:
        instructions = (
            "You are replying to an internal colleague.\n\n"
            "Your reply should:\n"
            "1. Be friendly yet professional\n"
            "2. Directly address their question or request\n"
            "3. Provide clear next steps or ask clarifying questions\n"
            "4. If you need information, be specific about what you need\n"
            "5. Keep it concise - respect their time\n\n"
        )
    else:
        instructions = (
            "You are replying to an external contact or customer.\n\n"
            "Your reply should:\n"
            "1. Be professional and courteous\n"
            "2. Directly address their inquiry or request\n"
            "3. Ask specific questions if you need more information\n"
            "4. Provide clear next steps or timeline\n"
            "5. Maintain a helpful, service-oriented tone\n\n"
        )

    # Add context-specific guidance
    if sender_analysis["is_urgent"]:
        instructions += "NOTE: This appears to be URGENT. Acknowledge the urgency and provide a rapid response timeline.\n"

    if sender_analysis["is_question"]:
        instructions += "NOTE: This contains direct questions. Make sure to address each question explicitly.\n"

    if sender_analysis["is_request"]:
        instructions += "NOTE: This contains a specific request. Confirm you can help or explain what you need to proceed.\n"

    instructions += "\nIMPORTANT RULES:\n"
    instructions += "- Do NOT invent facts or make promises you're unsure about\n"
    instructions += "- If you need information before proceeding, ask specific questions\n"
    instructions += "- Keep the reply concise but complete\n"
    instructions += "- Write in plain text with proper paragraphs\n"
    instructions += "- NEVER include a signature block, sign-off name, or closing like 'Best regards'\n"
    instructions += "- NEVER include placeholders like [Your Name], [Colleague's Name], or [End of message]\n"
    instructions += "- NEVER repeat the Subject line in the reply body\n"
    instructions += "- Just write the reply body text, nothing else\n"

    # Build the prompt
    prompt_parts = [instructions]

    if conversation_history:
        prompt_parts.append(conversation_history)

    prompt_parts.extend([
        f"CURRENT EMAIL TO REPLY TO:",
        f"From: {sender}",
        f"Subject: {subject}",
        f"",
        f"Message:",
        body,
        "",
        "Write an appropriate reply now:"
    ])

    return "\n".join(prompt_parts)


def generate_reply(
    model: str,
    subject: str,
    sender: str,
    body: str,
    is_ticket_new: bool,
    is_internal: bool,
    conversation_history: str,
    debug: bool,
) -> str:
    """Generate an intelligent reply using Anthropic Claude with enhanced context awareness."""

    prompt = build_enhanced_prompt(
        subject=subject,
        sender=sender,
        body=body,
        is_ticket_new=is_ticket_new,
        is_internal=is_internal,
        conversation_history=conversation_history
    )

    log_jsonl(LLM_LOG, {
        "event": "anthropic_request",
        "model": model,
        "sender": sender,
        "subject": subject,
        "is_ticket_new": is_ticket_new,
        "is_internal": is_internal,
        "has_conversation_history": bool(conversation_history),
        "prompt_preview": prompt[:1500],
    })

    system_prompt = (
        "You are an expert at drafting professional, accurate email replies. "
        "You understand business communication nuances and adapt your tone appropriately. "
        "Never invent facts. Never include signatures, sign-offs, or placeholders like [Your Name]. "
        "Never repeat the subject line. Never include 'Best regards' or similar closings. "
        "Write ONLY the reply body text — nothing else."
    )

    try:
        reply = anthropic_chat(
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            model=model,
        )
    except Exception as e:
        log_jsonl(LLM_LOG, {"event": "anthropic_error", "error": str(e), "model": model})
        if debug:
            print(f"DEBUG: Anthropic error: {e}")

        # Provide a context-appropriate fallback
        if is_ticket_new:
            return (
                "Thank you for reporting this issue. I'm looking into it now.\n\n"
                "To help diagnose this more quickly, could you please provide:\n"
                "- Any error messages or screenshots\n"
                "- When this started happening\n"
                "- What you were doing when you encountered this\n\n"
                "I'll follow up shortly once I have more details."
            )
        else:
            return (
                "Thanks for your email — I'm reviewing this now.\n"
                "If you can share any additional details, that would help.\n"
                "I'll follow up shortly."
            )

    log_jsonl(LLM_LOG, {
        "event": "anthropic_response",
        "model": model,
        "subject": subject,
        "reply_preview": reply[:1500],
        "reply_len": len(reply),
    })

    if debug:
        print(f"DEBUG: Anthropic reply_len={len(reply)} model={model}")

    # Clean up the reply
    reply = reply.strip()

    # Remove common unwanted patterns
    if reply.lower().startswith("dear "):
        lines = reply.split("\n", 1)
        reply = lines[1].strip() if len(lines) > 1 else reply

    # Remove "Subject: Re: ..." lines at the start
    if reply.lower().startswith("subject:"):
        lines = reply.split("\n", 1)
        reply = lines[1].strip() if len(lines) > 1 else reply

    # Remove trailing signature/sign-off blocks
    signoff_patterns = [
        r"\n\s*(Best regards|Kind regards|Regards|Sincerely|Thanks|Thank you|Cheers),?\s*\n\s*\[.*?\]\s*$",
        r"\n\s*(Best regards|Kind regards|Regards|Sincerely|Thanks|Thank you|Cheers),?\s*\n\s*[-—]\s*$",
        r"\n\s*\[Your Name\].*$",
        r"\n\s*\[End of message\].*$",
        r"\n\s*\[Colleague'?s? Name\].*$",
        r"\n\s*(Best regards|Kind regards|Regards|Sincerely),?\s*$",
    ]
    for pat in signoff_patterns:
        reply = re.sub(pat, "", reply, flags=re.IGNORECASE | re.DOTALL).strip()

    # Ensure we have a reply
    if not reply:
        return "Thanks — I'm reviewing this now and will follow up shortly."

    return reply


# ----------------------------
# Bot Delegation (SQL Bot for data lookups)
# ----------------------------
def detect_intent(subject: str, body: str, intent_keywords: Dict[str, List[str]]) -> Optional[str]:
    """Detect if the email requires delegation to another bot.

    Returns the intent name (e.g. 'lead_time', 'order_status') or None.
    """
    text = f"{subject} {body}".lower()
    for intent, keywords in intent_keywords.items():
        if any(kw in text for kw in keywords):
            return intent
    return None


def get_db_connection(rules: Dict[str, Any]):
    """Get a database connection using config or environment variables."""
    if pyodbc is None:
        return None

    host = rules.get("db_host") or os.getenv("DB_HOST", "")
    user = rules.get("db_user") or os.getenv("DB_USER", "")
    password = rules.get("db_password") or os.getenv("DB_PASSWORD", "")
    database = rules.get("db_name") or os.getenv("DB_NAME", "")

    if not user or not host:
        return None

    try:
        conn_str = (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={host};"
            f"DATABASE={database};"
            f"UID={user};"
            f"PWD={password};"
            f"Encrypt=yes;TrustServerCertificate=yes;"
        )
        return pyodbc.connect(conn_str, timeout=10)
    except Exception:
        return None


def run_sql_query(conn, query: str, max_rows: int = 20) -> Optional[List[Dict[str, Any]]]:
    """Execute a read-only SQL query and return results as list of dicts."""
    # Safety: block destructive queries
    q_upper = query.upper().strip()
    if any(kw in q_upper for kw in ["DROP", "DELETE", "TRUNCATE", "UPDATE", "INSERT", "ALTER", "EXEC"]):
        return None
    if not q_upper.startswith("SELECT"):
        return None

    try:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchmany(max_rows)
        return [dict(zip(columns, row)) for row in rows]
    except Exception:
        return None


def generate_sql_for_intent(
    model: str,
    intent: str, subject: str, body: str, debug: bool
) -> Optional[str]:
    """Use the LLM to generate a SQL query based on the email intent."""
    intent_prompts = {
        "lead_time": (
            "The customer is asking about LEAD TIME or delivery dates.\n"
            "Generate a SQL query to look up lead time information.\n"
            "Common tables: SO_SalesOrderHeader, SO_SalesOrderDetail, CI_Item, IM_ItemWarehouse.\n"
            "Look for columns like: LeadTimeDays, PromiseDate, RequestedShipDate, ShipDate.\n"
            "If the email mentions a specific item, PO, or order number, filter by that.\n"
        ),
        "order_status": (
            "The customer is asking about ORDER STATUS.\n"
            "Generate a SQL query to look up order status.\n"
            "Common tables: SO_SalesOrderHeader, SO_SalesOrderDetail.\n"
            "Look for columns like: OrderStatus, OrderDate, ShipDate, CustomerPONo.\n"
            "If the email mentions a specific PO or order number, filter by that.\n"
        ),
        "inventory": (
            "The customer is asking about INVENTORY or STOCK availability.\n"
            "Generate a SQL query to check inventory levels.\n"
            "Common tables: IM_ItemWarehouse, CI_Item.\n"
            "Look for columns like: QuantityOnHand, QuantityAvailable, ItemCode.\n"
            "If the email mentions a specific item, filter by that.\n"
        ),
        "report_request": (
            "The user is requesting a REPORT to be pulled from the database.\n"
            "Generate a SQL query to pull the requested report data.\n"
            "Common report tables:\n"
            "  - SO_SalesOrderHeader / SO_SalesOrderDetail: Sales orders, order dates, ship dates, POs, CustomerNo\n"
            "  - AR_Customer: Customer info (CustomerNo, CustomerName, City, State)\n"
            "  - CI_Item: Item master (ItemCode, ItemCodeDesc, ProductLine)\n"
            "  - IM_ItemWarehouse: Inventory by warehouse (QuantityOnHand, QuantityAvailable, WarehouseCode)\n"
            "  - PO_PurchaseOrderHeader / PO_PurchaseOrderDetail: Purchase orders, vendor info\n"
            "Common joins: SalesOrderNo, ItemCode, CustomerNo, PurchaseOrderNo\n"
            "Use TOP 500 for reports (larger result set).\n"
            "If the email mentions a customer, item, date range, or order type, filter accordingly.\n"
        ),
    }

    prompt = intent_prompts.get(intent, "")
    if not prompt:
        return None

    row_limit = 500 if intent == "report_request" else 20
    system = (
        "You are a SQL Server query generator for a Sage 100 ERP database (MAS_JEF). "
        "Return ONLY the SQL query, no explanation, no markdown. "
        f"Use TOP {row_limit} to limit results. Only generate SELECT queries."
    )

    user_prompt = (
        f"{prompt}\n"
        f"Email subject: {subject}\n"
        f"Email body (excerpt): {body[:1000]}\n\n"
        f"Generate a safe, read-only SELECT query:"
    )

    try:
        sql = anthropic_chat(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            max_tokens=512,
        )
        # Clean markdown formatting
        sql = sql.strip()
        if sql.startswith("```"):
            lines = sql.split("\n")
            sql = "\n".join(l for l in lines if not l.startswith("```")).strip()
        return sql if sql.upper().strip().startswith("SELECT") else None
    except Exception:
        return None


def format_query_results(results: List[Dict[str, Any]], intent: str) -> str:
    """Format SQL query results into a human-readable string for the draft."""
    if not results:
        return ""

    lines = [f"\n--- Data from database ({intent.replace('_', ' ').title()}) ---"]
    # Build a simple table
    headers = list(results[0].keys())
    lines.append(" | ".join(str(h) for h in headers))
    lines.append("-" * 60)
    for row in results[:10]:
        lines.append(" | ".join(str(row.get(h, "")) for h in headers))
    if len(results) > 10:
        lines.append(f"... ({len(results)} total rows)")
    lines.append("--- End of data ---\n")
    return "\n".join(lines)


REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def export_results_to_csv(
    results: List[Dict[str, Any]], intent: str, subject: str,
) -> Optional[str]:
    """Export query results to a CSV file. Returns the file path or None."""
    if not results:
        return None

    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)

        # Build a safe filename from the subject
        safe_subj = re.sub(r"[^a-zA-Z0-9_\- ]", "", subject)[:50].strip().replace(" ", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{intent}_{safe_subj}_{ts}.csv"
        filepath = os.path.join(REPORTS_DIR, filename)

        headers = list(results[0].keys())
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(results)

        return filepath
    except Exception:
        return None


def delegate_and_enrich(
    rules: Dict[str, Any],
    subject: str,
    body: str,
    debug: bool,
) -> Dict[str, Any]:
    """Detect email intent, query database if needed, return enrichment data.

    Returns dict with keys:
        text: enrichment text for draft (empty string if no delegation)
        csv_path: path to CSV file if report was exported (None otherwise)
        row_count: number of rows returned (0 if none)
    """
    _empty = {"text": "", "csv_path": None, "row_count": 0}

    if not rules.get("delegation_enabled", False):
        return _empty

    intent_keywords = rules.get("delegation_intent_keywords", {})
    intent = detect_intent(subject, body, intent_keywords)
    if intent is None:
        return _empty

    if debug:
        print(f"DEBUG: Delegation intent detected: {intent}")

    log_jsonl(ACTIONS_LOG, {
        "event": "delegation_intent_detected",
        "intent": intent,
        "subject": subject,
    })

    # Generate SQL query
    anthropic_model = rules.get("anthropic_model", "claude-sonnet-4-20250514")

    sql = generate_sql_for_intent(
        model=anthropic_model,
        intent=intent, subject=subject, body=body, debug=debug,
    )

    if not sql:
        if debug:
            print("DEBUG: Could not generate SQL query for delegation")
        return _empty

    if debug:
        print(f"DEBUG: Generated SQL: {sql[:200]}")

    log_jsonl(ACTIONS_LOG, {
        "event": "delegation_sql_generated",
        "intent": intent,
        "sql": sql[:500],
    })

    # Execute query
    conn = get_db_connection(rules)
    if conn is None:
        if debug:
            print("DEBUG: No database connection available for delegation")
        log_jsonl(ACTIONS_LOG, {"event": "delegation_no_db_connection"})
        return _empty

    # Reports get more rows than simple lookups
    max_rows = 500 if intent == "report_request" else 20

    try:
        results = run_sql_query(conn, sql, max_rows=max_rows)
        conn.close()
    except Exception as e:
        if debug:
            print(f"DEBUG: SQL query failed: {e}")
        log_jsonl(ACTIONS_LOG, {"event": "delegation_sql_error", "error": str(e)})
        try:
            conn.close()
        except Exception:
            pass
        return _empty

    if not results:
        if debug:
            print("DEBUG: SQL query returned no results")
        return _empty

    enrichment = format_query_results(results, intent)

    # For report requests, also export full results to CSV
    csv_path = None
    if intent == "report_request":
        csv_path = export_results_to_csv(results, intent, subject)
        if csv_path:
            enrichment += f"\nFull report exported to CSV: {csv_path}\n"
            enrichment += f"({len(results)} rows saved)\n"
            if debug:
                print(f"DEBUG: Report CSV saved to {csv_path}")

    log_jsonl(ACTIONS_LOG, {
        "event": "delegation_data_retrieved",
        "intent": intent,
        "row_count": len(results),
        "csv_path": csv_path,
    })

    if debug:
        print(f"DEBUG: Delegation retrieved {len(results)} rows for intent={intent}")

    return {"text": enrichment, "csv_path": csv_path, "row_count": len(results)}


# ----------------------------
# Candidate selection (UNREAD ONLY)
# ----------------------------
def snapshot_candidates(inbox, lookback_days: int, max_total: int, unread_only: bool = True) -> List[Tuple[str, Optional[str]]]:
    """
    Get candidate emails for processing.

    Args:
        inbox: Outlook inbox folder
        lookback_days: How many days back to look
        max_total: Maximum number of candidates to return
        unread_only: If True, only return unread messages (default: True)

    Returns:
        List of (entry_id, store_id) tuples
    """
    cutoff_ts = dt_to_ts(now_local() - timedelta(days=int(lookback_days)))
    items = inbox.Items

    try:
        folder_store_id = inbox.StoreID
    except Exception:
        folder_store_id = None

    candidates: List[Tuple[str, Optional[str]]] = []
    seen = set()

    # Get UNREAD items only
    try:
        unread_items = items.Restrict("[UnRead] = True")
    except Exception:
        unread_items = None

    if unread_items is not None:
        try:
            unread_items.Sort("[ReceivedTime]", True)
        except Exception:
            pass

        for i in range(1, unread_items.Count + 1):
            if len(candidates) >= max_total:
                break
            try:
                it = unread_items.Item(i)
                if getattr(it, "Class", None) != OL_MAILITEM_CLASS:
                    continue
                ts = item_time_ts(it)
                if ts > 0 and ts < cutoff_ts:
                    continue
                entry_id = it.EntryID
                store_id = folder_store_id or getattr(it.Parent, "StoreID", None)
                key = (entry_id, store_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(key)
            except Exception:
                continue

    # If unread_only is False, top up with read messages (legacy behavior)
    # By default, we skip this section to only process unread messages
    if not unread_only and len(candidates) < max_total:
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass

        remaining = max_total - len(candidates)
        scan_limit = min(items.Count, 8000)

        for i in range(1, scan_limit + 1):
            if remaining <= 0:
                break
            try:
                it = items.Item(i)
                if getattr(it, "Class", None) != OL_MAILITEM_CLASS:
                    continue
                if bool(getattr(it, "UnRead", False)):
                    continue
                ts = item_time_ts(it)
                if ts > 0 and ts < cutoff_ts:
                    continue
                entry_id = it.EntryID
                store_id = folder_store_id or getattr(it.Parent, "StoreID", None)
                key = (entry_id, store_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(key)
                remaining -= 1
            except Exception:
                continue

    return candidates


# ----------------------------
# Main processing pass
# ----------------------------
def process_pass(ns, cfg) -> Dict[str, Any]:
    debug = bool(cfg.get("debug", True))
    folders_cfg = cfg["folders"]
    rules = cfg["rules"]

    mailbox_name = cfg.get("mailbox_name", "")
    mailbox = find_mailbox_root(ns, mailbox_name)
    if mailbox is None:
        raise RuntimeError("Mailbox root not found. Set mailbox_name='' for default mailbox.")

    inbox = mailbox.Folders.Item(cfg.get("inbox_folder", "Inbox"))

    # Ensure folders exist
    create_folders = bool(cfg.get("create_folders_if_missing", True))
    f_alerts = get_or_create_subfolder(inbox, folders_cfg["automated_alerts"], create_folders)
    f_filed = get_or_create_subfolder(inbox, folders_cfg["filed"], create_folders)
    f_to_reply = get_or_create_subfolder(inbox, folders_cfg["to_reply"], create_folders)
    f_waiting = get_or_create_subfolder(inbox, folders_cfg["waiting"], create_folders)
    if any(x is None for x in (f_alerts, f_filed, f_to_reply, f_waiting)):
        raise RuntimeError("Missing required subfolders under Inbox. Enable create_folders_if_missing or create them.")

    drafts = ns.GetDefaultFolder(OL_FOLDER_DRAFTS)

    # refresh customer cache periodically
    last_refresh = load_last_refresh_ts()
    refresh_minutes = float(rules.get("customer_cache_refresh_minutes", 30))
    if time.time() - last_refresh > refresh_minutes * 60:
        refresh_customers_from_sent(ns, mailbox, rules, debug)
        save_last_refresh_ts(time.time())

    customer_cache = load_customers_cache()
    internal_domain = safe_lower(cfg.get("internal_domain", "jeffcofibres.com"))
    ticket_re = re.compile(rules.get("ticket_new_subject_regex", r"$^"), re.IGNORECASE)
    ticket_resolved_re = re.compile(rules.get("ticket_resolved_subject_regex", r"$^"), re.IGNORECASE)

    # Anthropic config
    anthropic_model = rules.get("anthropic_model", "claude-sonnet-4-20250514")
    max_body = int(rules.get("max_body_chars_to_model", 8000))
    cw_triage_enabled = bool(rules.get("cw_triage_enabled", True))

    candidates = snapshot_candidates(
        inbox,
        lookback_days=int(cfg.get("lookback_days", 20)),
        max_total=int(cfg.get("max_items_per_pass", 100)),
        unread_only=bool(cfg.get("unread_only", True)),
    )

    if debug:
        unread_mode = "UNREAD ONLY" if bool(cfg.get("unread_only", True)) else "UNREAD + READ"
        print(f"DEBUG: candidates={len(candidates)} (mode: {unread_mode})")

    moved = {"alerts": 0, "filed": 0, "to_reply": 0, "waiting": 0}
    drafts_created = 0
    processed = 0
    drafted_conversations = set()
    non_noise_emails = []  # track for CEO bot orchestration
    delegations = []       # track delegation actions
    triage_actions = []    # track CW ticket triage for CEO bot

    for entry_id, store_id in candidates:
        try:
            item = ns.GetItemFromID(entry_id, store_id) if store_id else ns.GetItemFromID(entry_id)
        except Exception:
            continue

        try:
            if item.Class != OL_MAILITEM_CLASS:
                continue
        except Exception:
            continue

        sender = get_sender_smtp(item)
        subj = get_subject(item)
        sender_dom = domain_of(sender)

        is_ticket_new = bool(ticket_re.search(subj or ""))
        is_ticket_resolved = bool(ticket_resolved_re.search(subj or ""))
        try:
            is_unread = bool(getattr(item, "UnRead", False))
        except Exception:
            is_unread = False

        # Helpdesk tickets: only draft if NEW and UNREAD; move read ones to Filed
        if is_ticket_new and not is_unread:
            try:
                item.Move(f_filed)
                moved["filed"] += 1
            except Exception:
                pass
            log_jsonl(ACTIONS_LOG, {
                "event": "ticket_read_filed",
                "sender": sender,
                "sender_domain": sender_dom,
                "subject": subj,
            })
            processed += 1
            continue
        if is_ticket_resolved:
            try:
                item.UnRead = False
            except Exception:
                pass
            try:
                item.Move(f_filed)
                moved["filed"] += 1
            except Exception:
                pass
            log_jsonl(ACTIONS_LOG, {
                "event": "ticket_resolved_filed",
                "sender": sender,
                "sender_domain": sender_dom,
                "subject": subj,
            })
            processed += 1
            continue

        # Deduplicate drafts by ConversationID (prevents multiple drafts for the same thread in one pass)
        try:
            conv_id = getattr(item, "ConversationID", "") or ""
        except Exception:
            conv_id = ""

        non_noise = is_non_noise(
            sender_email=sender,
            subject=subj,
            internal_domain=internal_domain,
            customer_cache=customer_cache,
            ticket_re=ticket_re,
            rules=rules,
            item=item,
        )

        # ---- NOISE ----
        if not non_noise:
            auto_like = is_automated_like(sender, subj, rules) or hard_noise(sender, subj, item, rules)
            try:
                item.UnRead = False
            except Exception:
                pass

            dest_name = "Automated Alerts" if auto_like else "Filed"
            try:
                if auto_like:
                    item.Move(f_alerts)
                    moved["alerts"] += 1
                else:
                    item.Move(f_filed)
                    moved["filed"] += 1
            except Exception:
                pass

            log_jsonl(ACTIONS_LOG, {
                "event": "noise_moved",
                "sender": sender,
                "sender_domain": sender_dom,
                "subject": subj,
                "dest": dest_name,
                "reason": "hard_noise_or_not_internal_or_not_customer",
            })
            processed += 1
            continue

        # ---- NON-NOISE (draft) ----
        body = extract_plain_text(item, max_body)

        # Determine if sender is internal
        is_internal = sender.endswith("@" + internal_domain)

        # only draft once per conversation per pass (unless no ConversationID)
        should_draft = True
        if conv_id:
            if conv_id in drafted_conversations:
                should_draft = False

        draft_text = ""
        drafted_ok = False
        draft_error = ""

        if should_draft:
            # Get conversation context for better replies
            conversation_history = ""
            try:
                conversation_history = get_conversation_context(item, max_items=3)
            except Exception as e:
                if debug:
                    print(f"DEBUG: Could not get conversation context: {e}")

            # --- ConnectWise ticket triage path ---
            delegation_text = ""
            delegation_csv = None
            delegation_intent = None
            triage_result = None

            if is_ticket_new and cw_triage_enabled and cw_triage_ticket is not None:
                try:
                    triage_result = cw_triage_ticket(
                        anthropic_chat_fn=anthropic_chat,
                        model=anthropic_model,
                        subject=subj,
                        body=body,
                        sender=sender,
                    )
                    draft_text = triage_result.get("response_text", "")

                    # Log triage decision
                    log_jsonl(ACTIONS_LOG, {
                        "event": "cw_ticket_triaged",
                        "ticket_number": triage_result["ticket_info"].get("ticket_number"),
                        "category": triage_result["classification"].get("category"),
                        "urgency": triage_result["classification"].get("urgency"),
                        "summary": triage_result["classification"].get("summary"),
                        "sender": sender,
                        "subject": subj,
                    })

                    # Track for CEO reporting
                    triage_actions.append({
                        "ticket_number": triage_result["ticket_info"].get("ticket_number"),
                        "category": triage_result["classification"]["category"],
                        "urgency": triage_result["classification"]["urgency"],
                        "summary": triage_result["classification"].get("summary", ""),
                        "sender": sender,
                        "subject": subj,
                    })

                    if debug:
                        cat = triage_result["classification"]["category"]
                        urg = triage_result["classification"]["urgency"]
                        tkt = triage_result["ticket_info"].get("ticket_number", "?")
                        print(f"DEBUG: CW Ticket #{tkt} triaged: {cat}/{urg}")
                except Exception as e:
                    if debug:
                        print(f"DEBUG: CW triage failed, falling back to generic reply: {e}")
                    draft_text = ""

            # --- Standard reply path (non-ticket or triage fallback) ---
            if not draft_text:
                # Delegate to SQL bot if email matches a data-lookup intent
                delegation_result = {"text": "", "csv_path": None, "row_count": 0}
                try:
                    intent_keywords = rules.get("delegation_intent_keywords", {})
                    delegation_intent = detect_intent(subj, body, intent_keywords) if rules.get("delegation_enabled") else None
                    delegation_result = delegate_and_enrich(
                        rules=rules, subject=subj, body=body, debug=debug,
                    )
                except Exception as e:
                    if debug:
                        print(f"DEBUG: Delegation failed: {e}")

                delegation_text = delegation_result.get("text", "") if isinstance(delegation_result, dict) else str(delegation_result)
                delegation_csv = delegation_result.get("csv_path") if isinstance(delegation_result, dict) else None

                # Combine conversation history with delegation data
                enriched_context = conversation_history
                if delegation_text:
                    enriched_context += "\n" + delegation_text + "\n"
                    enriched_context += (
                        "IMPORTANT: The database data above is relevant to this email. "
                        "Use it to provide a specific, data-driven response.\n"
                    )
                    if delegation_csv:
                        enriched_context += (
                            f"A full CSV report has been saved to: {delegation_csv}\n"
                            "Mention this file path in your reply so the requester knows the report is ready.\n"
                        )

                # Generate intelligent reply with context
                draft_text = generate_reply(
                    model=anthropic_model,
                    subject=subj,
                    sender=sender,
                    body=body,
                    is_ticket_new=is_ticket_new,
                    is_internal=is_internal,
                    conversation_history=enriched_context,
                    debug=debug,
                )

            # Create the draft and verify placement
            drafted_ok, draft_error = make_reply_draft(item, draft_text, drafts, debug=debug)

            if drafted_ok:
                drafts_created += 1
                if debug and draft_error:
                    print(f"DEBUG: Draft created with warning: {draft_error}")
            else:
                if debug:
                    print(f"DEBUG: Failed to create draft: {draft_error}")
                log_jsonl(ACTIONS_LOG, {
                    "event": "draft_creation_failed",
                    "sender": sender,
                    "subject": subj,
                    "error": draft_error
                })

            if conv_id:
                drafted_conversations.add(conv_id)

        # move original message
        dest = "Waiting"
        try:
            if is_unread and bool(rules.get("draft_for_unread_non_noise_to_to_reply", True)):
                item.Move(f_to_reply)
                moved["to_reply"] += 1
                dest = "To Reply"
            else:
                item.Move(f_waiting)
                moved["waiting"] += 1
                dest = "Waiting"
        except Exception:
            pass

        log_jsonl(ACTIONS_LOG, {
            "event": "non_noise_processed",
            "sender": sender,
            "sender_domain": sender_dom,
            "subject": subj,
            "dest": dest,
            "is_ticket_new": is_ticket_new,
            "draft_created": drafted_ok,
            "draft_preview": (draft_text[:400] if drafted_ok else ""),
            "delegated": bool(delegation_text),
            "triaged": triage_result is not None if should_draft else False,
        })

        # Track for structured results
        email_info = {
            "sender": sender,
            "subject": subj,
            "dest": dest,
            "draft_created": drafted_ok,
            "delegated": bool(delegation_text),
            "intent": delegation_intent,
            "csv_path": delegation_csv,
            "triaged": triage_result is not None if should_draft else False,
        }
        non_noise_emails.append(email_info)
        if delegation_text and delegation_intent:
            delegations.append({
                "intent": delegation_intent,
                "subject": subj,
                "sender": sender,
                "csv_path": delegation_csv,
            })

        processed += 1

    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[{ts}] processed={processed} drafts={drafts_created} "
        f"moved={{alerts:{moved['alerts']}, filed:{moved['filed']}, "
        f"to_reply:{moved['to_reply']}, waiting:{moved['waiting']}}} "
        f"triaged={len(triage_actions)}"
    )

    return {
        "processed": processed,
        "drafts_created": drafts_created,
        "moved": dict(moved),
        "non_noise_emails": non_noise_emails,
        "delegations": delegations,
        "triage_actions": triage_actions,
    }


def main():
    config_path = os.path.join(BASE_DIR, "inbox_bot.config.json")
    cfg = load_config(config_path)

    print("Starting Outlook Inbox Bot…")
    print(f"Mailbox: {cfg.get('mailbox_name') or '(default mailbox)'}")
    print(
        f"Lookback: {cfg.get('lookback_days')} days | "
        f"max/pass: {cfg.get('max_items_per_pass')} | loop: {cfg.get('loop_seconds')}s"
    )
    print("NOTE: This does NOT send email. Drafts are created in your personal Drafts.\n")

    if requests is None:
        print("ERROR: requests is not installed. Run: pip install requests")
        sys.exit(1)

    pythoncom.CoInitialize()
    try:
        lock = acquire_single_instance_lock(bool(cfg.get("single_instance_lock", True)), bool(cfg.get("debug", True)))
        if cfg.get("single_instance_lock", True) and lock is None:
            print("Another Inbox Bot instance is already running. Exiting.")
            sys.exit(1)

        outlook, ns = connect_outlook(bool(cfg.get("debug", True)))
        backoff = int(cfg.get("error_backoff_seconds", 15))

        while True:
            try:
                if bool(cfg.get("reload_config_each_pass", True)):
                    cfg = load_config(config_path)
                if ns is None:
                    outlook, ns = connect_outlook(bool(cfg.get("debug", True)))
                    if ns is None:
                        time.sleep(backoff)
                        backoff = min(int(cfg.get("max_backoff_seconds", 300)), backoff * 2)
                        continue

                process_pass(ns, cfg)
                backoff = int(cfg.get("error_backoff_seconds", 15))
            except KeyboardInterrupt:
                print("Stopping (Ctrl+C).")
                sys.exit(0)
            except Exception as e:
                ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts}] ERROR: {e}")
                log_exception("process_pass_error", e)
                ns = None

            time.sleep(int(cfg.get("loop_seconds", 60)))
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    main()
