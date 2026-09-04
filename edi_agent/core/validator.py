"""Lightweight structural validation of generated X12 documents.

Every generated document is run through :func:`validate_document` before it
is considered submittable. This checks envelope integrity, segment counts,
and control-number consistency -- the failure modes most likely to get a
document rejected by a trading partner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


class EDIValidationError(Exception):
    """Raised when a generated document fails structural validation."""


@dataclass
class ValidationResult:
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def raise_if_failed(self) -> "ValidationResult":
        if not self.ok:
            raise EDIValidationError("; ".join(self.errors))
        return self


def validate_document(x12: str, element_delim: str = "*", segment_delim: str = "~") -> ValidationResult:
    """Validate envelope structure and segment counts of an X12 string."""
    result = ValidationResult()

    segments = [s.strip() for s in x12.split(segment_delim) if s.strip()]
    if not segments:
        result.add_error("Document is empty")
        return result

    tags = [s.split(element_delim)[0].upper() for s in segments]

    # Envelope ordering.
    if tags[0] != "ISA":
        result.add_error("Document must start with ISA")
    if tags[-1] != "IEA":
        result.add_error("Document must end with IEA")
    if "GS" not in tags or "GE" not in tags:
        result.add_error("Missing GS/GE functional group envelope")
    if "ST" not in tags or "SE" not in tags:
        result.add_error("Missing ST/SE transaction set envelope")

    # ISA fixed width.
    if tags[0] == "ISA" and len(segments[0]) != 105:
        result.add_warning(
            f"ISA segment is {len(segments[0])} chars (expected 105 before terminator)"
        )

    def field_at(seg_tag: str, idx: int) -> str:
        for s in segments:
            parts = s.split(element_delim)
            if parts[0].upper() == seg_tag and len(parts) > idx:
                return parts[idx].strip()
        return ""

    # SE segment count must equal the number of segments from ST..SE inclusive.
    try:
        st_idx = tags.index("ST")
        se_idx = tags.index("SE")
        actual = se_idx - st_idx + 1
        declared = int(field_at("SE", 1) or -1)
        if declared != actual:
            result.add_error(
                f"SE segment count mismatch: declared {declared}, actual {actual}"
            )
    except (ValueError, IndexError):
        result.add_error("Could not locate ST/SE for segment count check")

    # Control-number consistency: ISA13==IEA02, GS06==GE02, ST02==SE02.
    if field_at("ISA", 13) and field_at("IEA", 2):
        if field_at("ISA", 13).lstrip("0") != field_at("IEA", 2).lstrip("0"):
            result.add_error("ISA13 / IEA02 control number mismatch")
    if field_at("GS", 6) and field_at("GE", 2):
        if field_at("GS", 6) != field_at("GE", 2):
            result.add_error("GS06 / GE02 control number mismatch")
    if field_at("ST", 2) and field_at("SE", 2):
        if field_at("ST", 2) != field_at("SE", 2):
            result.add_error("ST02 / SE02 control number mismatch")

    return result
