"""EDI Agent orchestrator -- FastAPI app + conversational brain.

Wires the parser, generators, validator, and Orderful connector together
and exposes them two ways:

  * REST endpoints from the brief (``/850/inbound``, ``/order/{po}`` ...)
  * a chat endpoint (``/chat``) that lets Robert drive the whole pipeline in
    plain language from the web UI.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import config
from .connectors.orderful import OrderfulClient, OrderfulError
from .core.models import Order
from .core.parser import EDIParseError, parse
from .core.validator import validate_document
from .generators.gen_810 import generate_810
from .generators.gen_855 import generate_855
from .generators.gen_856 import generate_856
from .generators.gen_997 import generate_997
from .mappings import default_mappings, merge_mappings

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
        self.parse_ok = True
        self.parse_error = ""

    def status(self) -> dict:
        return {
            "po_number": self.order.po_number,
            "parse_ok": self.parse_ok,
            "documents_generated": sorted(self.documents.keys()),
            "submissions": dict(self.submissions),
            "ready_to_ship": bool(self.mappings.get("ship_date") and
                                  self.mappings.get("tracking_numbers", {}).get(self.order.po_number)),
        }


_LOCK = threading.Lock()
_SESSIONS: Dict[str, OrderSession] = {}   # po_number -> session
_orderful = OrderfulClient()


GENERATORS = {
    "855": lambda s: generate_855(s.order, s.mappings),
    "856": lambda s: generate_856(s.order, s.mappings),
    "810": lambda s: generate_810(s.order, s.mappings),
}


def _generate(session: OrderSession, doc_type: str) -> str:
    """Generate, validate, and cache one document."""
    if doc_type == "997":
        x12 = generate_997(session.order, accepted=session.parse_ok)
    elif doc_type in GENERATORS:
        x12 = GENERATORS[doc_type](session)
    else:
        raise ValueError(f"Unknown doc type: {doc_type}")
    validate_document(x12).raise_if_failed()
    session.documents[doc_type] = x12
    return x12


def _submit(session: OrderSession, doc_type: str) -> str:
    if doc_type not in session.documents:
        _generate(session, doc_type)
    partner = session.mappings.get("trading_partner") or session.order.partner_isa_id
    tx_id = _orderful.submit(session.documents[doc_type], partner, doc_type)
    session.submissions[doc_type] = tx_id
    return tx_id


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

app = FastAPI(title="EDI Agent", version="1.0")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class InboundEDI(BaseModel):
    edi: str
    submit_997: bool = True


class ShipData(BaseModel):
    ship_date: Optional[str] = None
    ship_time: Optional[str] = None
    carrier_code: Optional[str] = None
    ship_method: Optional[str] = None
    tracking_numbers: Optional[list] = None
    submit: bool = False


class ChatMessage(BaseModel):
    message: str
    po_number: Optional[str] = None   # active order context


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "config": config.as_dict(),
            "orderful_configured": _orderful.configured}


@app.post("/850/inbound")
def inbound_850(payload: InboundEDI):
    """Receive a raw 850, parse it, and immediately produce the 997."""
    with _LOCK:
        try:
            order = parse(payload.edi)
        except EDIParseError as exc:
            # We can still send a rejecting 997 if we recovered control numbers.
            raise HTTPException(status_code=422, detail=str(exc))

        session = OrderSession(order)
        _SESSIONS[order.po_number] = session

        # 997 always fires immediately on receipt.
        edi_997 = _generate(session, "997")
        if payload.submit_997:
            _submit(session, "997")

        return {
            "po_number": order.po_number,
            "order": order.to_dict(),
            "ack_997": edi_997,
            "ack_997_submission": session.submissions.get("997"),
            "status": session.status(),
        }


@app.get("/order/{po_number}")
def get_order(po_number: str):
    session = _require(po_number)
    return {"order": session.order.to_dict(), "mappings": session.mappings,
            "status": session.status()}


class MappingUpdate(BaseModel):
    mappings: dict


@app.post("/order/{po_number}/mappings")
def update_mappings(po_number: str, payload: MappingUpdate):
    session = _require(po_number)
    with _LOCK:
        session.mappings = merge_mappings({**session.mappings, **payload.mappings})
    return {"mappings": session.mappings, "status": session.status()}


@app.post("/order/{po_number}/generate/{doc_type}")
def generate_doc(po_number: str, doc_type: str, submit: bool = False):
    session = _require(po_number)
    with _LOCK:
        try:
            x12 = _generate(session, doc_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        tx = _submit(session, doc_type) if submit else None
    return {"doc_type": doc_type, "edi": x12, "submission": tx,
            "status": session.status()}


@app.post("/order/{po_number}/ship")
def ship(po_number: str, data: ShipData):
    """Set ship data, then trigger the 856 and 810."""
    session = _require(po_number)
    with _LOCK:
        if data.ship_date:
            session.mappings["ship_date"] = data.ship_date
        if data.ship_time:
            session.mappings["ship_time"] = data.ship_time
        if data.carrier_code:
            session.mappings["carrier_code"] = data.carrier_code
        if data.ship_method:
            session.mappings["ship_method"] = data.ship_method
        if data.tracking_numbers:
            session.mappings.setdefault("tracking_numbers", {})[po_number] = data.tracking_numbers

        edi_856 = _generate(session, "856")
        edi_810 = _generate(session, "810")
        if data.submit:
            _submit(session, "856")
            _submit(session, "810")
    return {"ship_notice_856": edi_856, "invoice_810": edi_810,
            "status": session.status()}


@app.get("/order/{po_number}/output/{doc_type}")
def get_output(po_number: str, doc_type: str):
    session = _require(po_number)
    if doc_type not in session.documents:
        with _LOCK:
            try:
                _generate(session, doc_type)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"doc_type": doc_type, "edi": session.documents[doc_type]})


@app.post("/chat")
def chat(msg: ChatMessage):
    """Conversational entry point used by the web UI."""
    return handle_chat(msg.message, msg.po_number)


def _require(po_number: str) -> OrderSession:
    session = _SESSIONS.get(po_number)
    if not session:
        raise HTTPException(status_code=404, detail=f"No order {po_number}")
    return session


# --------------------------------------------------------------------------
# Conversational brain -- deterministic intent handling (no external LLM).
# --------------------------------------------------------------------------

def handle_chat(message: str, po_number: Optional[str]) -> dict:
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

    # tracking: "tracking 1Z999... [for PO ...]"
    tr = re.search(r"tracking\s+(?:number\s+|#\s*)?([A-Za-z0-9]{6,})", text, re.I)
    if tr:
        m.setdefault("tracking_numbers", {}).setdefault(session.order.po_number, []).append(tr.group(1))
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
    tracking = (m.get("tracking_numbers") or {}).get(s.order.po_number, [])
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
