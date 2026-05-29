"""997 -- Functional Acknowledgment.

Sent immediately on 850 receipt, before any other processing. Control
numbers in the ISA/GS mirror the inbound 850's control numbers so the
trading partner can correlate the ack to what they sent.
"""
from __future__ import annotations

from ..core.models import Order
from .base import builder_for, seg


class Generator997:
    def __init__(self, order: Order, accepted: bool = True, error_code: str = ""):
        self.order = order
        self.accepted = accepted
        self.error_code = error_code  # AK9/AK5 error code when rejected

    def _content(self, d: str) -> list[str]:
        order = self.order
        ak5_status = "A" if self.accepted else "E"
        ak9_status = "A" if self.accepted else "R"

        content = [
            # AK1: functional group response -- "PO" group, mirror inbound GS control
            seg(d, "AK1", "PO", order.gs_control or "1"),
            # AK2: transaction set response -- the 850 and its control number
            seg(d, "AK2", "850", order.st_control or "0001"),
        ]
        # AK3/AK4 error detail would go here on rejection (omitted segment-level
        # detail; AK5 carries the transaction-set verdict).
        if self.accepted:
            content.append(seg(d, "AK5", ak5_status))
        else:
            content.append(seg(d, "AK5", ak5_status, self.error_code or "5"))
        # AK9: functional group trailer -- status, #included, #received, #accepted
        accepted_count = "1" if self.accepted else "0"
        content.append(seg(d, "AK9", ak9_status, "1", "1", accepted_count))
        return content

    def to_x12(self) -> str:
        b = builder_for(self.order)
        d = b.element_delim
        content = self._content(d)
        # Mirror inbound interchange/group control numbers.
        isa_ctrl = int(self.order.isa_control) if (self.order.isa_control or "").strip().isdigit() else None
        gs_ctrl = int(self.order.gs_control) if (self.order.gs_control or "").strip().isdigit() else None
        return b.build(
            st_code="997",
            content_segments=content,
            st_control=(self.order.st_control or "0001"),
            isa_control=isa_ctrl,
            gs_control=gs_ctrl,
        )


def generate_997(order: Order, accepted: bool = True, error_code: str = "") -> str:
    return Generator997(order, accepted=accepted, error_code=error_code).to_x12()
