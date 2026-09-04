"""855 -- Purchase Order Acknowledgment.

Sent after Robert confirms the field mappings. Echoes each buyer line with
our vendor part, acknowledged quantity, and the agreed price from mappings.
"""
from __future__ import annotations

from ..core.models import Order
from ..mappings import price_for
from .base import builder_for, party_n1_loop, seg, today


class Generator855:
    def __init__(self, order: Order, mappings: dict):
        self.order = order
        self.mappings = mappings

    def _content(self, d: str) -> list[str]:
        order = self.order
        m = self.mappings
        ack_code = m.get("acknowledgment_code", "AC")

        content = [
            # BAK*purpose*ack type*PO number*PO date
            seg(d, "BAK", "00", ack_code, order.po_number, order.po_date or today()),
        ]
        content.append(seg(d, "REF", "PO", order.po_number))

        # FOB freight terms (partner-required, e.g. Walmart).
        content.append(seg(d, "FOB", m.get("fob_payment_code", "PP")))
        # Echo the requested ship date back to the buyer.
        ship_date = m.get("ship_date") or order.requested_ship_date
        if ship_date:
            content.append(seg(d, "DTM", "010", ship_date))

        # Party loops (vendor + ship-to give the partner context).
        content += party_n1_loop(d, "VN", order.vendor)
        content += party_n1_loop(d, "ST", order.ship_to)
        content += party_n1_loop(d, "BY", order.buyer)

        total_qty = 0
        for line in order.lines:
            price = price_for(m, line)
            qty_ack = line.qty_acknowledged or line.qty_ordered
            total_qty += qty_ack
            # PO1 echoes line, ack qty, uom, price, basis, and part qualifiers.
            po1 = ["PO1", line.line_num, _num(qty_ack), line.uom, _money(price), "PE"]
            if line.buyer_part:
                po1 += ["BP", line.buyer_part]
            if line.vendor_part:
                po1 += ["VP", line.vendor_part]
            if line.upc:
                po1 += ["UP", line.upc]
            content.append(seg(d, *po1))
            if line.description:
                content.append(seg(d, "PID", "F", "", "", "", line.description))

            # ACK line-status. If qty or price changed from the 850, flag it.
            qty_changed = line.qty_acknowledged not in (0, line.qty_ordered)
            price_changed = bool(line.unit_price) and abs(price - line.unit_price) > 1e-9
            if qty_changed:
                content.append(seg(d, "ACK", "IQ", _num(qty_ack), line.uom))
            elif price_changed:
                content.append(seg(d, "ACK", "IP", _num(qty_ack), line.uom))
            else:
                content.append(seg(d, "ACK", "IA", _num(qty_ack), line.uom))

        # CTT*line count*hash total (sum of quantities)
        content.append(seg(d, "CTT", str(len(order.lines)), _num(total_qty)))
        return content

    def to_x12(self) -> str:
        b = builder_for(self.order)
        return b.build(
            st_code="855",
            content_segments=self._content(b.element_delim),
            st_control=(self.order.st_control or "0001"),
        )


def _num(value) -> str:
    f = float(value or 0)
    return str(int(f)) if f.is_integer() else str(f)


def _money(value) -> str:
    return f"{float(value or 0):.2f}"


def generate_855(order: Order, mappings: dict) -> str:
    return Generator855(order, mappings).to_x12()
