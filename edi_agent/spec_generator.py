"""Spec-guided generation — conform a baseline X12 doc to a partner's spec.

Our generators produce a structurally valid baseline X12. This module hands
that baseline, plus the parsed order and the partner's companion-guide PDF, to
Claude and asks it to adjust the document so it conforms to the partner spec
(required segments, qualifiers, partner-specific values). The result is then
re-validated by our structural validator before it's accepted.

Safety model: the deterministic baseline is always valid; the LLM only refines
it; the validator is the gate. If no API key is set, the PDF can't be read, or
the tailored output fails validation, we fall back to the baseline.
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


def tailor_with_spec(doc_type: str, order, baseline_x12: str,
                     spec_path: str) -> Tuple[str, bool, str]:
    """Return (x12, used_spec, note).

    Falls back to ``baseline_x12`` (used_spec=False) on any problem.
    """
    if not available():
        return baseline_x12, False, "spec-guided generation unavailable (no API key)"
    if not spec_path or not os.path.exists(spec_path):
        return baseline_x12, False, "spec file not found"

    try:
        with open(spec_path, "rb") as fh:
            pdf_b64 = base64.standard_b64encode(fh.read()).decode()
    except OSError as exc:
        return baseline_x12, False, f"could not read spec: {exc}"

    instruction = (
        f"Document type: {doc_type}.\n"
        f"Parsed order (JSON):\n{order.to_dict()}\n\n"
        f"Baseline X12 to conform to the attached companion guide:\n{baseline_x12}"
    )

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SYSTEM_PROMPT,
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
    except Exception as exc:  # noqa: BLE001 - any API/PDF issue -> fall back
        logger.warning("Spec-guided generation failed for %s: %s", doc_type, exc)
        return baseline_x12, False, f"spec pass errored: {exc}"

    text = "".join(b.text for b in resp.content if b.type == "text")
    tailored = _extract_x12(text)
    if not tailored:
        return baseline_x12, False, "model did not return an X12 document"

    result = validate_document(tailored)
    if not result.ok:
        logger.info("Tailored %s failed validation, using baseline: %s",
                    doc_type, result.errors)
        return baseline_x12, False, f"tailored output invalid: {'; '.join(result.errors)}"

    return tailored, True, "conformed to partner spec"
