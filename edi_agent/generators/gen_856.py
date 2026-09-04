"""856 -- Ship Notice / Advance Ship Notice (ASN).

Follows the Phase 2 segment spec: BSN + an HL hierarchy of
Shipment -> Order -> Item. One HL*S loop per physical package (split
shipments), one HL*O per PO within it, one HL*I per line shipped.

Ship data comes from ``mappings`` (set by the /ship endpoint or pulled from
ShipStation): ``ship_date``, ``ship_time``, ``carrier_code``, ``service_level``,
``tracking_numbers``, ``bill_of_lading``, and ``packages`` (list of
``{tracking, weight_lbs, lines:[{line_num, qty_shipped}]}``).
"""
from __future__ import annotations

from ..core.models import Order
from .base import (builder_for, build_sscc18, party_n1_loop, seg, ship_from_party,
                   now_time)


class Generator856:
    def __init__(self, order: Order, mappings: dict):
        self.order = order
        self.mappings = mappings

    def _packages(self) -> list:
        """Return the packages to ship; synthesize one from all lines if none."""
        packages = self.mappings.get("packages") or []
        if packages:
            return packages
        tracking_list = self.mappings.get("tracking_numbers") or []
        tracking = tracking_list[0] if tracking_list else ""
        return [{
            "tracking": tracking,
            "weight_lbs": None,
            "lines": [
                {"line_num": li.line_num,
                 "qty_shipped": li.qty_acknowledged or li.qty_ordered}
                for li in self.order.lines
            ],
        }]

    def _content(self, d: str) -> list[str]:
        order = self.order
        m = self.mappings
        ship_date = m.get("ship_date") or order.requested_ship_date
        ship_time = m.get("ship_time") or now_time()
        carrier = m.get("carrier_code", "")
        service = m.get("service_level", "")
        bol = m.get("bill_of_lading", "")
        shipment_id = m.get("shipment_id") or f"{order.po_number}-{ship_date}-001"

        line_by_num = {li.line_num: li for li in order.lines}
        gs1_prefix = m.get("gs1_company_prefix") or _config_prefix()
        ship_from = ship_from_party(m, order)
        hl = 0
        pack_seq = 0

        def next_hl(parent: str, level: str) -> str:
            nonlocal hl
            hl += 1
            return seg(d, "HL", str(hl), parent, level)

        content = [seg(d, "BSN", "00", shipment_id, ship_date, ship_time, "0001")]

        for pkg in self._packages():
            pack_seq += 1
            units = sum(float(pl.get("qty_shipped", 0) or 0) for pl in pkg.get("lines", []))
            # --- Shipment level (one per physical carton) ---
            content.append(next_hl("", "S"))
            shipment_hl = hl
            content.append(seg(d, "DTM", "011", ship_date))
            weight = pkg.get("weight_lbs")
            if weight is not None:
                # TD1: carton count + gross weight (Walmart-required).
                content.append(seg(d, "TD1", "CTN", "1", "", "", "G", _num(weight), "LB"))
            else:
                content.append(seg(d, "TD1", "CTN", "1"))
            tracking = pkg.get("tracking", "")
            content.append(seg(d, "TD5", "", "2", carrier, service, tracking))
            if bol:
                content.append(seg(d, "REF", "BM", bol))
            # UCC-128 carton label (SSCC-18) + pack detail.
            content.append(seg(d, "MAN", "GM", build_sscc18(gs1_prefix, pack_seq)))
            content.append(seg(d, "PO4", _num(units)))
            content += party_n1_loop(d, "ST", order.ship_to)
            content += party_n1_loop(d, "SF", ship_from)   # Ship From (Walmart-required)

            # --- Order level (one per PO) ---
            content.append(next_hl(str(shipment_hl), "O"))
            order_hl = hl
            content.append(seg(d, "PRF", order.po_number, "", "", "", order.po_date))

            # --- Item level (one per line shipped in this package) ---
            for pl in pkg.get("lines", []):
                line_num = str(pl.get("line_num", ""))
                qty = pl.get("qty_shipped", 0)
                li = line_by_num.get(line_num)
                content.append(next_hl(str(order_hl), "I"))
                lin = ["LIN", line_num]
                if li and li.buyer_part:
                    lin += ["IN", li.buyer_part]
                if li and li.vendor_part:
                    lin += ["VN", li.vendor_part]
                if li and li.upc:
                    lin += ["UP", li.upc]
                content.append(seg(d, *lin))
                content.append(seg(d, "SN1", line_num, _num(qty),
                                   (li.uom if li else "EA")))
                content.append(seg(d, "REF", "LI", line_num))

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


def _config_prefix() -> str:
    from ..config import config
    return config.GS1_COMPANY_PREFIX


def generate_856(order: Order, mappings: dict) -> str:
    return Generator856(order, mappings).to_x12()
