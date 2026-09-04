"""Core internal data model for the EDI agent.

These dataclasses are the structured representation of an inbound X12 850
(Purchase Order). The parser populates them; the generators consume them.
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class Party:
    """An N1/N2/N3/N4 party block (ship-to, bill-to, buyer, vendor)."""
    qualifier: str = ""       # ID qualifier from N1 element 03, e.g. "92", "ZZ"
    id: str = ""              # ID from N1 element 04
    name: str = ""            # N1 element 02
    address: str = ""         # N3 element 01 (+ 02 if present)
    city: str = ""            # N4 element 01
    state: str = ""           # N4 element 02
    zip: str = ""             # N4 element 03

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LineItem:
    """A single PO1 line plus its associated PID/REF/DTM detail."""
    line_num: str = ""
    po_number: str = ""
    vendor_part: str = ""
    buyer_part: str = ""
    qty_ordered: float = 0
    qty_acknowledged: float = 0   # set by mappings (defaults to qty_ordered)
    unit_price: float = 0.0       # set by mappings (or parsed from PO1)
    uom: str = "EA"               # EA, CA, etc.
    description: str = ""
    upc: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Order:
    """The full parsed purchase order plus mapping-driven fields."""
    isa_control: str = ""
    gs_control: str = ""
    st_control: str = ""
    # Inbound interchange identity (the trading partner who sent the 850).
    partner_isa_qualifier: str = ""
    partner_isa_id: str = ""
    partner_gs_id: str = ""
    # Who the 850 was addressed to (us); used as our outbound sender id.
    receiver_isa_qualifier: str = ""
    receiver_isa_id: str = ""
    receiver_gs_id: str = ""
    po_number: str = ""
    po_date: str = ""
    ship_to: Optional[Party] = None
    bill_to: Optional[Party] = None
    vendor: Optional[Party] = None
    buyer: Optional[Party] = None
    lines: List[LineItem] = field(default_factory=list)
    ship_method: str = ""              # set by mappings
    carrier: str = ""                  # set by mappings
    requested_ship_date: str = ""
    requested_delivery_date: str = ""
    raw_segments: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-serializable representation for API responses."""
        return {
            "isa_control": self.isa_control,
            "gs_control": self.gs_control,
            "st_control": self.st_control,
            "po_number": self.po_number,
            "po_date": self.po_date,
            "ship_to": self.ship_to.to_dict() if self.ship_to else None,
            "bill_to": self.bill_to.to_dict() if self.bill_to else None,
            "vendor": self.vendor.to_dict() if self.vendor else None,
            "buyer": self.buyer.to_dict() if self.buyer else None,
            "lines": [li.to_dict() for li in self.lines],
            "ship_method": self.ship_method,
            "carrier": self.carrier,
            "requested_ship_date": self.requested_ship_date,
            "requested_delivery_date": self.requested_delivery_date,
            "line_count": len(self.lines),
        }


def order_to_storage(order: "Order") -> dict:
    """Full-fidelity serialization of an Order (for session persistence)."""
    return asdict(order)


def order_from_storage(d: dict) -> "Order":
    """Rebuild an Order from :func:`order_to_storage` output."""
    def _party(x):
        return Party(**x) if x else None
    lines = [LineItem(**li) for li in (d.get("lines") or [])]
    scalars = {k: v for k, v in d.items()
               if k not in ("ship_to", "bill_to", "vendor", "buyer", "lines")}
    return Order(
        ship_to=_party(d.get("ship_to")), bill_to=_party(d.get("bill_to")),
        vendor=_party(d.get("vendor")), buyer=_party(d.get("buyer")),
        lines=lines, **scalars,
    )
