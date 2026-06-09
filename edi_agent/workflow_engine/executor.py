"""workflow_engine/executor.py — Step-by-step workflow runner.

Executes a WorkflowDef against a live EDI transaction (OrderSession).
Each step type maps to real pipeline operations already in Order-API.

Step types:
  validate   → check inbound 850 against partner spec rules
  generate   → produce outbound EDI docs (997/855/856/810)
  submit     → send via connector (orderful/rest_api/...)
  enrich     → pull live data from Sage SQL or shipping API
  correct    → AI-correct a previously failed document
  transform  → apply field overrides to mappings
  condition  → branch on runtime condition
  log        → emit event to work_events
  webhook    → POST payload to external URL
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import requests

from .models import (
    ErrorAction,
    ExecutionResult,
    StepResult,
    WorkflowDef,
    WorkflowStep,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


class WorkflowExecutor:
    """Runs a WorkflowDef against an OrderSession."""

    def __init__(self, app_context: dict):
        """
        app_context keys (injected from agent.py singletons):
          sessions      → dict[po_number, OrderSession]
          specs         → SpecStore
          lessons       → LessonsStore
          state         → OrderStateManager
          orderful      → OrderfulClient
          registry      → PartnerRegistry   (connector factory)
          emit          → callable(source, category, msg, **kw)
        """
        self.ctx = app_context

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, wf: WorkflowDef, po_number: str, x12_string: str = "") -> ExecutionResult:
        """Execute a workflow for a given PO. Returns full ExecutionResult."""
        result = ExecutionResult(
            workflow_id=wf.id,
            partner_id=wf.partner_id,
            po_number=po_number,
        )

        # Ensure there's an active OrderSession for this PO
        session = self._get_or_create_session(po_number, x12_string)
        if session is None:
            return result.finish(ok=False, error=f"No order session for PO {po_number}")

        log.info(f"[{wf.name}] Starting execution for PO {po_number} ({len(wf.steps)} steps)")

        for step in wf.steps:
            sr = self._run_step(step, session, wf)
            result.steps.append(sr)

            if not sr.ok:
                action = self._resolve_error_action(step, wf)
                if action == "stop":
                    result.finish(ok=False, error=f"Step [{step.type}:{step.id}] failed — stopped: {sr.error}")
                    break
                elif action == "alert":
                    self._alert(wf, step, sr)
                    result.finish(ok=False, error=f"Step [{step.type}:{step.id}] failed — alerted: {sr.error}")
                    break
                elif action == "skip":
                    log.warning(f"[{wf.name}] Step [{step.id}] skipped after error: {sr.error}")
                    continue
                # correct / retry handled inside _run_step already
                if not sr.ok:
                    result.finish(ok=False, error=sr.error)
                    break
        else:
            result.finish(ok=True)

        # Log to Supabase
        self._log_result(result)
        return result

    # ------------------------------------------------------------------
    # Step dispatcher
    # ------------------------------------------------------------------

    def _run_step(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> StepResult:
        t0 = int(time.time() * 1000)
        sr = StepResult(step_id=step.id, step_type=step.type)

        try:
            handler = getattr(self, f"_step_{step.type}", None)
            if handler is None:
                raise NotImplementedError(f"Unknown step type: {step.type}")
            output = handler(step, session, wf) or {}
            sr.ok = True
            sr.output = output

        except Exception as exc:
            sr.ok = False
            sr.error = str(exc)
            log.warning(f"[{wf.name}] Step [{step.type}:{step.id}] error: {exc}")

            # Attempt correction/retry if configured
            action = self._resolve_error_action(step, wf)
            if action in ("correct", "retry"):
                max_retries = (step.on_error or wf.on_error).get("max_retries", 2)
                for attempt in range(1, max_retries + 1):
                    log.info(f"[{wf.name}] Retry {attempt}/{max_retries} for step [{step.id}]")
                    try:
                        if action == "correct" and step.type == "submit":
                            self._step_correct_submit(step, session, wf, str(exc))
                        output = handler(step, session, wf) or {}
                        sr.ok = True
                        sr.output = output
                        sr.error = ""
                        sr.retries = attempt
                        break
                    except Exception as retry_exc:
                        sr.error = str(retry_exc)
                        log.warning(f"[{wf.name}] Retry {attempt} failed: {retry_exc}")

        sr.duration_ms = int(time.time() * 1000) - t0
        return sr

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    def _step_validate(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Validate the inbound 850 against the partner spec."""
        from ..edi_spec_validator import EDIValidator

        spec = self.ctx["specs"].find("850", session.order.partner_isa_id)
        if not spec:
            return {"validated": False, "reason": "No spec found — skipped validation"}

        validator = EDIValidator(spec.get("rules", {}))
        errors = validator.validate(session.order.raw_x12 if hasattr(session.order, "raw_x12") else "")
        if errors:
            raise ValueError(f"Validation failed: {'; '.join(errors[:3])}")
        return {"validated": True, "spec_id": spec.get("id", "")}

    def _step_generate(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Generate one or more outbound EDI documents."""
        from .. import spec_generator as sg

        generated = []
        for doc_type in step.docs:
            try:
                doc = _generate_doc(session, doc_type, self.ctx)
                session.documents[doc_type] = doc
                generated.append(doc_type)
                log.info(f"[{wf.name}] Generated {doc_type} for PO {session.order.po_number}")
            except Exception as exc:
                raise RuntimeError(f"Generate {doc_type} failed: {exc}") from exc

        return {"generated": generated}

    def _step_submit(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Submit generated documents via the partner's connector."""
        connector = self._get_connector(step, session)
        partner_id = step.partner or session.order.partner_isa_id or ""
        submitted = {}

        docs_to_submit = step.docs or list(session.documents.keys())
        for doc_type in docs_to_submit:
            if doc_type not in session.documents:
                log.warning(f"[{wf.name}] {doc_type} not generated yet — skipping submit")
                continue

            result = connector.submit(
                x12_string=session.documents[doc_type],
                trading_partner=partner_id,
                doc_type=doc_type,
            )
            if not result.ok:
                raise RuntimeError(f"Submit {doc_type} failed: {result.error}")

            session.submissions[doc_type] = result.transaction_id
            self.ctx["state"].mark_doc_sent(
                session.order.po_number, doc_type, result.transaction_id
            )
            submitted[doc_type] = result.transaction_id

        return {"submitted": submitted}

    def _step_enrich(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Enrich the order with live data from Sage SQL or shipping API."""
        enriched = {}

        if step.source in ("sage_sql", ""):
            try:
                from ..connectors.sql_reader import SQLReader
                reader = SQLReader()
                po = session.order.po_number
                sage_order = reader.get_order(po)
                if sage_order:
                    # Inject Sage prices into mappings
                    lines = reader.get_order_lines(po)
                    prices = {
                        ln.get("ItemCode", ""): float(ln.get("UnitPrice", 0))
                        for ln in lines if ln.get("ItemCode")
                    }
                    if prices:
                        session.mappings["sku_prices"] = {
                            **session.mappings.get("sku_prices", {}), **prices
                        }
                        enriched["prices_loaded"] = len(prices)

                    freight = reader.get_freight(po)
                    if freight:
                        session.mappings.setdefault("freight_amount", freight)
                        enriched["freight_loaded"] = True
            except Exception as exc:
                log.warning(f"[{wf.name}] Sage enrich skipped: {exc}")
                return {"enriched": False, "reason": str(exc)}

        elif step.source == "shipping_api":
            try:
                from ..connectors.shipping import ShippingConnector
                conn = ShippingConnector()
                tracking = conn.get_tracking(session.order.po_number)
                if tracking:
                    session.mappings["tracking_numbers"] = [tracking]
                    enriched["tracking_loaded"] = True
            except Exception as exc:
                log.warning(f"[{wf.name}] Shipping enrich skipped: {exc}")
                return {"enriched": False, "reason": str(exc)}

        return {"enriched": True, **enriched}

    def _step_correct(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """AI-correct a previously failed document."""
        doc_type = step.doc_type or "850"
        failure_msg = step.message or "Unknown failure"

        corrected = _correct_doc(session, doc_type, failure_msg, self.ctx)
        return {"corrected": doc_type, "length": len(corrected)}

    def _step_transform(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Apply field overrides from the step mapping to session.mappings."""
        applied = {}
        for field_name, value in step.mapping.items():
            session.mappings[field_name] = value
            applied[field_name] = value
        return {"applied": applied}

    def _step_condition(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Evaluate a condition and run then_steps or else_steps."""
        result = self._eval_condition(step.condition, session)
        branch_steps = step.then_steps if result else step.else_steps

        branch_results = []
        for substep in branch_steps:
            sr = self._run_step(substep, session, wf)
            branch_results.append(sr.step_id)
            if not sr.ok:
                raise RuntimeError(f"Condition branch step [{substep.id}] failed: {sr.error}")

        return {"condition": step.condition, "result": result, "ran_steps": branch_results}

    def _step_log(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """Emit a log event to work_events."""
        msg = step.message.format(
            po=session.order.po_number,
            partner=session.order.partner_isa_id or "",
            workflow=wf.name,
        )
        emit = self.ctx.get("emit")
        if emit:
            emit("workflow", "edi", msg, po=session.order.po_number, workflow_id=wf.id)
        log.info(f"[{wf.name}] LOG: {msg}")
        return {"logged": msg}

    def _step_webhook(self, step: WorkflowStep, session: Any, wf: WorkflowDef) -> dict:
        """POST to an external webhook URL."""
        if not step.url:
            raise ValueError("webhook step requires a url")

        payload: dict[str, Any] = {
            "workflow": wf.name,
            "po_number": session.order.po_number,
            "partner": session.order.partner_isa_id,
        }
        if step.include_order:
            payload["order"] = {
                "po_number": session.order.po_number,
                "po_date": session.order.po_date,
                "lines": len(session.order.lines),
            }

        try:
            resp = requests.post(step.url, json=payload, timeout=15)
            resp.raise_for_status()
            return {"posted": step.url, "status": resp.status_code}
        except requests.RequestException as exc:
            raise RuntimeError(f"Webhook POST failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Correction helper (called during retry)
    # ------------------------------------------------------------------

    def _step_correct_submit(self, step: WorkflowStep, session: Any, wf: WorkflowDef, error: str) -> None:
        """Before retrying a failed submit — AI-correct the document."""
        for doc_type in (step.docs or list(session.documents.keys())):
            try:
                _correct_doc(session, doc_type, error, self.ctx)
            except Exception as exc:
                log.warning(f"Pre-retry correction for {doc_type} failed: {exc}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_create_session(self, po_number: str, x12_string: str) -> Any | None:
        sessions = self.ctx.get("sessions", {})
        if po_number in sessions:
            return sessions[po_number]
        if x12_string:
            try:
                from ..core.parser import parse as parse_850, EDIParseError
                from .. import order_state
                order = parse_850(x12_string)
                session = order_state.OrderSession(order)
                sessions[po_number] = session
                return session
            except Exception as exc:
                log.error(f"Could not parse 850 for PO {po_number}: {exc}")
                return None
        return None

    def _get_connector(self, step: WorkflowStep, session: Any):
        registry = self.ctx.get("registry")
        if registry and session:
            # Try to look up connector from partner registry via ISA qualifier
            try:
                partner = registry.find_by_isa(session.order.partner_isa_id or "")
                if partner:
                    return registry.get_connector(partner["id"])
            except Exception:
                pass

        # Fall back to step-specified connector or default Orderful
        connector_name = step.connector or "orderful"
        if connector_name == "orderful":
            return self.ctx.get("orderful")
        elif connector_name == "rest_api":
            from ..connectors.rest_api import RestApiConnector
            # Load stored connector_config from registry if available
            cfg = {}
            if registry and session:
                try:
                    partner = registry.find_by_isa(session.order.partner_isa_id or "")
                    if partner:
                        cfg = partner.get("connector_config") or {}
                except Exception:
                    pass
            return RestApiConnector(connector_config=cfg)
        elif connector_name == "rithum":
            from ..connectors.rithum import RithumConnector
            cfg = {}
            if registry and session:
                try:
                    partner = registry.find_by_isa(session.order.partner_isa_id or "")
                    if partner:
                        cfg = partner.get("connector_config") or {}
                except Exception:
                    pass
            return RithumConnector(connector_config=cfg)
        return self.ctx.get("orderful")

    def _resolve_error_action(self, step: WorkflowStep, wf: WorkflowDef) -> ErrorAction:
        cfg = step.on_error or wf.on_error or {}
        return cfg.get("type", "correct")

    def _eval_condition(self, condition: str, session: Any) -> bool:
        """Evaluate a simple condition string against the order."""
        try:
            order = session.order
            context = {
                "order": order,
                "lines": order.lines,
                "mappings": session.mappings,
                "line_count": len(order.lines),
                "po_number": order.po_number,
            }
            return bool(eval(condition, {"__builtins__": {}}, context))  # noqa: S307
        except Exception as exc:
            log.warning(f"Condition eval failed [{condition!r}]: {exc}")
            return False

    def _alert(self, wf: WorkflowDef, step: WorkflowStep, sr: StepResult) -> None:
        emit = self.ctx.get("emit")
        if emit:
            emit(
                "workflow", "error",
                f"Workflow [{wf.name}] step [{step.type}:{step.id}] FAILED: {sr.error}",
                workflow_id=wf.id,
                outcome="error",
            )

    def _log_result(self, result: ExecutionResult) -> None:
        try:
            store = self.ctx.get("workflow_store")
            if store:
                store.log_execution({
                    "workflow_id": result.workflow_id,
                    "partner_id": result.partner_id,
                    "po_number": result.po_number,
                    "ok": result.ok,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                    "step_count": len(result.steps),
                    "steps_ok": sum(1 for s in result.steps if s.ok),
                })
        except Exception as exc:
            log.warning(f"Could not log execution result: {exc}")


# ---------------------------------------------------------------------------
# Private helpers (call into existing agent.py pipeline functions)
# ---------------------------------------------------------------------------

def _generate_doc(session: Any, doc_type: str, ctx: dict) -> str:
    """Call the spec_generator (or raw generators) to produce an EDI doc."""
    import edi_agent.spec_generator as sg
    from edi_agent import spec_store

    specs = ctx.get("specs")
    lessons_store = ctx.get("lessons")
    partner = session.order.partner_isa_id or ""

    if sg.available() and specs:
        spec = specs.find(doc_type, partner)
        if spec:
            lessons = lessons_store.lessons_for(partner, doc_type) if lessons_store else []
            doc, notes = sg.generate(session.order, session.mappings, doc_type, spec, lessons)
            session.spec_notes[doc_type] = notes
            session.documents[doc_type] = doc
            return doc

    # Fall back to deterministic generators
    from edi_agent.generators import gen_997, gen_855, gen_856, gen_810
    from edi_agent.core.envelope import EnvelopeBuilder
    env = EnvelopeBuilder(session.order)
    generators = {
        "997": gen_997.generate,
        "855": gen_855.generate,
        "856": gen_856.generate,
        "810": gen_810.generate,
    }
    gen = generators.get(doc_type)
    if not gen:
        raise ValueError(f"No generator for doc type: {doc_type}")
    doc = gen(session.order, session.mappings, env)
    session.documents[doc_type] = doc
    return doc


def _correct_doc(session: Any, doc_type: str, failure_message: str, ctx: dict) -> str:
    """AI-correct a failed document using spec + lessons."""
    import edi_agent.spec_generator as sg
    from edi_agent import spec_store

    specs = ctx.get("specs")
    lessons_store = ctx.get("lessons")
    partner = session.order.partner_isa_id or ""

    if not sg.available():
        raise RuntimeError("LLM not available for correction (no ANTHROPIC_API_KEY)")

    spec = specs.find(doc_type, partner) if specs else None
    if not spec:
        raise RuntimeError(f"No {doc_type} spec found for {partner} — cannot correct")

    if lessons_store:
        lessons_store.record(partner, doc_type, failure_message, source="rejection")
    lessons = lessons_store.lessons_for(partner, doc_type) if lessons_store else []

    current = session.documents.get(doc_type, "")
    corrected, notes = sg.correct(session.order, session.mappings, doc_type, spec, lessons, current, failure_message)
    session.documents[doc_type] = corrected
    session.spec_notes[doc_type] = notes
    return corrected
