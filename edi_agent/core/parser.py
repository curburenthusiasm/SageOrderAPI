"""X12 850 (Purchase Order) parser.

Splits a raw EDI string into segments, auto-detecting delimiters from the
ISA segment, and maps each segment tag to a handler that populates the
internal :class:`Order` model.
"""
from __future__ import annotations

from typing import List

from .models import Order, LineItem, Party


class EDIParseError(Exception):
    """Raised when an inbound document cannot be parsed.

    Carries the offending segment tag and element index for debugging.
    """

    def __init__(self, message: str, segment: str = "", element_index: int = -1):
        self.segment = segment
        self.element_index = element_index
        detail = ""
        if segment:
            detail = f" [segment={segment}"
            if element_index >= 0:
                detail += f", element={element_index}"
            detail += "]"
        super().__init__(message + detail)


# N1 entity qualifier -> Order attribute name
_PARTY_MAP = {
    "ST": "ship_to",
    "BT": "bill_to",
    "BY": "buyer",
    "VN": "vendor",
    "SE": "vendor",   # selling party sometimes used for vendor
    "SF": "ship_to",  # ship-from fallback
}


def _detect_delimiters(raw: str):
    """Return (element_delim, segment_delim, subelement_delim).

    ISA is fixed-width: element delimiter is the 4th char (index 3),
    the sub-element delimiter is ISA16 (the char immediately before the
    segment terminator), and the segment terminator is the char after it.
    """
    if not raw.lstrip().startswith("ISA"):
        raise EDIParseError("Document does not start with ISA segment")

    raw = raw.lstrip()
    element_delim = raw[3]
    # ISA is 105 chars + 1 terminator. ISA16 sub-element delim is at index 104,
    # segment terminator at index 105.
    sub_delim = raw[104] if len(raw) > 104 else ">"
    seg_delim = raw[105] if len(raw) > 105 else "~"
    return element_delim, seg_delim, sub_delim


def _split_segments(raw: str, seg_delim: str) -> List[str]:
    """Split on the segment terminator, tolerating ``~`` and ``~\\n`` styles."""
    segments = []
    for seg in raw.split(seg_delim):
        seg = seg.strip("\r\n").strip()
        if seg:
            segments.append(seg)
    return segments


def parse(raw_edi: str) -> Order:
    """Parse a raw X12 850 string into an :class:`Order`."""
    if not raw_edi or not raw_edi.strip():
        raise EDIParseError("Empty document")

    element_delim, seg_delim, sub_delim = _detect_delimiters(raw_edi)
    segments = _split_segments(raw_edi, seg_delim)
    if not segments:
        raise EDIParseError("No segments found")

    order = Order(raw_segments=segments)
    current_party: Party | None = None
    declared_line_count: int | None = None
    line: LineItem | None = None

    def elems(seg: str) -> List[str]:
        return seg.split(element_delim)

    for seg in segments:
        parts = elems(seg)
        tag = parts[0].upper()

        try:
            if tag == "ISA":
                order.partner_isa_qualifier = parts[5].strip() if len(parts) > 5 else ""
                order.partner_isa_id = parts[6].strip() if len(parts) > 6 else ""
                order.receiver_isa_qualifier = parts[7].strip() if len(parts) > 7 else ""
                order.receiver_isa_id = parts[8].strip() if len(parts) > 8 else ""
                order.isa_control = parts[13].strip() if len(parts) > 13 else ""

            elif tag == "GS":
                order.partner_gs_id = parts[2].strip() if len(parts) > 2 else ""
                order.receiver_gs_id = parts[3].strip() if len(parts) > 3 else ""
                order.gs_control = parts[6].strip() if len(parts) > 6 else ""

            elif tag == "ST":
                if len(parts) > 1 and parts[1].strip() != "850":
                    raise EDIParseError(
                        f"Expected transaction set 850, got {parts[1]}", tag, 1
                    )
                order.st_control = parts[2].strip() if len(parts) > 2 else ""

            elif tag == "BEG":
                # BEG*purpose*type*PO number**date
                order.po_number = parts[3].strip() if len(parts) > 3 else ""
                order.po_date = parts[5].strip() if len(parts) > 5 else ""

            elif tag == "N1":
                ent = parts[1].strip().upper() if len(parts) > 1 else ""
                current_party = Party(
                    name=parts[2].strip() if len(parts) > 2 else "",
                    qualifier=parts[3].strip() if len(parts) > 3 else "",
                    id=parts[4].strip() if len(parts) > 4 else "",
                )
                attr = _PARTY_MAP.get(ent)
                if attr:
                    setattr(order, attr, current_party)

            elif tag == "N3" and current_party is not None:
                addr = parts[1].strip() if len(parts) > 1 else ""
                if len(parts) > 2 and parts[2].strip():
                    addr = f"{addr} {parts[2].strip()}"
                current_party.address = addr

            elif tag == "N4" and current_party is not None:
                current_party.city = parts[1].strip() if len(parts) > 1 else ""
                current_party.state = parts[2].strip() if len(parts) > 2 else ""
                current_party.zip = parts[3].strip() if len(parts) > 3 else ""

            elif tag == "PO1":
                # PO1*line*qty*uom*price*basis*[qual*value]...
                line = LineItem(po_number=order.po_number)
                line.line_num = parts[1].strip() if len(parts) > 1 else str(len(order.lines) + 1)
                if len(parts) > 2 and parts[2].strip():
                    line.qty_ordered = float(parts[2].strip())
                    line.qty_acknowledged = line.qty_ordered
                line.uom = parts[3].strip() if len(parts) > 3 else "EA"
                if len(parts) > 4 and parts[4].strip():
                    try:
                        line.unit_price = float(parts[4].strip())
                    except ValueError:
                        line.unit_price = 0.0
                # Product id qualifier/value pairs start at element 6 (index 6)
                idx = 6
                while idx + 1 < len(parts):
                    qual = parts[idx].strip().upper()
                    val = parts[idx + 1].strip()
                    if qual in ("VP", "VN"):
                        line.vendor_part = val
                    elif qual in ("BP", "IN", "PI"):
                        line.buyer_part = val
                    elif qual == "UP" or qual == "UK" or qual == "EN":
                        line.upc = val
                    idx += 2
                order.lines.append(line)

            elif tag == "PID" and line is not None:
                # PID*F****description  -> description at index 5
                desc = ""
                if len(parts) > 5 and parts[5].strip():
                    desc = parts[5].strip()
                elif len(parts) > 4 and parts[4].strip():
                    desc = parts[4].strip()
                if desc:
                    line.description = desc

            elif tag == "REF":
                qual = parts[1].strip().upper() if len(parts) > 1 else ""
                val = parts[2].strip() if len(parts) > 2 else ""
                if line is not None:
                    if qual in ("VN", "VP"):
                        line.vendor_part = line.vendor_part or val
                    elif qual == "IN":
                        line.buyer_part = line.buyer_part or val

            elif tag == "DTM":
                qual = parts[1].strip() if len(parts) > 1 else ""
                date = parts[2].strip() if len(parts) > 2 else ""
                if qual == "010":
                    order.requested_ship_date = date
                elif qual == "002":
                    order.requested_delivery_date = date

            elif tag == "CTT":
                if len(parts) > 1 and parts[1].strip():
                    declared_line_count = int(float(parts[1].strip()))

        except EDIParseError:
            raise
        except (ValueError, IndexError) as exc:
            raise EDIParseError(f"Failed to parse {tag}: {exc}", tag) from exc

    if not order.po_number:
        raise EDIParseError("No PO number found (missing BEG segment)", "BEG", 3)

    if not order.lines:
        raise EDIParseError("No line items found (missing PO1 segments)", "PO1")

    if declared_line_count is not None and declared_line_count != len(order.lines):
        raise EDIParseError(
            f"CTT line count {declared_line_count} does not match "
            f"actual PO1 count {len(order.lines)}",
            "CTT",
            1,
        )

    return order
