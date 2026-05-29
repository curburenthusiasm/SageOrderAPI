"""810 -- Invoice.

Sent after the ship date is confirmed (can run alongside the 856). Line
prices come from the mappings; the TDS total is expressed in integer cents.
"""
from __future__ import annotations

import uuid

from ..core.models import Order
from ..mappings import price_for
from .base import builder_for, party_n1_loop, seg, today


class Generator810:
    def __init__(self, order: Order, mappings: dict):
        self.order = order
        self.mappings = mappings

    def _content(self, d: str) -> list[str]:
        order = self.order
        m = self.mappings
        invoice_date = m.get("invoice_date") or today()
        invoice_number = m.get("invoice_number") or f"INV{uuid.uuid4().hex[:8].upper()}"

        content = [
            # BIG*invoice date*invoice number*PO date*PO number
            seg(d, "BIG", invoice_date, invoice_number, order.po_date, order.po_number),
            seg(d, "REF", "PO", order.po_number),
        ]
        content += party_n1_loop(d, "BY", order.buyer)
        content += party_n1_loop(d, "VN", order.vendor)
        content += party_n1_loop(d, "ST", order.ship_to)

        total_cents = 0
        for line in order.lines:
            price = price_for(m, line)
            qty = line.qty_acknowledged or line.qty_ordered
            line_total = round(price * qty, 2)
            total_cents += int(round(line_total * 100))
            it1 = ["IT1", line.line_num, _num(qty), line.uom, _money(price), "PE"]
            if line.buyer_part:
                it1 += ["BP", line.buyer_part]
            if line.vendor_part:
                it1 += ["VP", line.vendor_part]
            if line.upc:
                it1 += ["UP", line.upc]
            content.append(seg(d, *it1))
            if line.description:
                content.append(seg(d, "PID", "F", "", "", "", line.description))

        # TDS: total invoice amount in integer cents.
        content.append(seg(d, "TDS", str(total_cents)))

        # CAD: carrier detail (mirrors 856 TD5).
        carrier = m.get("carrier_code", "")
        tracking_list = (m.get("tracking_numbers") or {}).get(order.po_number, [])
        tracking = tracking_list[0] if tracking_list else ""
        if carrier or tracking:
            content.append(seg(d, "CAD", "", "", "", carrier, "", "", tracking))

        # ITD: payment terms.
        days = m.get("payment_terms_days")
        if days:
            content.append(seg(d, "ITD", "", "", "", "", str(days), "", str(days)))

        # CTT: line count.
        content.append(seg(d, "CTT", str(len(order.lines))))
        return content

    def to_x12(self) -> str:
        b = builder_for(self.order)
        return b.build(
            st_code="810",
            content_segments=self._content(b.element_delim),
            st_control=(self.order.st_control or "0001"),
        )


def _num(value) -> str:
    f = float(value or 0)
    return str(int(f)) if f.is_integer() else str(f)


def _money(value) -> str:
    return f"{float(value or 0):.2f}"


def generate_810(order: Order, mappings: dict) -> str:
    return Generator810(order, mappings).to_x12()
