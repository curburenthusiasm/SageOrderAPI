"""workflow_engine/builder.py — Spec → WorkflowDef auto-generator.

Given a parsed spec dict and partner record, builds a complete WorkflowDef
that the executor can run immediately. The brain can then edit individual
steps at any time via the store's patch methods.

Default pipeline for an EDI X12 partner:
  1. validate    → check inbound 850 against spec rules
  2. generate    → 997 (functional ACK — always immediate)
  3. submit      → send 997
  4. enrich      → pull prices/freight from Sage SQL
  5. generate    → 855 (PO acknowledgment)
  6. submit      → send 855
  7. log         → "Waiting for ship confirmation"
  [8. generate   → 856 + 810 (triggered separately on ship event)]

For REST API partners the submit steps use connector="rest_api".
For Orderful partners the submit steps use connector="orderful".
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .models import WorkflowDef, WorkflowStep

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_from_spec(partner: dict, spec: dict) -> WorkflowDef:
    """Generate a WorkflowDef from a partner record and parsed spec.

    :param partner: Row from edi_partners (id, name, platform, isa_qualifier, ...)
    :param spec:    Parsed spec dict (from onboarding.py parser)
    :returns:       Ready-to-save WorkflowDef (not yet persisted)
    """
    platform = partner.get("platform", "orderful")
    connector = _connector_for_platform(platform)
    partner_qualifier = partner.get("isa_qualifier") or partner.get("name", "")
    doc_types = spec.get("doc_types") or spec.get("transaction_sets") or ["850"]
    has_856 = "856" in doc_types or True   # always offer 856/810
    needs_sage = _spec_needs_sage_enrich(spec)

    steps = _build_steps(
        connector=connector,
        partner=partner_qualifier,
        needs_sage=needs_sage,
        has_856=has_856,
        spec=spec,
    )

    wf = WorkflowDef(
        id=str(uuid.uuid4()),
        partner_id=partner["id"],
        name=f"{partner['name']} EDI Pipeline",
        doc_type="850",
        steps=steps,
        on_error={
            "type": "correct",
            "max_retries": 3,
        },
        metadata={
            "platform": platform,
            "spec_partner": spec.get("partner", ""),
            "spec_source": spec.get("source", ""),
            "auto_generated": True,
        },
    )
    log.info(f"Built workflow [{wf.name}] — {len(steps)} steps, connector={connector}")
    return wf


def build_ship_workflow(partner: dict) -> WorkflowDef:
    """Separate workflow triggered when a shipment is confirmed.

    Generates 856 (Ship Notice) and 810 (Invoice) and submits them.
    Triggered by POST /webhook/ship or manual /order/{po}/ship.
    """
    platform = partner.get("platform", "orderful")
    connector = _connector_for_platform(platform)
    partner_qualifier = partner.get("isa_qualifier") or partner.get("name", "")

    steps = [
        WorkflowStep(
            type="enrich",
            source="shipping_api",
            notes="Load tracking number from ShipStation",
        ),
        WorkflowStep(
            type="generate",
            docs=["856"],
            notes="Generate Ship Notice / ASN",
        ),
        WorkflowStep(
            type="submit",
            connector=connector,
            partner=partner_qualifier,
            docs=["856"],
        ),
        WorkflowStep(
            type="enrich",
            source="sage_sql",
            notes="Load invoice data from Sage",
        ),
        WorkflowStep(
            type="generate",
            docs=["810"],
            notes="Generate Invoice",
        ),
        WorkflowStep(
            type="submit",
            connector=connector,
            partner=partner_qualifier,
            docs=["810"],
        ),
        WorkflowStep(
            type="log",
            message="PO {po} fully processed — 856 + 810 sent to {partner}",
        ),
    ]

    return WorkflowDef(
        id=str(uuid.uuid4()),
        partner_id=partner["id"],
        name=f"{partner['name']} Ship + Invoice Pipeline",
        doc_type="ship_event",
        steps=steps,
        on_error={"type": "correct", "max_retries": 3},
        metadata={"auto_generated": True, "trigger": "ship_event"},
    )


# ---------------------------------------------------------------------------
# LLM-enhanced builder — generates partner-specific step config from spec
# ---------------------------------------------------------------------------

def build_from_spec_llm(partner: dict, spec: dict) -> WorkflowDef:
    """Like build_from_spec but uses OpenAI to generate smarter step config.

    Looks at the spec's field_map and validation_rules to:
    - Add transform steps for partner-specific field requirements
    - Add condition steps for split-line / partial-ship logic
    - Set per-step on_error handling based on spec criticality
    """
    import json, os

    try:
        import openai
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    except Exception:
        log.warning("OpenAI not available — falling back to standard builder")
        return build_from_spec(partner, spec)

    base_wf = build_from_spec(partner, spec)

    prompt = f"""You are an EDI integration engineer.

Partner: {partner.get('name')} (platform: {partner.get('platform')})
Spec field_map (first 2000 chars): {json.dumps(spec.get('field_map', {}))[:2000]}
Validation rules: {json.dumps(spec.get('validation_rules', []))[:1000]}
Doc types: {spec.get('doc_types', [])}

Current workflow steps (JSON):
{json.dumps([s.to_dict() for s in base_wf.steps], indent=2)}

Suggest improvements to the steps as a JSON array. You may:
- Add transform steps for required field overrides (e.g. specific ack codes, carrier codes)
- Add condition steps for split-shipment logic if the spec mentions it
- Add a webhook step if the spec mentions partner notifications
- Adjust on_error per step based on criticality

Return ONLY a valid JSON array of step objects. Keep the existing step IDs where possible.
If no improvements needed, return the original steps unchanged."""

    try:
        resp = client.chat.completions.create(
            model=os.getenv("EDI_PARSER_MODEL", "gpt-4o"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=3000,
        )
        content = resp.choices[0].message.content or "[]"
        steps_data = json.loads(content)
        base_wf.steps = [WorkflowStep.from_dict(s) for s in steps_data]
        base_wf.metadata["llm_enhanced"] = True
        log.info(f"LLM enhanced workflow [{base_wf.name}] to {len(base_wf.steps)} steps")
    except Exception as exc:
        log.warning(f"LLM workflow enhancement failed: {exc} — using base workflow")

    return base_wf


# ---------------------------------------------------------------------------
# Step builders
# ---------------------------------------------------------------------------

def _build_steps(
    connector: str,
    partner: str,
    needs_sage: bool,
    has_856: bool,
    spec: dict,
) -> list[WorkflowStep]:

    steps: list[WorkflowStep] = []

    # Step 1: Validate inbound 850
    steps.append(WorkflowStep(
        type="validate",
        spec_id=f"{partner.lower().replace(' ', '_')}-850",
        on_error={"type": "correct", "max_retries": 1},
        notes="Validate inbound 850 against partner companion guide",
    ))

    # Step 2: Generate + submit 997 (immediate ACK — always first)
    steps.append(WorkflowStep(
        type="generate",
        docs=["997"],
        notes="Functional Acknowledgment — sent immediately on receipt",
    ))
    steps.append(WorkflowStep(
        type="submit",
        connector=connector,
        partner=partner,
        docs=["997"],
        on_error={"type": "retry", "max_retries": 3},
        notes="Submit 997 to partner",
    ))

    # Step 3: Enrich from Sage SQL (prices, freight, inventory)
    if needs_sage:
        steps.append(WorkflowStep(
            type="enrich",
            source="sage_sql",
            on_error={"type": "skip"},   # non-fatal — use 850 prices as fallback
            notes="Load unit prices and freight from Sage 100",
        ))

    # Step 4: Apply any spec-mandated field overrides
    transform_mapping = _extract_required_overrides(spec)
    if transform_mapping:
        steps.append(WorkflowStep(
            type="transform",
            mapping=transform_mapping,
            notes="Apply partner-required field overrides from spec",
        ))

    # Step 5: Generate + submit 855 (PO Acknowledgment)
    steps.append(WorkflowStep(
        type="generate",
        docs=["855"],
        notes="Purchase Order Acknowledgment",
    ))
    steps.append(WorkflowStep(
        type="submit",
        connector=connector,
        partner=partner,
        docs=["855"],
        on_error={"type": "correct", "max_retries": 3},
        notes="Submit 855 PO Ack",
    ))

    # Step 6: Log — wait for ship event
    steps.append(WorkflowStep(
        type="log",
        message="PO {po} acknowledged — awaiting ship confirmation for 856/810",
        notes="856 and 810 are generated by the Ship Pipeline when shipment is confirmed",
    ))

    return steps


# ---------------------------------------------------------------------------
# Spec analysis helpers
# ---------------------------------------------------------------------------

def _connector_for_platform(platform: str) -> str:
    mapping = {
        "orderful": "orderful",
        "rest_api": "rest_api",
        "rithum": "rithum",
        "tray": "orderful",   # tray deprecated — fall back to orderful
        "pending": "orderful",
    }
    return mapping.get(platform, "orderful")


def _spec_needs_sage_enrich(spec: dict) -> bool:
    """Determine if this spec requires Sage SQL enrichment."""
    field_map = spec.get("field_map", {})
    sage_indicators = {"PO1_04", "PO1_05", "CTT"}  # price, unit, line count
    return bool(set(field_map.keys()) & sage_indicators) or bool(field_map)


def _extract_required_overrides(spec: dict) -> dict[str, Any]:
    """Pull mandatory field overrides from spec validation rules."""
    overrides: dict[str, Any] = {}
    for rule in spec.get("validation_rules", []):
        if rule.get("requirement") == "M" and rule.get("fixed_value"):
            field = rule.get("element_id", "")
            if field:
                overrides[field] = rule["fixed_value"]
    return overrides
