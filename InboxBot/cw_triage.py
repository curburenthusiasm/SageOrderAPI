"""
ConnectWise Manage Ticket Triage Module

Classifies and generates responses for ConnectWise ticket notification emails.
Since direct CW API access is not available (shared third-party vendor),
triage operates entirely through email: replying to CW notification emails
updates the ticket in ConnectWise Manage.

Flow:
  1. parse_ticket_from_email()  — extract ticket #, status, priority from email
  2. classify_ticket_with_claude() — AI classification by category + urgency
  3. generate_triage_response()  — personalized draft based on category template
  4. triage_ticket()             — orchestrate the full pipeline
"""

import json
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Regex patterns for parsing CW ticket emails
# ---------------------------------------------------------------------------
CW_TICKET_NUMBER_RE = re.compile(r"Ticket#(\d+)", re.IGNORECASE)
CW_STATUS_RE = re.compile(r"status=(\w+)", re.IGNORECASE)
CW_PRIORITY_RE = re.compile(r"priority[:\s=]+(\w+)", re.IGNORECASE)
CW_BOARD_RE = re.compile(r"board[:\s=]+([^\n,]+)", re.IGNORECASE)
CW_COMPANY_RE = re.compile(r"company[:\s=]+([^\n,]+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Classification constants
# ---------------------------------------------------------------------------
TICKET_CATEGORIES = [
    "network",      # connectivity, VPN, firewall, DNS, DHCP, Wi-Fi
    "hardware",     # printers, computers, monitors, peripherals, docking stations
    "software",     # application issues, installations, updates, licenses
    "access",       # password resets, permissions, account lockouts, MFA
    "email",        # Outlook, Exchange, email delivery, calendar, Teams
    "other",        # anything not fitting above
]

URGENCY_LEVELS = ["critical", "high", "medium", "low"]

# ---------------------------------------------------------------------------
# Category-specific response templates (Claude personalizes these)
# ---------------------------------------------------------------------------
RESPONSE_TEMPLATES: Dict[str, str] = {
    "network": (
        "Thank you for reporting this network issue. I'm looking into it now.\n\n"
        "To help diagnose this quickly, could you please confirm:\n"
        "- Are other users in your area experiencing the same issue?\n"
        "- Are you connected via Wi-Fi or Ethernet?\n"
        "- Can you access any websites or is it completely down?\n"
        "- When did this start happening?\n\n"
        "I'll begin investigating from the infrastructure side immediately."
    ),
    "hardware": (
        "Thank you for reporting this hardware issue. I'll get this resolved for you.\n\n"
        "Could you please provide the following details:\n"
        "- The make/model or asset tag of the affected device\n"
        "- Any error messages or lights/indicators you're seeing\n"
        "- When the issue started\n"
        "- Have you tried restarting the device?\n\n"
        "I'll check our inventory for replacement options in the meantime."
    ),
    "software": (
        "Thank you for reporting this software issue. I'm reviewing it now.\n\n"
        "To help troubleshoot, could you share:\n"
        "- The exact application name and version if known\n"
        "- Any error messages (a screenshot would be very helpful)\n"
        "- What you were doing when the issue occurred\n"
        "- Has this worked before, or is this a new request?\n\n"
        "I'll start looking into this right away."
    ),
    "access": (
        "Thank you for reaching out about this access issue. I understand "
        "how frustrating it can be when you can't get into what you need.\n\n"
        "To resolve this quickly, please confirm:\n"
        "- Which specific system or application you need access to\n"
        "- Your username or email used to log in\n"
        "- Any error messages you're seeing\n"
        "- Whether this is a new access request or a lost/locked account\n\n"
        "I'll work on getting this sorted as quickly as possible."
    ),
    "email": (
        "Thank you for reporting this email issue. I'm looking into it now.\n\n"
        "Could you please provide:\n"
        "- Whether this affects Outlook desktop, web, or mobile\n"
        "- Any error messages you're seeing\n"
        "- Whether you can send, receive, or both are affected\n"
        "- When this started happening\n\n"
        "I'll check the mail system status on our end immediately."
    ),
    "other": (
        "Thank you for submitting this request. I'm reviewing the details now.\n\n"
        "Could you please provide any additional context that might help:\n"
        "- A more detailed description of what you're experiencing or need\n"
        "- Any relevant screenshots or error messages\n"
        "- How urgently this needs to be resolved\n\n"
        "I'll follow up shortly with next steps."
    ),
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_ticket_from_email(
    subject: str, body: str, sender: str
) -> Dict[str, Any]:
    """Parse ConnectWise ticket details from email subject and body.

    Returns dict with keys:
        ticket_number, status, priority, board, company,
        description, requester, raw_subject
    """
    combined = (subject or "") + "\n" + (body or "")

    ticket_match = CW_TICKET_NUMBER_RE.search(subject or "")
    status_match = CW_STATUS_RE.search(subject or "")
    priority_match = CW_PRIORITY_RE.search(combined)
    board_match = CW_BOARD_RE.search(combined)
    company_match = CW_COMPANY_RE.search(combined)

    return {
        "ticket_number": ticket_match.group(1) if ticket_match else None,
        "status": status_match.group(1) if status_match else None,
        "priority": priority_match.group(1).strip() if priority_match else None,
        "board": board_match.group(1).strip() if board_match else None,
        "company": company_match.group(1).strip() if company_match else None,
        "description": (body or "")[:2000],
        "requester": sender,
        "raw_subject": subject or "",
    }


# ---------------------------------------------------------------------------
# AI Classification
# ---------------------------------------------------------------------------
def classify_ticket_with_claude(
    anthropic_chat_fn: Callable,
    model: str,
    ticket_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Use Claude to classify the ticket by category and urgency.

    Args:
        anthropic_chat_fn: The anthropic_chat() callable from inbox_bot
        model: Anthropic model name
        ticket_info: parsed ticket dict from parse_ticket_from_email()

    Returns dict: {category, urgency, summary, reasoning}
    """
    system_prompt = (
        "You are an IT helpdesk ticket classifier for Jeffco Fibres, a textile manufacturing company. "
        "Classify the following support ticket into exactly ONE category and ONE urgency level.\n\n"
        f"Categories: {', '.join(TICKET_CATEGORIES)}\n"
        f"Urgency levels: {', '.join(URGENCY_LEVELS)}\n\n"
        "Rules for urgency:\n"
        "- critical: Production systems down, entire office affected, security breach\n"
        "- high: Individual cannot work, customer-facing system degraded\n"
        "- medium: Workaround exists, non-blocking issue, feature request\n"
        "- low: Cosmetic, informational, planned change\n\n"
        "Return ONLY valid JSON with keys: category, urgency, summary, reasoning\n"
        "No markdown fences, no explanation outside the JSON."
    )

    user_prompt = (
        f"Ticket #{ticket_info.get('ticket_number', 'unknown')}\n"
        f"Requester: {ticket_info['requester']}\n"
        f"Subject: {ticket_info['raw_subject']}\n"
        f"Priority from CW: {ticket_info.get('priority', 'not specified')}\n"
        f"Board: {ticket_info.get('board', 'not specified')}\n"
        f"Company: {ticket_info.get('company', 'not specified')}\n\n"
        f"Description:\n{ticket_info['description'][:1500]}\n\n"
        "Classify this ticket (return JSON only):"
    )

    try:
        response_text = anthropic_chat_fn(
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            max_tokens=512,
        )

        # Strip markdown fences if present
        response_text = response_text.strip()
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()

        result = json.loads(response_text)

        # Validate and normalize
        category = result.get("category", "other").lower()
        if category not in TICKET_CATEGORIES:
            category = "other"

        urgency = result.get("urgency", "medium").lower()
        if urgency not in URGENCY_LEVELS:
            urgency = "medium"

        return {
            "category": category,
            "urgency": urgency,
            "summary": result.get("summary", ""),
            "reasoning": result.get("reasoning", ""),
        }

    except Exception:
        # Fallback classification
        return {
            "category": "other",
            "urgency": "medium",
            "summary": ticket_info.get("raw_subject", ""),
            "reasoning": "Auto-classification failed; defaulted to other/medium",
        }


# ---------------------------------------------------------------------------
# Response Generation
# ---------------------------------------------------------------------------
def generate_triage_response(
    anthropic_chat_fn: Callable,
    model: str,
    ticket_info: Dict[str, Any],
    classification: Dict[str, Any],
) -> str:
    """Generate a context-aware triage response using Claude.

    Uses the category-specific template as a base, then asks Claude to
    personalize it based on the actual ticket details.
    """
    category = classification.get("category", "other")
    template = RESPONSE_TEMPLATES.get(category, RESPONSE_TEMPLATES["other"])

    system_prompt = (
        "You are an IT support professional at Jeffco Fibres responding to a ConnectWise support ticket. "
        "You have been given a response template and the original ticket details. "
        "Personalize the template to address the specific issue described in the ticket. "
        "Keep the structure of diagnostic questions but make them relevant to the actual problem. "
        "Be empathetic and professional. "
        "Do NOT include signatures, sign-offs, or placeholders like [Your Name]. "
        "Write ONLY the reply body text."
    )

    user_prompt = (
        f"TICKET DETAILS:\n"
        f"Ticket #: {ticket_info.get('ticket_number', 'unknown')}\n"
        f"Category: {category}\n"
        f"Urgency: {classification.get('urgency', 'medium')}\n"
        f"Summary: {classification.get('summary', '')}\n"
        f"Requester: {ticket_info['requester']}\n"
        f"Subject: {ticket_info['raw_subject']}\n"
        f"Description: {ticket_info['description'][:1000]}\n\n"
        f"RESPONSE TEMPLATE (personalize this):\n{template}\n\n"
        "Write a personalized response now:"
    )

    try:
        response = anthropic_chat_fn(
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            model=model,
            max_tokens=1024,
        )
        return response.strip() if response.strip() else template
    except Exception:
        return template


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def triage_ticket(
    anthropic_chat_fn: Callable,
    model: str,
    subject: str,
    body: str,
    sender: str,
) -> Dict[str, Any]:
    """Main entry point: parse, classify, and generate response for a CW ticket.

    Returns dict with keys:
        ticket_info: parsed ticket details
        classification: {category, urgency, summary, reasoning}
        response_text: personalized triage response for the draft
        triaged_at: ISO timestamp
    """
    ticket_info = parse_ticket_from_email(subject, body, sender)

    classification = classify_ticket_with_claude(
        anthropic_chat_fn, model, ticket_info
    )

    response_text = generate_triage_response(
        anthropic_chat_fn, model, ticket_info, classification
    )

    return {
        "ticket_info": ticket_info,
        "classification": classification,
        "response_text": response_text,
        "triaged_at": datetime.now().isoformat(),
    }
