"""ISA/GS/ST envelope builder with stateful control numbers.

Wraps a list of transaction-set content segments in the full X12 envelope
(ISA/GS/ST ... SE/GE/IEA), padding ISA fields to exact spec widths and
auto-incrementing control numbers persisted to ``control_numbers.json``.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import List, Optional

ELEMENT_DELIM = "*"
SUBELEMENT_DELIM = ">"
SEGMENT_DELIM = "~"

_STATE_LOCK = threading.Lock()

# Functional group code per transaction set type.
_GS_CODE = {
    "997": "FA",
    "855": "PR",
    "856": "SH",
    "810": "IN",
}


def _state_path() -> str:
    return os.environ.get(
        "EDI_CONTROL_STATE",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "control_numbers.json"),
    )


def _load_state() -> dict:
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {"isa": 1, "gs": 1, "st": 1}


def _save_state(state: dict) -> None:
    try:
        with open(_state_path(), "w") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass


def next_control_numbers() -> dict:
    """Atomically increment and persist the ISA/GS/ST control counters."""
    with _STATE_LOCK:
        state = _load_state()
        nums = {k: int(state.get(k, 1)) for k in ("isa", "gs", "st")}
        for k in ("isa", "gs", "st"):
            state[k] = nums[k] + 1
        _save_state(state)
        return nums


def _pad(value: str, width: int) -> str:
    """Left-justify and pad/truncate a string field to an exact width."""
    value = (value or "")[:width]
    return value.ljust(width)


def _isa_id(value: str) -> str:
    return _pad(value, 15)


class EnvelopeBuilder:
    """Builds complete X12 documents around transaction-set content."""

    def __init__(
        self,
        sender_id: str,
        sender_qualifier: str,
        receiver_id: str,
        receiver_qualifier: str,
        element_delim: str = ELEMENT_DELIM,
        subelement_delim: str = SUBELEMENT_DELIM,
        segment_delim: str = SEGMENT_DELIM,
        version: str = "004010",
        usage_indicator: str = "P",
    ):
        self.sender_id = sender_id
        self.sender_qualifier = sender_qualifier
        self.receiver_id = receiver_id
        self.receiver_qualifier = receiver_qualifier
        self.element_delim = element_delim
        self.subelement_delim = subelement_delim
        self.segment_delim = segment_delim
        self.version = version
        self.usage_indicator = usage_indicator

    def build(
        self,
        st_code: str,
        content_segments: List[str],
        st_control: str,
        isa_control: Optional[int] = None,
        gs_control: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> str:
        """Wrap ``content_segments`` (everything between ST and SE) in an envelope.

        Control numbers are auto-allocated unless explicitly supplied (used
        by the 997 generator, which mirrors the inbound 850's numbers).
        """
        d = self.element_delim
        now = now or datetime.utcnow()

        if isa_control is None or gs_control is None:
            nums = next_control_numbers()
            if isa_control is None:
                isa_control = nums["isa"]
            if gs_control is None:
                gs_control = nums["gs"]

        isa_ctrl_str = str(int(isa_control)).zfill(9)
        gs_ctrl_str = str(int(gs_control))
        date_yymmdd = now.strftime("%y%m%d")
        date_ccyymmdd = now.strftime("%Y%m%d")
        time_hhmm = now.strftime("%H%M")

        # --- Transaction set: ST ... SE ---
        st_seg = d.join(["ST", st_code, str(st_control)])
        body = [st_seg] + list(content_segments)
        # SE count includes ST and SE themselves.
        se_count = len(body) + 1
        se_seg = d.join(["SE", str(se_count), str(st_control)])
        body.append(se_seg)

        # --- Functional group: GS ... GE ---
        gs_code = _GS_CODE.get(st_code, "PR")
        gs_seg = d.join([
            "GS", gs_code, self.sender_id, self.receiver_id,
            date_ccyymmdd, time_hhmm, gs_ctrl_str, "X", self.version,
        ])
        ge_seg = d.join(["GE", "1", gs_ctrl_str])

        # --- Interchange: ISA ... IEA (ISA is fixed-width, 106 chars) ---
        isa_fields = [
            "ISA",
            "00", _pad("", 10),                       # auth info
            "00", _pad("", 10),                       # security info
            _pad(self.sender_qualifier, 2), _isa_id(self.sender_id),
            _pad(self.receiver_qualifier, 2), _isa_id(self.receiver_id),
            date_yymmdd, time_hhmm,
            "U", self.version[:5].zfill(5) if self.version.isdigit() else "00401",
            isa_ctrl_str, "0", self.usage_indicator, self.subelement_delim,
        ]
        isa_seg = d.join(isa_fields)
        iea_seg = d.join(["IEA", "1", isa_ctrl_str])

        all_segments = [isa_seg, gs_seg] + body + [ge_seg, iea_seg]
        return self.segment_delim.join(all_segments) + self.segment_delim
