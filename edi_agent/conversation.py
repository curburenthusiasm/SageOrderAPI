"""Conversational LLM layer — talk to the EDI agent in natural language.

This wraps Claude (Anthropic API) with a tool surface that drives the EDI
pipeline: parse an inbound 850, set human field mappings, generate/validate/
submit the 997/855/856/810, pull ship data, etc. The actual work is done by
handler callables supplied by ``agent.py`` (so this module has no dependency
on the FastAPI app or the in-memory store — it just orchestrates the model).

If the ``anthropic`` SDK isn't installed or ``ANTHROPIC_API_KEY`` isn't set,
:func:`llm_available` returns False and the caller falls back to the
deterministic intent parser. This keeps document production working with no
external dependency, while the LLM path is available the moment a key exists.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import anthropic
except Exception:  # noqa: BLE001
    anthropic = None

MODEL = os.environ.get("EDI_AGENT_MODEL", "claude-opus-4-8")


def llm_available() -> bool:
    """True when the Claude API can be used for the conversational layer."""
    return (
        anthropic is not None
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
        and os.environ.get("EDI_AGENT_LLM", "1") != "0"
    )


# Frozen system prompt — kept byte-stable so it caches across requests.
SYSTEM_PROMPT = """\
You are the EDI agent for a vendor (Jeffco Fibres) trading via the Orderful API.
You help Robert process inbound X12 850 Purchase Orders and produce the four \
outbound documents: 997 (Functional Acknowledgment), 855 (PO Acknowledgment), \
856 (Ship Notice / ASN), and 810 (Invoice).

Workflow you follow:
1. When the user pastes an X12 850 (it starts with "ISA"), call parse_inbound_850. \
This parses the PO and immediately generates and submits the 997. Summarize what \
you parsed (PO number, ship-to, line items, quantities).
2. Collect the human-provided field mappings and apply them with update_mappings: \
unit prices per SKU (sku_prices, keyed by vendor part / buyer part / UPC), \
acknowledgment_code (AC=accepted, IA=item accepted, IQ=qty change, IP=price change), \
carrier_code (e.g. UPSN, FDXG), ship_method (e.g. GROUND), payment terms.
3. When asked, generate the 855 with generate_document. Always validate before submit \
(the tool does this). Only submit when the user asks to submit/send.
4. For shipping: set ship_date (YYYYMMDD), ship_time (HHMM), carrier, and \
tracking_numbers via set_ship_data, then generate the 856 and 810. They can be \
generated together once ship data is present.

Rules and formats:
- All dates are YYYYMMDD; all times are HHMM.
- Never invent prices, tracking numbers, or carriers — ask the user, or use values \
already on the order/mappings.
- Trading-partner identifiers come from the parsed 850 or config; never hardcode them.
- After generating a document, tell the user it's ready (it appears in the viewer); \
mention the transaction id if it was submitted.
- Be concise. Confirm what you did and what you need next. If a request is ambiguous \
(e.g. a price without a SKU), ask one focused question.
"""


# Tool schemas. Stable order so the prompt prefix caches.
TOOLS = [
    {
        "name": "parse_inbound_850",
        "description": "Parse a raw X12 850 Purchase Order, then immediately generate "
        "and submit the 997 acknowledgment. Use when the user provides EDI starting "
        "with 'ISA'. Returns the parsed order and status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "edi": {"type": "string", "description": "The raw X12 850 document text."}
            },
            "required": ["edi"],
        },
    },
    {
        "name": "get_order",
        "description": "Return the parsed order, current mappings, and document status "
        "for a PO number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "po_number": {"type": "string"},
            },
            "required": ["po_number"],
        },
    },
    {
        "name": "update_mappings",
        "description": "Set human-provided field mappings on an order (prices, carrier, "
        "ship method, acknowledgment code, payment terms, etc.). Merges into existing "
        "mappings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "po_number": {"type": "string"},
                "mappings": {
                    "type": "object",
                    "description": "Subset of mapping fields to set. Keys include: "
                    "sku_prices (object: SKU->price), default_unit_price (number), "
                    "acknowledgment_code (AC|IA|IQ|IP), carrier_code (string), "
                    "ship_method (string), payment_terms_days (integer), "
                    "invoice_number (string), invoice_date (YYYYMMDD).",
                },
            },
            "required": ["po_number", "mappings"],
        },
    },
    {
        "name": "generate_document",
        "description": "Generate and validate an outbound document (997, 855, 856, or "
        "810) for a PO. Optionally submit it to Orderful. Returns status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "po_number": {"type": "string"},
                "doc_type": {"type": "string", "enum": ["997", "855", "856", "810"]},
                "submit": {"type": "boolean", "description": "Also submit to Orderful."},
            },
            "required": ["po_number", "doc_type"],
        },
    },
    {
        "name": "set_ship_data",
        "description": "Set ship date/time, carrier, ship method, and tracking numbers "
        "for a PO. Required before the 856 and 810 can reflect shipment details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "po_number": {"type": "string"},
                "ship_date": {"type": "string", "description": "YYYYMMDD"},
                "ship_time": {"type": "string", "description": "HHMM"},
                "carrier_code": {"type": "string"},
                "ship_method": {"type": "string"},
                "tracking_numbers": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["po_number"],
        },
    },
    {
        "name": "get_status",
        "description": "Return document/submission status for a PO.",
        "input_schema": {
            "type": "object",
            "properties": {"po_number": {"type": "string"}},
            "required": ["po_number"],
        },
    },
]


class Conversation:
    """A single multi-turn conversation thread with the EDI agent."""

    def __init__(self, execute: Callable[[str, dict], dict], max_tool_iterations: int = 12):
        self.execute = execute
        self.max_tool_iterations = max_tool_iterations
        self.messages: list = []
        self._client = anthropic.Anthropic() if llm_available() else None

    def send(self, user_text: str) -> str:
        """Send a user message; run the tool loop; return the agent's reply text."""
        if self._client is None:
            raise RuntimeError("LLM not available")

        self.messages.append({"role": "user", "content": user_text})

        for _ in range(self.max_tool_iterations):
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=self.messages,
            )
            # Preserve full content (incl. thinking + tool_use blocks) in history.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text").strip()

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    out = self.execute(block.name, dict(block.input))
                    is_error = isinstance(out, dict) and "error" in out
                except Exception as exc:  # noqa: BLE001 - report back to the model
                    out = {"error": str(exc)}
                    is_error = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str),
                    "is_error": is_error,
                })
            self.messages.append({"role": "user", "content": tool_results})

        return ("I've run several steps but didn't finish cleanly — please check the "
                "document viewer and let me know how to proceed.")
