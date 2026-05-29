"""Shared helpers for the X12 document generators."""
from __future__ import annotations

from datetime import datetime
from typing import List

from ..config import config
from ..core.envelope import EnvelopeBuilder
from ..core.models import Order


def builder_for(order: Order) -> EnvelopeBuilder:
    """Construct an :class:`EnvelopeBuilder` for an outbound doc.

    Sender = us (the vendor). Receiver = the trading partner that sent the
    850. Partner IDs are taken from the parsed interchange, never hardcoded.
    """
    sender_id = config.VENDOR_ISA_ID or order.receiver_isa_id
    sender_qual = config.VENDOR_ISA_QUALIFIER or order.receiver_isa_qualifier or "ZZ"
    receiver_id = order.partner_isa_id or "PARTNER"
    receiver_qual = order.partner_isa_qualifier or "ZZ"
    return EnvelopeBuilder(
        sender_id=sender_id,
        sender_qualifier=sender_qual,
        receiver_id=receiver_id,
        receiver_qualifier=receiver_qual,
        version=config.X12_VERSION,
        usage_indicator=config.USAGE_INDICATOR,
    )


def today() -> str:
    """Current date as CCYYMMDD."""
    return datetime.utcnow().strftime("%Y%m%d")


def now_time() -> str:
    """Current time as HHMM."""
    return datetime.utcnow().strftime("%H%M")


def seg(element_delim: str, *fields) -> str:
    """Join fields into a segment, stripping trailing empty elements."""
    parts = [str(f) if f is not None else "" for f in fields]
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return element_delim.join(parts)


def party_n1_loop(element_delim: str, code: str, party) -> List[str]:
    """Build an N1/N3/N4 loop for a party, skipping empty segments."""
    if party is None:
        return []
    out = [seg(element_delim, "N1", code, party.name, party.qualifier, party.id)]
    if party.address:
        out.append(seg(element_delim, "N3", party.address))
    if party.city or party.state or party.zip:
        out.append(seg(element_delim, "N4", party.city, party.state, party.zip))
    return out
