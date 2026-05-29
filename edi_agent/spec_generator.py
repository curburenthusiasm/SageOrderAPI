"""Spec-guided generation — conform a baseline X12 doc to a partner's spec.

Our generators produce a structurally valid baseline X12. This module hands
that baseline, plus the parsed order and the partner's companion-guide PDF, to
Claude and asks it to adjust the document so it conforms to the partner spec
(required segments, qualifiers, partner-specific values). The result is then
re-validated by our structural validator before it's accepted.

Safety model: the deterministic baseline is always valid; the LLM only refines
it; the validator is the gate. If no API key is set, the PDF can't be read, or
the tailored output fails validation, we fall back to the baseline. When a
partner rejects a file, the same spec context can be used with the rejection
message to create a corrected version.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional, Tuple

from .config import config
from .core.validator import validate_document

logger = logging.getLogger(__name__)

try:
    import anthropic
except Exception:  # noqa: BLE001
    anthropic = None

MODEL = os.environ.get("EDI_AGENT_MODEL", "claude-opus-4-8")

SYSTEM_PROMPT = """\
You are an X12 EDI expert. You are given a structurally valid baseline X12 \
document and the trading partner's companion guide (a PDF). Adjust the baseline \
so it conforms to the partner's guide: add/repair required segments, fix \
qualifiers and codes, and set partner-specific values that the guide mandates.

Hard rules:
- Output ONLY the raw X12 document, nothing else. It must start with "ISA" and \
end with "~". No code fences, no commentary.
- Keep the same delimiters as the baseline (element "*", sub-element ">", \
segment "~").
- Keep the ISA, GS, and ST control numbers identical to the baseline.
- Recompute the SE segment count, the GE/IEA counts, and any CTT counts so they \
are correct after your edits.
- Never invent data the order doesn't contain (real prices, tracking, parties). \
Only apply structure, qualifiers, and constant values the guide specifies.
- If the guide doesn't require a change, return the baseline unchanged.
"""

REPAIR_SYSTEM_PROMPT = """\
You are an X12 EDI expert correcting a rejected outbound X12 document. You are \
given the rejected X12, the trading partner's failure message, the parsed order, \
and the trading partner's companion guide (a PDF). Use the failure message to \
identify exactly what the partner rejected, then use the companion guide as the \
source of truth for the corrected structure, qualifiers, codes, and constants.

Hard rules:
- Output ONLY the corrected raw X12 document, nothing else. It must start with \
"ISA" and end with "~". No code fences, no commentary.
- Keep the same delimiters as the rejected file (element "*", sub-element ">", \
segment "~").
- Keep the ISA, GS, and ST control numbers identical to the rejected file unless \
the failure message specifically says a control number is invalid.
- Recompute the SE segment count, the GE/IEA counts, and any CTT counts so they \
are correct after your edits.
- Preserve business data from the rejected file and parsed order: PO number, \
quantities, item identifiers, prices, parties, tracking, and dates. Do not invent \
missing business data.
- Fix only what the rejection message and companion guide justify.
"""


def available() -> bool:
    return (
        config.SPEC_GUIDED
        and anthropic is not None
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )


def _extract_x12(text: str) -> str:
    """Pull the X12 payload out of the model's reply (defensive)."""
    text = text.strip()
    if "```" in text:  # strip accidental code fences
        parts = text.split("```")
        text = max(parts, key=len)
    start = text.find("ISA")
    end = text.rfind("~")
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start:end + 1]


def _fix_counts(x12: str, element_delim: str = "*", segment_delim: str = "~") -> str:
    """Repair envelope math + counts after LLM edits, before validation.

    The LLM conforms segments to the partner spec but routinely (a) miscounts
    SE/GE/IEA/CTT and (b) lets envelope control numbers drift out of sync
    (e.g. it rewrites ISA13 but not IEA02). Those are the two things that make
    an otherwise-correct tailored doc fail validation and fall back to the
    baseline. We fix them deterministically and let the validator be the final
    gate on everything else.
    """
    clean = [s.strip() for s in x12.split(segment_delim) if s.strip()]

    def tag(s: str) -> str:
        return s.split(element_delim)[0].upper() if s else ""

    tags = [tag(s) for s in clean]

    def get(seg_tag: str, idx: int):
        if seg_tag in tags:
            parts = clean[tags.index(seg_tag)].split(element_delim)
            if len(parts) > idx:
                return parts[idx]
        return None

    def put(seg_tag: str, idx: int, value: str):
        if seg_tag not in tags:
            return
        i = tags.index(seg_tag)
        parts = clean[i].split(element_delim)
        while len(parts) <= idx:
            parts.append("")
        parts[idx] = value
        clean[i] = element_delim.join(parts)

    # --- Segment counts ---
    # SE = number of segments from ST..SE inclusive.
    try:
        st_i, se_i = tags.index("ST"), tags.index("SE")
        put("SE", 1, str(se_i - st_i + 1))
    except (ValueError, IndexError):
        pass
    put("GE", 1, str(tags.count("ST")))    # GE = # of ST in the group
    put("IEA", 1, str(tags.count("GS")))   # IEA = # of GS in the interchange

    # CTT = total HL segments (856) else the number of line items (855 PO1 / 810 IT1).
    if "CTT" in tags:
        hl = tags.count("HL")
        ctt01 = hl if hl else (tags.count("PO1") + tags.count("IT1"))
        put("CTT", 1, str(ctt01))

    # --- Envelope control-number consistency (the LLM often drifts these) ---
    isa13, gs06, st02 = get("ISA", 13), get("GS", 6), get("ST", 2)
    if isa13 is not None:
        put("IEA", 2, isa13)
    if gs06 is not None:
        put("GE", 2, gs06)
    if st02 is not None:
        put("SE", 2, st02)

    return (segment_delim + "\n").join(clean) + segment_delim


def _read_pdf_b64(spec_path: str) -> tuple[Optional[str], Optional[str]]:
    if not spec_path or not os.path.exists(spec_path):
        return None, "spec file not found"
    try:
        with open(spec_path, "rb") as fh:
            return base64.standard_b64encode(fh.read()).decode(), None
    except OSError as exc:
        return None, f"could not read spec: {exc}"


def _message_with_spec(system_prompt: str, instruction: str, pdf_b64: str):
    client = anthropic.Anthropic()
    return client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": pdf_b64}},
                {"type": "text", "text": instruction},
            ],
        }],
    )


def tailor_with_spec(doc_type: str, order, baseline_x12: str,
                     spec_path: str) -> Tuple[str, bool, str]:
    """Return (x12, used_spec, note).

    Falls back to ``baseline_x12`` (used_spec=False) on any problem.
    """
    if not available():
        return baseline_x12, False, "spec-guided generation unavailable (no API key)"
    pdf_b64, error = _read_pdf_b64(spec_path)
    if error:
        return baseline_x12, False, error

    instruction = (
        f"Document type: {doc_type}.\n"
        f"Parsed order (JSON):\n{order.to_dict()}\n\n"
        f"Baseline X12 to conform to the attached companion guide:\n{baseline_x12}"
    )

    try:
        resp = _message_with_spec(SYSTEM_PROMPT, instruction, pdf_b64)
    except Exception as exc:  # noqa: BLE001 - any API/PDF issue -> fall back
        logger.warning("Spec-guided generation failed for %s: %s", doc_type, exc)
        return baseline_x12, False, f"spec pass errored: {exc}"

    text = "".join(b.text for b in resp.content if b.type == "text")
    tailored = _extract_x12(text)
    if not tailored:
        logger.warning("Spec-guided %s: model returned no X12. Raw reply (first 500): %.500s",
                       doc_type, text)
        return baseline_x12, False, "model did not return an X12 document"

    # Auto-fix segment counts before validating -- the LLM may add/remove
    # segments correctly but miscount SE/GE/IEA.
    tailored = _fix_counts(tailored)

    result = validate_document(tailored)
    if not result.ok:
        logger.warning("Tailored %s still invalid after count fix, using baseline. "
                       "Errors: %s\nTailored doc (first 800):\n%.800s",
                       doc_type, result.errors, tailored)
        return baseline_x12, False, f"tailored output invalid: {'; '.join(result.errors)}"

    return tailored, True, "conformed to partner spec"


def repair_with_failure(doc_type: str, order, failed_x12: str,
                        failure_message: str, spec_path: str) -> Tuple[str, bool, str]:
    """Return (x12, used_spec, note) corrected from a partner failure message.

    Falls back to ``failed_x12`` (used_spec=False) on any problem. The caller can
    decide whether to surface the note or keep the original file.
    """
    failure_message = (failure_message or "").strip()
    if not failure_message:
        return failed_x12, False, "failure message required"
    if not available():
        return failed_x12, False, "spec-guided correction unavailable (no API key)"
    pdf_b64, error = _read_pdf_b64(spec_path)
    if error:
        return failed_x12, False, error

    instruction = (
        f"Document type: {doc_type}.\n"
        f"Partner failure message:\n{failure_message}\n\n"
        + (f"Parsed order (JSON):\n{order.to_dict()}\n\n" if order is not None else "")
        + f"Rejected X12 to correct against the attached companion guide:\n{failed_x12}"
    )

    try:
        resp = _message_with_spec(REPAIR_SYSTEM_PROMPT, instruction, pdf_b64)
    except Exception as exc:  # noqa: BLE001 - any API/PDF issue -> fall back
        logger.warning("Spec-guided correction failed for %s: %s", doc_type, exc)
        return failed_x12, False, f"correction pass errored: {exc}"

    text = "".join(b.text for b in resp.content if b.type == "text")
    repaired = _extract_x12(text)
    if not repaired:
        logger.warning("Spec-guided repair %s: model returned no X12. Raw reply (first 500): %.500s",
                       doc_type, text)
        return failed_x12, False, "model did not return an X12 document"

    repaired = _fix_counts(repaired)

    result = validate_document(repaired)
    if not result.ok:
        logger.warning("Corrected %s still invalid after count fix, keeping original. "
                       "Errors: %s\nCorrected doc (first 800):\n%.800s",
                       doc_type, result.errors, repaired)
        return failed_x12, False, f"corrected output invalid: {'; '.join(result.errors)}"

    return repaired, True, "corrected from failure message using partner spec"
