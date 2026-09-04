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


def build_sscc18(company_prefix: str, serial: int) -> str:
    """Build an 18-digit SSCC for a UCC-128 carton label (856 MAN*GM).

    Layout: extension digit (0) + GS1 company prefix + zero-padded serial
    reference (filling to 17 digits) + a GS1 mod-10 check digit.
    """
    prefix = "".join(ch for ch in (company_prefix or "") if ch.isdigit()) or "0000000"
    body = ("0" + prefix)[:16]                       # ext digit + prefix, room for >=1 serial
    fill = 17 - len(body)
    digits17 = (body + str(int(serial)).rjust(fill, "0"))[:17].ljust(17, "0")
    total = sum(int(ch) * (3 if i % 2 == 0 else 1)
                for i, ch in enumerate(reversed(digits17)))
    check = (10 - (total % 10)) % 10
    return digits17 + str(check)


def ship_from_party(mappings: dict, order: Order):
    """Resolve the 856 Ship-From party (mappings -> order.vendor -> config)."""
    from ..core.models import Party
    sf = mappings.get("ship_from") or {}
    if sf.get("name"):
        return Party(name=sf.get("name", ""), address=sf.get("address", ""),
                     city=sf.get("city", ""), state=sf.get("state", ""),
                     zip=sf.get("zip", ""))
    # Prefer the vendor record only if it has a full address; otherwise use the
    # configured warehouse so the SF loop has N3/N4 (Walmart requires it).
    if order.vendor and order.vendor.name and order.vendor.address:
        return order.vendor
    return Party(name=config.SHIP_FROM_NAME, address=config.SHIP_FROM_ADDRESS,
                 city=config.SHIP_FROM_CITY, state=config.SHIP_FROM_STATE,
                 zip=config.SHIP_FROM_ZIP)
