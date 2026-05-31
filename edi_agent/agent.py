"""EDI Agent orchestrator -- FastAPI app + conversational brain.

Wires the parser, generators, validator, and Orderful connector together
and exposes them two ways:

  * REST endpoints from the brief (``/850/inbound``, ``/order/{po}`` ...)
  * a chat endpoint (``/chat``) that lets Robert drive the whole pipeline in
    plain language from the web UI.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import conversation, order_state, spec_generator, spec_store
from . import agent_events, judge as judge_mod
from .config import config
from .connectors.orderful import OrderfulClient, OrderfulError
from .connectors.roi_insynch import RoiInsynchClient, RoiInsynchError
from .connectors.shipping import ShippingConnector, ShippingError
from .connectors.sql_reader import SQLReader
from .core.models import Order, order_from_storage, order_to_storage
from .learning import LessonStore
from .core.parser import EDIParseError, parse
from .core.validator import validate_document
from .generators.gen_810 import generate_810
from .generators.gen_855 import generate_855
from .generators.gen_856 import generate_856
from .generators.gen_997 import generate_997
from .mappings import default_mappings, merge_mappings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# In-memory state. One process; protected by a lock. Persist to a DB later.
# --------------------------------------------------------------------------


class OrderSession:
    """Everything the agent knows about one purchase order."""

    def __init__(self, order: Order):
        self.order = order
        self.mappings = default_mappings()
        self.mappings["trading_partner"] = order.partner_isa_id
        self.documents: Dict[str, str] = {}       # doc_type -> x12 string
        self.submissions: Dict[str, str] = {}      # doc_type -> transaction id
        self.spec_notes: Dict[str, str] = {}       # doc_type -> spec-pass note
        self.sage_order_no: Optional[str] = None    # Sage SO# after ROI import
        self.parse_ok = True
        self.parse_error = ""

    def status(self) -> dict:
        return {
            "po_number": self.order.po_number,
            "parse_ok": self.parse_ok,
            "documents_generated": sorted(self.documents.keys()),
            "submissions": dict(self.submissions),
            "spec_notes": dict(self.spec_notes),
            "sage_order_no": self.sage_order_no,
            "ready_to_ship": bool(self.mappings.get("ship_date")
                                  and self.mappings.get("tracking_numbers")),
        }

    def to_storage(self) -> dict:
        """Full snapshot for persistence (survives restarts)."""
        return {
            "order": order_to_storage(self.order),
            "mappings": self.mappings,
            "documents": self.documents,
            "submissions": self.submissions,
            "spec_notes": self.spec_notes,
            "sage_order_no": self.sage_order_no,
            "parse_ok": self.parse_ok,
        }

    @staticmethod
    def from_storage(data: dict) -> "OrderSession":
        s = OrderSession(order_from_storage(data["order"]))
        s.mappings = data.get("mappings") or s.mappings
        s.documents = data.get("documents") or {}
        s.submissions = data.get("submissions") or {}
        s.spec_notes = data.get("spec_notes") or {}
        s.sage_order_no = data.get("sage_order_no")
        s.parse_ok = data.get("parse_ok", True)
        return s


_LOCK = threading.Lock()
_SESSIONS: Dict[str, OrderSession] = {}   # po_number -> session
_orderful = OrderfulClient()
_shipping = ShippingConnector()
_sql = SQLReader()
_state = order_state.OrderStateStore()
_specs = spec_store.SpecStore()
_roi = RoiInsynchClient()
_lessons = LessonStore()
_events = agent_events.EventStore()


def _emit(source: str, event_type: str, subject: str, outcome: str = "resolved",
          decision: Optional[str] = None, **metadata) -> None:
    """Record an agent-activity event for the dashboard (best-effort)."""
    try:
        _events.record(source, event_type, subject, outcome, decision, metadata or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record agent event: %s", exc)


GENERATORS = {
    "855": lambda s: generate_855(s.order, s.mappings),
    "856": lambda s: generate_856(s.order, s.mappings),
    "810": lambda s: generate_810(s.order, s.mappings),
}


def _persist(session: OrderSession) -> None:
    """Snapshot a session to SQLite so it survives a restart (best-effort)."""
    try:
        _state.save_session(session.order.po_number, session.to_storage())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist session %s: %s", session.order.po_number, exc)


def _generate(session: OrderSession, doc_type: str) -> str:
    """Generate a baseline doc, refine it against the partner spec, validate."""
    if doc_type == "997":
        x12 = generate_997(session.order, accepted=session.parse_ok)
    elif doc_type in GENERATORS:
        x12 = GENERATORS[doc_type](session)
    else:
        raise ValueError(f"Unknown doc type: {doc_type}")
    # The deterministic baseline must always be structurally valid.
    validate_document(x12).raise_if_failed()

    # Spec-guided refinement: conform to the trading partner's companion guide,
    # primed with lessons learned from this partner's past failures.
    partner = session.order.partner_isa_id
    spec = _specs.find(doc_type, partner)
    if spec and spec_generator.available():
        lessons = _lessons.lessons_for(partner, doc_type)
        tailored, used, note = spec_generator.tailor_with_spec(
            doc_type, session.order, x12, spec.get("file_path"), lessons=lessons)
        session.spec_notes[doc_type] = note
        if used:
            x12 = tailored
        elif "invalid" in note:
            # The model produced an invalid doc -> learn from it.
            _lessons.record(partner, doc_type, note, source="validation")
    elif spec:
        session.spec_notes[doc_type] = "spec on file; spec-guided pass disabled/unavailable"

    session.documents[doc_type] = x12
    _persist(session)
    _emit("edi_agent", "edi", f"{doc_type} generated for PO {session.order.po_number}",
          outcome="resolved", decision="self_heal", po=session.order.po_number,
          doc_type=doc_type, spec_note=session.spec_notes.get(doc_type))
    return x12


def _repair_document(session: OrderSession, doc_type: str, failure_message: str,
                     source_x12: Optional[str] = None) -> str:
    """Repair a generated document from a partner rejection/failure message."""
    if doc_type != "997" and doc_type not in GENERATORS:
        raise ValueError(f"Unknown doc type: {doc_type}")
    if not (failure_message or "").strip():
        raise ValueError("failure_message is required")

    x12 = source_x12 or session.documents.get(doc_type)
    if not x12:
        x12 = _generate(session, doc_type)

    partner = session.order.partner_isa_id
    spec = _specs.find(doc_type, partner)
    if not spec:
        raise ValueError(
            f"No {doc_type} spec found for {partner or 'this partner'}"
        )

    # The doc failed -> record the failure as a durable lesson, then correct it
    # (priming the correction with everything we've learned for this partner/doc).
    _lessons.record(partner, doc_type, failure_message, source="rejection")
    lessons = _lessons.lessons_for(partner, doc_type)
    repaired, used, note = spec_generator.repair_with_failure(
        doc_type, session.order, x12, failure_message, spec.get("file_path"),
        lessons=lessons)
    session.spec_notes[doc_type] = note
    if not used:
        raise ValueError(note)

    validate_document(repaired).raise_if_failed()
    session.documents[doc_type] = repaired
    _persist(session)
    _emit("edi_agent", "resolution",
          f"{doc_type} rejection auto-corrected for PO {session.order.po_number}",
          outcome="resolved", decision="self_heal", po=session.order.po_number,
          doc_type=doc_type, failure=failure_message[:200])
    return repaired


def _submit(session: OrderSession, doc_type: str) -> str:
    if doc_type not in session.documents:
        _generate(session, doc_type)
    partner = session.mappings.get("trading_partner") or session.order.partner_isa_id
    try:
        tx_id = _orderful.submit(session.documents[doc_type], partner, doc_type)
    except OrderfulError as exc:
        _state.log_error(session.order.po_number, f"{doc_type} submit: {exc}")
        try:
            _repair_document(session, doc_type, str(exc))
        except Exception as repair_exc:  # noqa: BLE001 - original submit error matters most
            session.spec_notes[doc_type] = (
                f"submission failed; correction unavailable: {repair_exc}"
            )
            raise exc
        tx_id = _orderful.submit(session.documents[doc_type], partner, doc_type)
    session.submissions[doc_type] = tx_id
    # Persist to the order state machine.
    _state.mark_doc_sent(session.order.po_number, doc_type, tx_id)
    if doc_type == "856" and session.mappings.get("ship_date"):
        _state.set_fields(session.order.po_number, ship_date=session.mappings["ship_date"])
    if doc_type == "810":
        _state.set_fields(session.order.po_number,
                          invoice_number=session.mappings.get("invoice_number"))
    _persist(session)
    _emit("edi_agent", "edi",
          f"{doc_type} submitted for PO {session.order.po_number} (tx {tx_id})",
          outcome="resolved", decision="self_heal", po=session.order.po_number,
          doc_type=doc_type, transaction_id=tx_id)
    return tx_id


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

app = FastAPI(title="EDI Agent", version="2.0")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# --------------------------------------------------------------------------
# Response envelope -- every JSON endpoint returns {success, data, error} so
# OpenClaw (and any other caller) gets a consistent contract.
# --------------------------------------------------------------------------

def ok(data) -> dict:
    return {"success": True, "data": data, "error": None}


def fail(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None,
                 "error": {"message": message, "status": status, **extra}},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
    return fail(str(exc.detail), status=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    return fail("Request validation failed", status=422, details=exc.errors())


@app.exception_handler(Exception)
async def _unhandled_exc_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return fail(f"{type(exc).__name__}: {exc}", status=500)


class InboundEDI(BaseModel):
    edi: str
    submit_997: bool = True


class ShipData(BaseModel):
    po_number: Optional[str] = None       # used by the /webhook/ship body
    ship_date: Optional[str] = None       # YYYYMMDD
    ship_time: Optional[str] = None        # HHMM
    carrier_code: Optional[str] = None     # X12 code, e.g. UPSN
    ship_method: Optional[str] = None
    service_level: Optional[str] = None
    bill_of_lading: Optional[str] = None
    tracking_numbers: Optional[list] = None
    packages: Optional[list] = None        # [{tracking, weight_lbs, lines:[{line_num, qty_shipped}]}]
    freight_amount: Optional[float] = None
    submit: bool = True


class ChatMessage(BaseModel):
    message: str
    po_number: Optional[str] = None   # active order context


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return ok({
        "status": "ok",
        "config": config.as_dict(),
        "orderful_configured": _orderful.configured,
        "shipping_configured": _shipping.configured,
        "sql_configured": _sql.available,
        "llm_enabled": conversation.llm_available(),
        "spec_guided": spec_generator.available(),
        "integrations": [
            {**p, "activated": bool(_specs.activation(p["trading_partner"]))}
            for p in _specs.partners()
        ],
        "roi_configured": _roi.configured,
        "lessons_learned": _lessons.count(),
    })


@app.get("/learning")
def list_learning(trading_partner: Optional[str] = None, doc_type: Optional[str] = None):
    """Failure lessons the agent has learned (newest first)."""
    if trading_partner and doc_type:
        return ok({"lessons": _lessons.lessons_for(trading_partner, doc_type),
                   "trading_partner": trading_partner, "doc_type": doc_type})
    return ok({"lessons": _lessons.all(), "count": _lessons.count()})


# --------------------------------------------------------------------------
# Multi-bot dashboard + Judge
# --------------------------------------------------------------------------

class AgentEvent(BaseModel):
    source: str                         # which bot reported (inboxbot, open_claw, ...)
    event_type: str = "event"           # heartbeat | email | edi | erp_query | ...
    subject: str = ""
    outcome: str = "resolved"           # pending | resolved | escalated | failed | ignored
    decision: Optional[str] = None      # self_heal | escalate | watch
    metadata: Optional[dict] = None
    created_at: Optional[str] = None


@app.post("/events")
async def ingest_event(request: Request):
    """Ingest agent activity from any bot (single event or a list).

    Lets the autonomous-department agents (InboxBot, EDI Monitor, sage_bot,
    leadtime_bot, ceo_scheduler, open_claw) report into the unified dashboard
    using the work_events shape — the bridge ahead of the full merge.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="Body must be JSON")
    items = body if isinstance(body, list) else [body]
    recorded = 0
    for raw in items:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="Each event must be an object")
        try:
            ev = AgentEvent(**raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"Invalid event: {exc}")
        _events.record(ev.source, ev.event_type, ev.subject, ev.outcome,
                       ev.decision, ev.metadata, ev.created_at)
        recorded += 1
    return ok({"recorded": recorded, "total_events": _events.count()})


@app.get("/api/dashboard")
def api_dashboard(days: int = 7):
    """Everything the dashboard renders: judged agents + EDI pipeline + learning."""
    window = _events.all_in_window(days)
    evaluation = judge_mod.evaluate(window)
    orders = _state.list_orders()
    phase_counts: dict = {}
    for o in orders:
        phase_counts[o["phase"]] = phase_counts.get(o["phase"], 0) + 1
    return ok({
        "judge": evaluation,
        "recent_events": _events.recent(limit=40),
        "edi_pipeline": {
            "orders": len(orders),
            "by_phase": phase_counts,
            "recent_orders": orders[:15],
        },
        "learning": {"lessons": _lessons.count()},
        "connectors": {
            "orderful": _orderful.configured,
            "shipping": _shipping.configured,
            "sql": _sql.available,
            "roi": _roi.configured,
            "llm": conversation.llm_available(),
        },
        "window_days": days,
    })


@app.get("/api/judge")
def api_judge(days: int = 7):
    """The Judge's per-agent verdicts (success/failure + reward) over a window."""
    return ok(judge_mod.evaluate(_events.all_in_window(days)))


@app.get("/dashboard")
def dashboard_page():
    return FileResponse(os.path.join(_STATIC_DIR, "dashboard.html"))


@app.post("/850/inbound")
def inbound_850(payload: InboundEDI):
    """Receive a raw 850, parse it, and immediately produce the 997."""
    with _LOCK:
        try:
            order = parse(payload.edi)
        except EDIParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        session = OrderSession(order)
        _SESSIONS[order.po_number] = session
        _state.record_received(order.po_number, parsed_ok=True)
        _emit("edi_agent", "edi", f"850 PO {order.po_number} parsed ({len(order.lines)} lines)", outcome="resolved", decision="self_heal", po=order.po_number)

        # 997 always fires immediately on receipt.
        edi_997 = _generate(session, "997")
        if payload.submit_997:
            _submit(session, "997")

        return ok({
            "po_number": order.po_number,
            "order": order.to_dict(),
            "ack_997": edi_997,
            "ack_997_submission": session.submissions.get("997"),
            "status": session.status(),
        })


@app.get("/order/{po_number}")
def get_order(po_number: str):
    session = _require(po_number)
    return ok({"order": session.order.to_dict(), "mappings": session.mappings,
               "status": session.status()})


class MappingUpdate(BaseModel):
    mappings: dict


class CorrectionRequest(BaseModel):
    failure_message: str
    edi: Optional[str] = None
    submit: bool = False


@app.post("/order/{po_number}/mappings")
def update_mappings(po_number: str, payload: MappingUpdate):
    session = _require(po_number)
    with _LOCK:
        session.mappings = merge_mappings({**session.mappings, **payload.mappings})
        _persist(session)
    return ok({"mappings": session.mappings, "status": session.status()})


@app.post("/order/{po_number}/generate/{doc_type}")
def generate_doc(po_number: str, doc_type: str, submit: bool = False):
    session = _require(po_number)
    with _LOCK:
        try:
            x12 = _generate(session, doc_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        tx = _submit(session, doc_type) if submit else None
    return ok({"doc_type": doc_type, "edi": x12, "submission": tx,
               "status": session.status()})


@app.post("/order/{po_number}/correct/{doc_type}")
def correct_doc(po_number: str, doc_type: str, payload: CorrectionRequest):
    """Correct a failed outbound file using the failure text + partner spec."""
    session = _require(po_number)
    with _LOCK:
        try:
            x12 = _repair_document(
                session, doc_type, payload.failure_message, payload.edi)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        tx = _submit(session, doc_type) if payload.submit else None
    return ok({"doc_type": doc_type, "edi": x12, "submission": tx,
               "status": session.status()})


class StandaloneCorrection(BaseModel):
    edi: str                              # the rejected X12 file
    failure_message: str
    trading_partner: Optional[str] = None  # inferred from the EDI's ISA if omitted
    doc_type: Optional[str] = None         # inferred from the EDI's ST if omitted


def _peek_x12(edi: str) -> tuple:
    """Pull (doc_type, trading_partner) out of a raw outbound X12 doc.

    doc_type = ST01; trading_partner = ISA08 (the receiver of an outbound doc).
    """
    text = (edi or "").lstrip()
    elem = text[3] if text[:3] == "ISA" and len(text) > 3 else "*"
    doc_type, partner = "", ""
    for seg in re.split(r"[~\n]", text):
        parts = seg.strip().split(elem)
        tag = parts[0].upper() if parts else ""
        if tag == "ISA" and len(parts) > 8:
            partner = parts[8].strip()
        elif tag == "ST" and len(parts) > 1 and not doc_type:
            doc_type = parts[1].strip()
    return doc_type, partner


@app.post("/correct")
def correct_standalone(payload: StandaloneCorrection):
    """Correct any rejected X12 file from its error message — no active order needed.

    Reads the doc type + trading partner off the file (overridable), looks up
    that partner's companion guide, and returns a corrected, re-validated file.
    """
    if not (payload.edi or "").strip():
        raise HTTPException(status_code=400, detail="edi (the rejected file) is required")
    if not (payload.failure_message or "").strip():
        raise HTTPException(status_code=400, detail="failure_message is required")

    peek_doc, peek_partner = _peek_x12(payload.edi)
    doc_type = payload.doc_type or peek_doc
    partner = payload.trading_partner or peek_partner
    if doc_type not in ("997", "855", "856", "810"):
        raise HTTPException(status_code=422,
                            detail=f"Couldn't read a supported doc type from the file "
                                   f"(got {doc_type!r}). Pass doc_type explicitly.")
    if not spec_generator.available():
        raise HTTPException(status_code=422,
                            detail="Spec-guided correction needs ANTHROPIC_API_KEY in edi_agent/.env.")
    spec = _specs.find(doc_type, partner)
    if not spec:
        raise HTTPException(status_code=422,
                            detail=f"No {doc_type} companion guide on file for "
                                   f"'{partner or 'this partner'}'. Upload one (Upload spec) first.")

    repaired, used, note = spec_generator.repair_with_failure(
        doc_type, None, payload.edi, payload.failure_message, spec.get("file_path"))
    if not used:
        raise HTTPException(status_code=422, detail=note)
    return ok({"doc_type": doc_type, "trading_partner": partner, "edi": repaired, "note": note})


def _apply_ship_payload(session: OrderSession, data: "ShipData", po_number: str) -> None:
    """Merge a ship payload (and any ShipStation pull) into the session mappings."""
    pulled = {}
    if _shipping.configured:
        try:
            pulled = _shipping.get_shipment(po_number) or {}
        except ShippingError as exc:
            logger.warning("Shipping lookup failed for %s: %s", po_number, exc)
    m = session.mappings
    m["ship_date"] = data.ship_date or pulled.get("ship_date") or m.get("ship_date")
    m["ship_time"] = data.ship_time or pulled.get("ship_time") or m.get("ship_time")
    m["carrier_code"] = data.carrier_code or pulled.get("carrier_code") or m.get("carrier_code")
    if data.ship_method:
        m["ship_method"] = data.ship_method
    if data.service_level or pulled.get("service_level"):
        m["service_level"] = data.service_level or pulled.get("service_level")
    if data.bill_of_lading:
        m["bill_of_lading"] = data.bill_of_lading
    tracking = data.tracking_numbers or pulled.get("tracking_numbers")
    if tracking:
        m["tracking_numbers"] = list(tracking)
    packages = data.packages or pulled.get("packages")
    if packages:
        m["packages"] = packages
    if data.freight_amount is not None:
        m["freight_amount"] = data.freight_amount


@app.post("/order/{po_number}/ship")
def ship(po_number: str, data: ShipData):
    """Set ship data, then auto-trigger the 856 and (if submitted) the 810."""
    session = _require(po_number)
    with _LOCK:
        _apply_ship_payload(session, data, po_number)
        m = session.mappings
        if not (m.get("ship_date") and m.get("tracking_numbers")):
            raise HTTPException(
                status_code=422,
                detail="Need a ship_date and tracking numbers (in the payload or via "
                       "ShipStation) before generating the 856/810.",
            )
        edi_856 = _generate(session, "856")
        edi_810 = _generate(session, "810")
        submissions = {}
        if data.submit:
            submissions["856"] = _submit(session, "856")
            submissions["810"] = _submit(session, "810")  # 810 fires after the 856
    return ok({"po_number": po_number, "ship_notice_856": edi_856,
               "invoice_810": edi_810, "submissions": submissions,
               "status": session.status()})


@app.post("/order/{po_number}/invoice")
def invoice(po_number: str, submit: bool = True):
    """Manually generate (and optionally submit) the 810 invoice only."""
    session = _require(po_number)
    with _LOCK:
        edi_810 = _generate(session, "810")
        tx = _submit(session, "810") if submit else None
    return ok({"po_number": po_number, "invoice_810": edi_810,
               "submission": tx, "status": session.status()})


@app.get("/order/{po_number}/status")
def order_status(po_number: str):
    """Full document state for a PO (persisted), merged with the live session."""
    state = _state.get(po_number)
    session = _SESSIONS.get(po_number)
    if not state and not session:
        raise HTTPException(status_code=404, detail=f"No order {po_number}")
    return ok({
        "po_number": po_number,
        "state": state,
        "session_status": session.status() if session else None,
    })


@app.get("/orders")
def list_orders():
    """List every known PO and its current state."""
    return ok({"orders": _state.list_orders()})


@app.get("/order/{po_number}/output/{doc_type}")
def get_output(po_number: str, doc_type: str):
    session = _require(po_number)
    if doc_type not in session.documents:
        with _LOCK:
            try:
                _generate(session, doc_type)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
    return ok({"doc_type": doc_type, "edi": session.documents[doc_type]})


@app.post("/webhook/ship")
def ship_webhook(data: ShipData):
    """Ship-event webhook: same behaviour as /order/{po}/ship, PO in the body."""
    if not data.po_number:
        raise HTTPException(status_code=422, detail="po_number is required")
    return ship(data.po_number, data)


@app.post("/order/{po_number}/import-to-sage")
def import_to_sage(po_number: str):
    """Create the Sage 100 sales order for this PO via the ROI InSynch API."""
    session = _require(po_number)
    with _LOCK:
        try:
            result = _roi.import_sales_order(session.order, session.mappings)
        except RoiInsynchError as exc:
            _state.log_error(po_number, f"sage import: {exc}")
            raise HTTPException(status_code=502, detail=str(exc))
        session.sage_order_no = result.get("sales_order_no")
        _state.set_fields(po_number)  # touch updated_at
        _persist(session)
    return ok({"po_number": po_number, "sage_import": result,
               "status": session.status()})


@app.post("/order/{po_number}/enrich-prices")
def enrich_prices(po_number: str):
    """Fill missing SKU prices from SQL Server product master (Phase 2)."""
    session = _require(po_number)
    if not _sql.available:
        raise HTTPException(status_code=503,
                            detail="SQL Server connector not configured (set SQL_SERVER_CONN).")
    with _LOCK:
        _sql.enrich_mappings(session.order, session.mappings)
    return ok({"mappings": session.mappings, "status": session.status()})


# --------------------------------------------------------------------------
# Partner spec library + integration onboarding
# --------------------------------------------------------------------------

@app.post("/specs")
async def upload_spec(
    doc_type: str = Form(...),
    trading_partner: str = Form(""),
    file: UploadFile = File(...),
):
    """Upload a partner companion-guide PDF for a doc type (855/856/810/...)."""
    if doc_type not in ("850", "855", "856", "810", "997"):
        raise HTTPException(status_code=400, detail=f"Unsupported doc_type {doc_type!r}")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    spec = _specs.save(trading_partner, doc_type, file.filename or "spec.pdf", content)
    return ok(spec_store.public(spec))


@app.get("/specs")
def list_specs(trading_partner: Optional[str] = None):
    return ok({"specs": [spec_store.public(s) for s in _specs.list(trading_partner)]})


@app.delete("/specs/{spec_id}")
def delete_spec(spec_id: str):
    if not _specs.delete(spec_id):
        raise HTTPException(status_code=404, detail=f"No spec {spec_id}")
    return ok({"deleted": spec_id})


@app.get("/integrations")
def list_integrations():
    """Each onboarded partner, the doc types its specs cover, and activation."""
    integrations = []
    for p in _specs.partners():
        act = _specs.activation(p["trading_partner"])
        integrations.append({**p, "activated": bool(act),
                             "workflow": act.get("workflow") if act else None})
    return ok({"integrations": integrations,
               "spec_guided_active": spec_generator.available()})


class BuildRequest(BaseModel):
    sample_850: str                         # a sample X12 850 for this partner
    submit: bool = False


@app.post("/integrations/{trading_partner}/build")
def build_integration(trading_partner: str, payload: BuildRequest):
    """Exercise a partner integration end-to-end from a sample 850.

    Parses the sample, then for every doc type the partner has a spec for (997
    always included), generates + validates the document (spec-guided when an
    API key is set). Returns a per-doc report you can turn into tests.
    """
    try:
        order = parse(payload.sample_850)
    except EDIParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    covered = {s["doc_type"] for s in _specs.list(trading_partner)}
    covered.add("997")  # always acknowledge
    order_phase = ["997", "855", "856", "810"]
    doc_types = [d for d in order_phase if d in covered]

    with _LOCK:
        session = OrderSession(order)
        _SESSIONS[order.po_number] = session
        _state.record_received(order.po_number, parsed_ok=True)
        _emit("edi_agent", "edi", f"850 PO {order.po_number} parsed ({len(order.lines)} lines)", outcome="resolved", decision="self_heal", po=order.po_number)
        report = {}
        for dt in doc_types:
            try:
                edi = _generate(session, dt)
                tx = _submit(session, dt) if payload.submit else None
                report[dt] = {
                    "ok": True,
                    "spec_guided": session.spec_notes.get(dt, "no spec on file"),
                    "submission": tx,
                    "edi": edi,
                }
            except Exception as exc:  # noqa: BLE001
                report[dt] = {"ok": False, "error": str(exc)}

    return ok({
        "trading_partner": trading_partner,
        "po_number": order.po_number,
        "doc_types_built": doc_types,
        "spec_guided_active": spec_generator.available(),
        "report": report,
        "status": session.status(),
    })


def _build_workflow(trading_partner: str, doc_types: list) -> dict:
    """Describe the 3-phase automation wired for a partner (LogicBroker-style)."""
    produced = set(doc_types)
    phases = [
        {"phase": "import", "trigger": "inbound 850 from Orderful",
         "produces": [d for d in ("997", "855") if d in produced],
         "sink": "Sage 100 sales order via ROI InSynch"},
        {"phase": "asn", "trigger": "shipment in ShipStation",
         "produces": [d for d in ("856",) if d in produced]},
        {"phase": "invoice", "trigger": "invoice in Sage AR",
         "produces": [d for d in ("810",) if d in produced]},
    ]
    return {
        "trading_partner": trading_partner,
        "doc_types": sorted(doc_types),
        "phases": [p for p in phases if p["produces"]],
        "run": "python -m edi_agent.orderful_sync",
    }


@app.post("/order/{po_number}/activate-workflow")
def activate_workflow(po_number: str):
    """Wire up the partner's automated workflow once its docs generate cleanly.

    Validates that every doc type the partner has a spec for (plus the 997)
    generates and passes validation for this order, then records the partner's
    workflow so the orderful_sync loop runs it. This is the "build the
    workflow" button — the LogicBroker-style import/ASN/invoice automation.
    """
    session = _require(po_number)
    partner = session.order.partner_isa_id or ""
    covered = {s["doc_type"] for s in _specs.list(partner)} if partner else set()
    if not covered:
        raise HTTPException(
            status_code=422,
            detail=f"No companion guides on file for '{partner or 'this partner'}'. "
                   "Upload at least one spec (855/856/810) before wiring the workflow.",
        )
    doc_types = [d for d in ("997", "855", "856", "810") if d == "997" or d in covered]

    report = {}
    all_ok = True
    with _LOCK:
        for dt in doc_types:
            try:
                _generate(session, dt)
                report[dt] = {"ok": True, "spec_guided": session.spec_notes.get(dt, "baseline")}
            except Exception as exc:  # noqa: BLE001
                report[dt] = {"ok": False, "error": str(exc)}
                all_ok = False

        workflow = _build_workflow(partner, doc_types)
        if all_ok:
            _specs.activate(partner, doc_types, workflow)

    return ok({
        "trading_partner": partner,
        "activated": all_ok,
        "doc_types": doc_types,
        "report": report,
        "workflow": workflow,
        "spec_guided_active": spec_generator.available(),
        "status": session.status(),
    })


class SyncRequest(BaseModel):
    dry_run: bool = False
    sample_850: Optional[str] = None   # raw X12 to import instead of polling Orderful


@app.post("/sync/{phase}")
def run_sync(phase: str, payload: Optional[SyncRequest] = None):
    """Trigger the Orderful<->Sage sync over REST (OpenClaw / scheduler / button).

    ``phase`` is import | asn | invoice | all. Mirrors
    ``python -m edi_agent.orderful_sync --phase {phase}``; dry-safe without creds.
    """
    if phase not in ("import", "asn", "invoice", "all"):
        raise HTTPException(status_code=422,
                            detail="phase must be one of: import, asn, invoice, all")
    payload = payload or SyncRequest()
    from . import orderful_sync  # lazy import (orderful_sync imports this module)

    sample_orders = [payload.sample_850] if payload.sample_850 else None
    totals = {"imported": 0, "asn": 0, "invoice": 0}
    if phase in ("import", "all"):
        totals["imported"] = orderful_sync.phase_import(
            dry_run=payload.dry_run, sample_orders=sample_orders)
    if phase in ("asn", "all"):
        totals["asn"] = orderful_sync.phase_asn(dry_run=payload.dry_run)
    if phase in ("invoice", "all"):
        totals["invoice"] = orderful_sync.phase_invoice(dry_run=payload.dry_run)
    return ok({"phase": phase, "dry_run": payload.dry_run, "totals": totals,
               "orders": _state.list_orders()})


@app.post("/chat")
def chat(msg: ChatMessage):
    """Conversational entry point used by the web UI."""
    return ok(handle_chat(msg.message, msg.po_number))


@app.post("/agent/message")
def agent_message(msg: ChatMessage):
    """Single natural-language entry point for OpenClaw / external agents.

    Accepts a free-text instruction (+ optional po_number) and routes it
    through the same conversational brain used by the web UI, returning the
    standard {success, data, error} envelope.
    """
    return ok(handle_chat(msg.message, msg.po_number))


def _require(po_number: str) -> OrderSession:
    session = _SESSIONS.get(po_number)
    if session is None:
        # Lazy-rehydrate from the persisted snapshot (survives restarts).
        snap = _state.get_session(po_number)
        if snap:
            try:
                session = OrderSession.from_storage(snap)
                _SESSIONS[po_number] = session
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to restore session %s: %s", po_number, exc)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No order {po_number}")
    return session


def _restore_sessions() -> None:
    """Rehydrate persisted sessions into memory on startup."""
    for po in _state.list_session_pos():
        if po in _SESSIONS:
            continue
        snap = _state.get_session(po)
        try:
            if snap:
                _SESSIONS[po] = OrderSession.from_storage(snap)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to restore session %s on startup: %s", po, exc)


# --------------------------------------------------------------------------
# Conversational brain. Two backends:
#   * LLM (Claude API) when ANTHROPIC_API_KEY is set -- natural conversation
#     driven by tool use (see conversation.py).
#   * Deterministic intent parser otherwise -- reliable, no external dependency.
# --------------------------------------------------------------------------

_CONVERSATION: Optional["conversation.Conversation"] = None
# Per-turn scratch: lets the LLM tool executor surface the active PO back to
# the HTTP response so the web UI can refresh and fetch generated documents.
_llm_turn: dict = {}


def handle_chat(message: str, po_number: Optional[str]) -> dict:
    """Route a chat message to the LLM backend, falling back to the parser."""
    if conversation.llm_available():
        try:
            return _handle_chat_llm(message, po_number)
        except Exception as exc:  # noqa: BLE001 - never lose the user's turn
            logger.warning("LLM chat failed, falling back to deterministic: %s", exc)
    return _handle_chat_deterministic(message, po_number)


def _get_conversation() -> "conversation.Conversation":
    global _CONVERSATION
    if _CONVERSATION is None:
        _CONVERSATION = conversation.Conversation(_llm_execute)
    return _CONVERSATION


def _handle_chat_llm(message: str, po_number: Optional[str]) -> dict:
    """Drive the pipeline via Claude tool use, then shape the UI response."""
    global _llm_turn
    _llm_turn = {"po": po_number}
    reply = _get_conversation().send(message)
    po = _llm_turn.get("po") or po_number
    status = _SESSIONS[po].status() if po and po in _SESSIONS else None
    return {"reply": reply, "po_number": po, "status": status}


def _llm_execute(name: str, args: dict) -> dict:
    """Execute one tool call from the LLM against the pipeline."""
    if name == "parse_inbound_850":
        return _tool_parse(args["edi"])

    po = args.get("po_number")
    if not po or po not in _SESSIONS:
        return {"error": f"No order found for po_number={po!r}. Parse the 850 first."}
    session = _SESSIONS[po]
    _llm_turn["po"] = po

    if name == "get_order":
        return {"order": session.order.to_dict(), "mappings": session.mappings,
                "status": session.status()}
    if name == "get_status":
        return {"status": session.status()}
    if name == "update_mappings":
        with _LOCK:
            session.mappings = merge_mappings({**session.mappings, **(args.get("mappings") or {})})
            _persist(session)
        return {"mappings": session.mappings, "status": session.status()}
    if name == "generate_document":
        doc_type = args["doc_type"]
        with _LOCK:
            try:
                _generate(session, doc_type)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{doc_type} generation failed: {exc}"}
            tx = _submit(session, doc_type) if args.get("submit") else None
        return {"doc_type": doc_type, "submitted": bool(tx), "transaction_id": tx,
                "status": session.status()}
    if name == "correct_document":
        doc_type = args["doc_type"]
        with _LOCK:
            try:
                _repair_document(
                    session, doc_type, args.get("failure_message") or "", args.get("edi"))
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{doc_type} correction failed: {exc}"}
            tx = _submit(session, doc_type) if args.get("submit") else None
        return {"doc_type": doc_type, "corrected": True, "submitted": bool(tx),
                "transaction_id": tx, "status": session.status()}
    if name == "import_to_sage":
        with _LOCK:
            try:
                result = _roi.import_sales_order(session.order, session.mappings)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"Sage import failed: {exc}"}
            session.sage_order_no = result.get("sales_order_no")
            _persist(session)
        return {"sage_import": {k: v for k, v in result.items() if k != "payload"},
                "status": session.status()}
    if name == "set_ship_data":
        with _LOCK:
            m = session.mappings
            for key in ("ship_date", "ship_time", "carrier_code", "ship_method",
                        "service_level", "bill_of_lading"):
                if args.get(key):
                    m[key] = args[key]
            if args.get("tracking_numbers"):
                m["tracking_numbers"] = list(args["tracking_numbers"])
            if args.get("packages"):
                m["packages"] = args["packages"]
            _persist(session)
        return {"mappings": session.mappings, "status": session.status()}

    return {"error": f"Unknown tool {name}"}


def _tool_parse(edi: str) -> dict:
    """Tool: parse an 850 and fire the 997 (shared by the LLM executor)."""
    with _LOCK:
        order = parse(edi)
        session = OrderSession(order)
        _SESSIONS[order.po_number] = session
        _state.record_received(order.po_number, parsed_ok=True)
        _emit("edi_agent", "edi", f"850 PO {order.po_number} parsed ({len(order.lines)} lines)", outcome="resolved", decision="self_heal", po=order.po_number)
        _generate(session, "997")
        try:
            _submit(session, "997")
        except OrderfulError as exc:
            logger.warning("997 submission failed: %s", exc)
    _llm_turn["po"] = order.po_number
    return {"po_number": order.po_number, "order": order.to_dict(),
            "ack_997_submitted": session.submissions.get("997"),
            "status": session.status()}


def _handle_chat_deterministic(message: str, po_number: Optional[str]) -> dict:
    """Interpret a natural-language message and act on the pipeline.

    Returns ``{reply, po_number, status?, edi?, doc_type?}``.
    """
    text = (message or "").strip()
    low = text.lower()

    # 1) Inbound EDI pasted directly into chat.
    if "ISA" in text and text.lstrip().startswith("ISA"):
        return _chat_ingest(text)

    # Resolve active session.
    session = _SESSIONS.get(po_number) if po_number else None
    if session is None and _SESSIONS:
        # Fall back to the most recently added order.
        session = list(_SESSIONS.values())[-1]

    if low in ("help", "?", "/help"):
        return {"reply": _help_text(), "po_number": po_number}

    if session is None:
        return {"reply": "Paste an inbound X12 850 here (starting with `ISA`) and "
                         "I'll acknowledge it with a 997 and walk you through the rest.",
                "po_number": None}

    po = session.order.po_number

    # 2) Show order / status.
    if any(k in low for k in ("show order", "show the order", "what did you parse", "parsed")):
        return {"reply": _summarize_order(session), "po_number": po,
                "status": session.status()}
    if "status" in low:
        return {"reply": _status_text(session), "po_number": po, "status": session.status()}

    # 3) Mapping updates.
    updated = _apply_mapping_commands(session, text)
    if updated:
        return {"reply": "Updated: " + ", ".join(updated) + ".\n\n" + _mapping_summary(session),
                "po_number": po, "status": session.status()}

    # 4) Correct from a partner rejection message.
    # Detect when the user pastes an error report from a trading partner portal.
    if _looks_like_rejection(text) and session.documents:
        return _chat_correct(session, text)

    # 5) Generate / submit documents.
    do_submit = "submit" in low or "send" in low
    docs = _detect_doc_types(low)
    if docs:
        replies, last_edi, last_type = [], None, None
        with _LOCK:
            for dt in docs:
                try:
                    edi = _generate(session, dt)
                    last_edi, last_type = edi, dt
                    if do_submit:
                        tx = _submit(session, dt)
                        replies.append(f"{dt}: generated, validated, submitted (tx `{tx}`)")
                    else:
                        replies.append(f"{dt}: generated and validated ✓")
                except Exception as exc:  # noqa: BLE001 -- surface to user
                    replies.append(f"{dt}: error -- {exc}")
        return {"reply": "\n".join(replies), "po_number": po,
                "edi": last_edi, "doc_type": last_type, "status": session.status()}

    return {"reply": "I didn't catch a command there. Try `show order`, `set price SKU 29.99`, "
                     "`carrier UPSN`, `ship date 20250605`, `generate 855`, or `help`.",
            "po_number": po, "status": session.status()}


_REJECTION_KEYWORDS = re.compile(
    r"\b(rejected|rejection|invalid|error|failed|mismatch|missing|required|"
    r"expected|not found|not valid|does not match|incorrect|unrecognized|"
    r"segment|element|qualifier|acknowledgment code)\b",
    re.I,
)


def _looks_like_rejection(text: str) -> bool:
    """Heuristic: does this look like a trading-partner error / rejection report?"""
    if text.lstrip().startswith("ISA"):   # raw EDI, not an error
        return False
    if len(text) > 4000:                  # way too long to be an error message
        return False
    matches = _REJECTION_KEYWORDS.findall(text)
    return len(matches) >= 2              # need at least two error-ish words


def _chat_correct(session: OrderSession, error_text: str) -> dict:
    """Try to correct the most relevant doc from an error message pasted in chat."""
    po = session.order.po_number
    low = error_text.lower()

    # Guess doc type from the error text; fall back to most recently generated.
    if "855" in low:
        dt = "855"
    elif "856" in low or "asn" in low or "ship notice" in low:
        dt = "856"
    elif "810" in low or "invoice" in low:
        dt = "810"
    elif "997" in low or "acknowledgment" in low or "ack" in low:
        dt = "997"
    else:
        # Fall back to the most recently generated doc (last in sorted order).
        dt = sorted(session.documents.keys())[-1] if session.documents else None

    if not dt:
        return {"reply": "I see an error message but no active documents to correct. "
                         "Generate a document first.",
                "po_number": po, "status": session.status()}

    if not spec_generator.available():
        return {"reply": f"I can see this looks like a **{dt}** rejection, but the "
                         "spec-guided correction requires an `ANTHROPIC_API_KEY` in "
                         "`edi_agent/.env`. Add your key and restart.",
                "po_number": po, "status": session.status()}

    with _LOCK:
        try:
            x12 = _repair_document(session, dt, error_text)
        except ValueError as exc:
            return {"reply": f"⚠️ Could not auto-correct **{dt}**: {exc}\n\n"
                             "Make sure you have a companion guide PDF uploaded for "
                             "this trading partner (Upload spec button).",
                    "po_number": po, "status": session.status()}

    return {
        "reply": f"✓ Detected a **{dt}** rejection and corrected the document. "
                 "The updated EDI is now in the viewer — download and re-upload.",
        "po_number": po,
        "edi": x12,
        "doc_type": dt,
        "status": session.status(),
    }


def _chat_ingest(text: str) -> dict:
    with _LOCK:
        try:
            order = parse(text)
        except EDIParseError as exc:
            return {"reply": f"I couldn't parse that 850: {exc}\n\n"
                             "I'd normally return a rejecting 997 here. Check the segment noted above.",
                    "po_number": None}
        session = OrderSession(order)
        _SESSIONS[order.po_number] = session
        _state.record_received(order.po_number, parsed_ok=True)
        _emit("edi_agent", "edi", f"850 PO {order.po_number} parsed ({len(order.lines)} lines)", outcome="resolved", decision="self_heal", po=order.po_number)
        edi_997 = _generate(session, "997")
        try:
            _submit(session, "997")
            sub = session.submissions.get("997")
        except OrderfulError as exc:
            sub = f"(submission failed: {exc})"
    reply = (f"Got it — parsed PO **{order.po_number}** with {len(order.lines)} line(s).\n"
             f"I generated and submitted the **997** acknowledgment (tx `{sub}`).\n\n"
             + _summarize_order(session) +
             "\n\nNext, give me the mappings you want (prices, carrier, ship method), "
             "then say `generate 855`.")
    return {"reply": reply, "po_number": order.po_number, "edi": edi_997,
            "doc_type": "997", "status": session.status()}


_DOC_RE = re.compile(r"\b(997|855|856|810)\b")


def _detect_doc_types(low: str) -> list:
    if not any(w in low for w in ("generate", "create", "make", "build", "submit", "send", "produce")):
        return []
    if "all" in low or "everything" in low:
        return ["997", "855", "856", "810"]
    return _DOC_RE.findall(low)


def _apply_mapping_commands(session: OrderSession, text: str) -> list:
    """Parse simple mapping commands out of free text. Returns labels changed."""
    low = text.lower()
    m = session.mappings
    changed = []

    # price: "set price SKU123 29.99" / "price for ABC123 is 29.99"
    for match in re.finditer(r"price\s+(?:for\s+)?([A-Za-z0-9_\-]+)\s+(?:is\s+|to\s+|=\s*|at\s+)?\$?(\d+(?:\.\d+)?)", low):
        sku, price = match.group(1).upper(), float(match.group(2))
        m.setdefault("sku_prices", {})[sku] = price
        changed.append(f"price {sku}={price}")

    # default price: "default price 19.99"
    dp = re.search(r"default price\s+\$?(\d+(?:\.\d+)?)", low)
    if dp:
        m["default_unit_price"] = float(dp.group(1))
        changed.append(f"default price={dp.group(1)}")

    # carrier: "carrier UPSN"
    c = re.search(r"carrier\s+(?:code\s+)?([A-Za-z0-9]+)", low)
    if c:
        m["carrier_code"] = c.group(1).upper()
        changed.append(f"carrier={m['carrier_code']}")

    # ship method
    sm = re.search(r"ship method\s+([A-Za-z0-9 ]+?)(?:[.,]|$)", low)
    if sm:
        m["ship_method"] = sm.group(1).strip().upper()
        changed.append(f"ship method={m['ship_method']}")

    # acknowledgment code: "ack code AC" / "acknowledgment IA"
    ack = re.search(r"ack(?:nowledg(?:e|ment))?\s+(?:code\s+)?(AC|IA|IQ|IP|IR|RJ)", text, re.I)
    if ack:
        m["acknowledgment_code"] = ack.group(1).upper()
        changed.append(f"ack code={m['acknowledgment_code']}")

    # ship date: "ship date 20250605"
    sd = re.search(r"ship date\s+(\d{8})", low)
    if sd:
        m["ship_date"] = sd.group(1)
        changed.append(f"ship date={sd.group(1)}")

    # ship time
    st = re.search(r"ship time\s+(\d{3,4})", low)
    if st:
        m["ship_time"] = st.group(1).zfill(4)
        changed.append(f"ship time={m['ship_time']}")

    # invoice date / number
    idt = re.search(r"invoice date\s+(\d{8})", low)
    if idt:
        m["invoice_date"] = idt.group(1)
        changed.append(f"invoice date={idt.group(1)}")
    inum = re.search(r"invoice (?:number|no|#)\s+([A-Za-z0-9\-]+)", low)
    if inum:
        m["invoice_number"] = inum.group(1).upper()
        changed.append(f"invoice #={m['invoice_number']}")

    # tracking: "tracking 1Z999..."
    tr = re.search(r"tracking\s+(?:number\s+|#\s*)?([A-Za-z0-9]{6,})", text, re.I)
    if tr:
        m.setdefault("tracking_numbers", [])
        if not isinstance(m["tracking_numbers"], list):
            m["tracking_numbers"] = []
        m["tracking_numbers"].append(tr.group(1))
        changed.append(f"tracking={tr.group(1)}")

    # payment terms days: "net 30" / "terms 30"
    pt = re.search(r"(?:net|terms)\s+(\d{1,3})", low)
    if pt:
        m["payment_terms_days"] = int(pt.group(1))
        m["payment_terms"] = f"Net{pt.group(1)}"
        changed.append(f"terms=Net{pt.group(1)}")

    return changed


def _summarize_order(s: OrderSession) -> str:
    o = s.order
    lines = "\n".join(
        f"  • line {li.line_num}: {int(li.qty_ordered)} {li.uom} "
        f"{li.vendor_part or li.buyer_part or li.upc or ''} "
        f"{('@ $' + format(li.unit_price, '.2f')) if li.unit_price else ''} "
        f"{('— ' + li.description) if li.description else ''}".rstrip()
        for li in o.lines
    )
    ship = o.ship_to.name if o.ship_to else "?"
    return (f"**PO {o.po_number}** (dated {o.po_date or '—'})\n"
            f"Ship-to: {ship}\n"
            f"Requested ship: {o.requested_ship_date or '—'}, "
            f"delivery: {o.requested_delivery_date or '—'}\n"
            f"Lines ({len(o.lines)}):\n{lines}")


def _mapping_summary(s: OrderSession) -> str:
    m = s.mappings
    prices = m.get("sku_prices") or {}
    tracking = m.get("tracking_numbers") or []
    return ("Current mappings:\n"
            f"  ack code: {m.get('acknowledgment_code')}\n"
            f"  prices: {prices or '(none — using 850 prices / default)'}\n"
            f"  default price: {m.get('default_unit_price')}\n"
            f"  carrier: {m.get('carrier_code')}, method: {m.get('ship_method')}\n"
            f"  ship date: {m.get('ship_date') or '—'}, tracking: {tracking or '—'}\n"
            f"  invoice date: {m.get('invoice_date') or '(today)'}, "
            f"terms: {m.get('payment_terms')}")


def _status_text(s: OrderSession) -> str:
    st = s.status()
    return (f"PO {st['po_number']}\n"
            f"  generated: {', '.join(st['documents_generated']) or '—'}\n"
            f"  submitted: {st['submissions'] or '—'}\n"
            f"  ready to ship (856/810): {st['ready_to_ship']}")


def _help_text() -> str:
    return (
        "I'm your EDI agent. Here's what I understand:\n"
        "  • Paste an X12 **850** (starts with `ISA`) — I parse it and fire the 997.\n"
        "  • `show order` / `status` — see what I parsed and what's done.\n"
        "  • `set price SKU123 29.99` · `default price 19.99`\n"
        "  • `carrier UPSN` · `ship method GROUND` · `ack code AC`\n"
        "  • `ship date 20250605` · `ship time 1400` · `tracking 1Z999AA10123456784`\n"
        "  • `invoice date 20250605` · `invoice number INV1001` · `net 30`\n"
        "  • `generate 855` · `generate 856` · `generate 810` · `generate all`\n"
        "  • add `submit`/`send` to also push it to Orderful (e.g. `submit 855`)."
    )


# Rehydrate any persisted sessions so orders survive a restart.
_restore_sessions()

# Mount static assets last so routes above take precedence.
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
