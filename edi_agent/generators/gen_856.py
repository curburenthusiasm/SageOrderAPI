"""856 -- Ship Notice / Advance Ship Notice (ASN).

Sent once ship_date and tracking_numbers are set in mappings. Builds the
standard HL hierarchy: Shipment -> Order -> Item.
"""
from __future__ import annotations

import uuid

from ..core.models import Order
from .base import builder_for, party_n1_loop, seg, now_time


class Generator856:
    def __init__(self, order: Order, mappings: dict):
        self.order = order
        self.mappings = mappings

    def _content(self, d: str) -> list[str]:
        order = self.order
        m = self.mappings
        ship_date = m.get("ship_date") or order.requested_ship_date
        ship_time = m.get("ship_time", "1200") or now_time()
        carrier = m.get("carrier_code", "")
        ship_method = m.get("ship_method", "")
        tracking_list = (m.get("tracking_numbers") or {}).get(order.po_number, [])
        tracking = tracking_list[0] if tracking_list else ""
        shipment_id = m.get("shipment_id") or uuid.uuid4().hex[:12].upper()

        hl = 0

        def next_hl(parent: str, level: str) -> str:
            nonlocal hl
            hl += 1
            return seg(d, "HL", str(hl), parent, level)

        content = [
            # BSN*purpose*shipment id*ship date*ship time
            seg(d, "BSN", "00", shipment_id, ship_date, ship_time),
            seg(d, "DTM", "011", ship_date),  # 011 = shipped date
        ]

        # --- Shipment level (S) ---
        shipment_hl = next_hl("", "S")
        content.append(shipment_hl)
        # TD1: packaging/weight summary (carton count = number of lines as fallback)
        content.append(seg(d, "TD1", "CTN", str(len(order.lines))))
        # TD5: carrier routing
        if carrier or ship_method:
            content.append(seg(d, "TD5", "", "2", carrier, "", ship_method))
        # REF: tracking / carrier reference at shipment level
        if tracking:
            content.append(seg(d, "REF", "CN", tracking))
        content += party_n1_loop(d, "ST", order.ship_to)

        # --- Order level (O) ---
        content.append(next_hl("1", "O"))
        content.append(seg(d, "PRF", order.po_number, "", "", order.po_date))

        # --- Item levels (I), one per line ---
        order_hl_index = hl  # parent for items
        for line in order.lines:
            content.append(next_hl(str(order_hl_index), "I"))
            # LIN: item identification
            lin = ["LIN", line.line_num]
            if line.buyer_part:
                lin += ["BP", line.buyer_part]
            if line.vendor_part:
                lin += ["VP", line.vendor_part]
            if line.upc:
                lin += ["UP", line.upc]
            content.append(seg(d, *lin))
            # SN1: shipped quantity
            content.append(seg(d, "SN1", "", _num(line.qty_acknowledged or line.qty_ordered), line.uom))
            if line.description:
                content.append(seg(d, "PID", "F", "", "", "", line.description))

        # CTT: total HL segments
        content.append(seg(d, "CTT", str(hl)))
        return content

    def to_x12(self) -> str:
        b = builder_for(self.order)
        return b.build(
            st_code="856",
            content_segments=self._content(b.element_delim),
            st_control=(self.order.st_control or "0001"),
        )


def _num(value) -> str:
    f = float(value or 0)
    return str(int(f)) if f.is_integer() else str(f)


def generate_856(order: Order, mappings: dict) -> str:
    return Generator856(order, mappings).to_x12()
