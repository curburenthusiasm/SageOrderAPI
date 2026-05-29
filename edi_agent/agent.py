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
from .config import config
from .connectors.orderful import OrderfulClient, OrderfulError
from .connectors.roi_insynch import RoiInsynchClient, RoiInsynchError
from .connectors.shipping import ShippingConnector, ShippingError
from .connectors.sql_reader import SQLReader
from .core.models import Order
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


_LOCK = threading.Lock()
_SESSIONS: Dict[str, OrderSession] = {}   # po_number -> session
_orderful = OrderfulClient()
_shipping = ShippingConnector()
_sql = SQLReader()
_state = order_state.OrderStateStore()
_specs = spec_store.SpecStore()
_roi = RoiInsynchClient()


GENERATORS = {
    "855": lambda s: generate_855(s.order, s.mappings),
    "856": lambda s: generate_856(s.order, s.mappings),
    "810": lambda s: generate_810(s.order, s.mappings),
}


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

    # Spec-guided refinement: conform to the trading partner's companion guide.
    spec = _specs.find(doc_type, session.order.partner_isa_id)
    if spec and spec_generator.available():
        tailored, used, note = spec_generator.tailor_with_spec(
            doc_type, session.order, x12, spec.get("file_path"))
        session.spec_notes[doc_type] = note
        if used:
            x12 = tailored
    elif spec:
        session.spec_notes[doc_type] = "spec on file; spec-guided pass disabled/unavailable"

    session.documents[doc_type] = x12
    return x12


def _submit(session: OrderSession, doc_type: str) -> str:
    if doc_type not in session.documents:
        _generate(session, doc_type)
    partner = session.mappings.get("trading_partner") or session.order.partner_isa_id
    tx_id = _orderful.submit(session.documents[doc_type], partner, doc_type)
    session.submissions[doc_type] = tx_id
    # Persist to the order state machine.
    _state.mark_doc_sent(session.order.po_number, doc_type, tx_id)
    if doc_type == "856" and session.mappings.get("ship_date"):
        _state.set_fields(session.order.po_number, ship_date=session.mappings["ship_date"])
    if doc_type == "810":
        _state.set_fields(session.order.po_number,
                          invoice_number=session.mappings.get("invoice_number"))
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
        "integrations": _specs.partners(),
        "roi_configured": _roi.configured,
    })


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


@app.post("/order/{po_number}/mappings")
def update_mappings(po_number: str, payload: MappingUpdate):
    session = _require(po_number)
    with _LOCK:
        session.mappings = merge_mappings({**session.mappings, **payload.mappings})
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
    """Each onboarded partner and the doc types its uploaded specs cover."""
    return ok({"integrations": _specs.partners(),
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
    if not session:
        raise HTTPException(status_code=404, detail=f"No order {po_number}")
    return session


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
    if name == "import_to_sage":
        with _LOCK:
            try:
                result = _roi.import_sales_order(session.order, session.mappings)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"Sage import failed: {exc}"}
            session.sage_order_no = result.get("sales_order_no")
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
        return {"mappings": session.mappings, "status": session.status()}

    return {"error": f"Unknown tool {name}"}


def _tool_parse(edi: str) -> dict:
    """Tool: parse an 850 and fire the 997 (shared by the LLM executor)."""
    with _LOCK:
        order = parse(edi)
        session = OrderSession(order)
        _SESSIONS[order.po_number] = session
        _state.record_received(order.po_number, parsed_ok=True)
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

    # 4) Generate / submit documents.
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


# Mount static assets last so routes above take precedence.
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
