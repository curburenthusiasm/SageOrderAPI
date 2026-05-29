"""810 -- Invoice.

Follows the Phase 2 segment spec: BIG / REF / N1 loops (BT, ST, VN) / DTM /
IT1 lines / TDS (integer cents) / CAD / ISS / optional SAC freight / CTT.

Fires after the 856, or manually via /order/{po}/invoice. Line quantities are
the shipped quantities when ship packages are present, else the acknowledged
quantities; lines with qty 0 are omitted.
"""
from __future__ import annotations

from ..core.models import Order
from ..mappings import price_for
from .base import builder_for, party_n1_loop, seg, today


class Generator810:
    def __init__(self, order: Order, mappings: dict):
        self.order = order
        self.mappings = mappings

    def _shipped_by_line(self) -> dict:
        """Sum shipped qty per line across packages (empty if none provided)."""
        totals: dict = {}
        for pkg in self.mappings.get("packages") or []:
            for pl in pkg.get("lines", []):
                ln = str(pl.get("line_num", ""))
                totals[ln] = totals.get(ln, 0) + float(pl.get("qty_shipped", 0) or 0)
        return totals

    def _content(self, d: str) -> list[str]:
        order = self.order
        m = self.mappings
        invoice_date = m.get("invoice_date") or today()
        invoice_number = m.get("invoice_number") or f"INV-{order.po_number}-{invoice_date}"
        ship_date = m.get("ship_date") or order.requested_ship_date
        bol = m.get("bill_of_lading", "")
        contract = m.get("contract_number", "")
        shipped = self._shipped_by_line()

        content = [
            seg(d, "BIG", invoice_date, invoice_number, order.po_date, order.po_number),
        ]
        if bol:
            content.append(seg(d, "REF", "BM", bol))
        if contract:
            content.append(seg(d, "REF", "CO", contract))

        content += party_n1_loop(d, "BT", order.bill_to)
        content += party_n1_loop(d, "ST", order.ship_to)
        content += party_n1_loop(d, "VN", order.vendor)

        content.append(seg(d, "DTM", "003", invoice_date))
        if ship_date:
            content.append(seg(d, "DTM", "011", ship_date))

        total_cents = 0
        line_count = 0
        for line in order.lines:
            qty = shipped.get(line.line_num)
            if qty is None:
                qty = line.qty_acknowledged or line.qty_ordered
            if not qty:  # skip qty 0 lines
                continue
            line_count += 1
            price = price_for(m, line)
            total_cents += int(round(price * qty * 100))
            it1 = ["IT1", line.line_num, _num(qty), line.uom, _money(price), ""]
            if line.buyer_part:
                it1 += ["IN", line.buyer_part]
            if line.vendor_part:
                it1 += ["VN", line.vendor_part]
            if line.upc:
                it1 += ["UP", line.upc]
            content.append(seg(d, *it1))
            if line.description:
                content.append(seg(d, "PID", "F", "", "", "", line.description))
            content.append(seg(d, "REF", "LI", line.line_num))

        content.append(seg(d, "TDS", str(total_cents)))

        # CAD (carrier detail) mirrors the 856 TD5.
        carrier = m.get("carrier_code", "")
        tracking_list = m.get("tracking_numbers") or []
        tracking = tracking_list[0] if tracking_list else ""
        if carrier or tracking:
            content.append(seg(d, "CAD", "", "", "", carrier, "", tracking))

        # ISS: total package count.
        packages = m.get("packages") or []
        total_pkgs = len(packages) or len(tracking_list)
        if total_pkgs:
            content.append(seg(d, "ISS", str(total_pkgs), "CT"))

        # SAC: freight/charges, only if a freight amount is present.
        freight_cents = _freight_cents(m)
        if freight_cents > 0:
            content.append(seg(d, "SAC", "C", "D240", "", "", str(freight_cents)))

        content.append(seg(d, "CTT", str(line_count)))
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


def _freight_cents(m: dict) -> int:
    if m.get("freight_cents") is not None:
        return int(m["freight_cents"])
    if m.get("freight_amount") is not None:
        return int(round(float(m["freight_amount"]) * 100))
    return 0


def generate_810(order: Order, mappings: dict) -> str:
    return Generator810(order, mappings).to_x12()
