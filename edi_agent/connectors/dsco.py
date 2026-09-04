"""connectors/dsco.py — Rithum Dsco Platform API connector (V3).

Rithum's Dsco platform (formerly DSCO) is a drop-ship/marketplace hub.
Target, Wayfair, and others use Dsco for supplier onboarding.

API docs: https://api.dsco.io/api/v3 (OpenAPI spec at /openapi.yaml)
Staging:  https://staging-api.dsco.io/api/v3

OAuth2 Client Credentials flow:
  1. POST /oauth2/accessToken  → bearer token
  2. Include "Authorization: bearer <token>" on every subsequent request

Supplier workflow (what we implement here):
  a. Inbound orders  → GET /orders  (poll by date) OR stream events
  b. Acknowledge PO  → POST /order/{dscoOrderId}/acknowledge
  c. Ship notice     → POST /shipment
  d. Invoice         → POST /invoice

Configuration (stored in partner registry connector_config JSONB):
{
    "dsco_retailer_id":  "12345",          // your Dsco retailer account ID
    "dsco_supplier_id":  "67890",          // your Dsco supplier account ID
    "base_url":          "https://api.dsco.io/api/v3",
    "auth": {
        "type":          "oauth2_client_credentials",
        "client_id":     "REPLACE_ME",     // from Dsco portal → Integrations → API keys
        "client_secret": "***",
        "token_url":     "https://api.dsco.io/api/v3/oauth2/token"
    },
    "order_stream_id":  "",                // optional: poll a Dsco stream instead of GET /orders
    "staging":          false              // set true to hit staging-api.dsco.io
}
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from .base import ConnectorBase, InboundDocument, SubmitResult

log = logging.getLogger(__name__)

_PROD_BASE    = "https://api.dsco.io/api/v3"
_STAGING_BASE = "https://staging-api.dsco.io/api/v3"
_TOKEN_PATH   = "/api/v3/oauth2/token"


class DscoConnector(ConnectorBase):
    """Rithum Dsco Platform API connector (V3)."""

    PLATFORM = "dsco"

    def __init__(self, connector_config: dict):
        self.cfg = connector_config or {}
        self._token: str | None = None
        self._token_expires: float = 0.0

    # ------------------------------------------------------------------
    # ConnectorBase interface
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        auth = self.cfg.get("auth", {})
        return bool(auth.get("client_id") and auth.get("client_secret"))

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> SubmitResult:
        """Submit an outbound document to Dsco.

        - 997 → skip (Dsco handles functional ACKs internally)
        - 855 → POST /order/{id}/acknowledge
        - 856 → POST /shipment
        - 810 → POST /invoice
        """
        if not self.configured:
            return SubmitResult("", ok=False,
                                error="DscoConnector not configured — set auth.client_id + client_secret")

        doc_type = doc_type.upper()
        po_number = _extract_po_number(x12_string)

        if doc_type == "997":
            return SubmitResult(f"DSCO-997-NOOP-{uuid.uuid4().hex[:8]}", ok=True,
                                raw={"note": "997 not required — Dsco handles ACKs internally"})
        if doc_type == "855":
            return self._submit_acknowledgment(po_number, x12_string)
        if doc_type == "856":
            return self._submit_shipment(po_number, x12_string)
        if doc_type == "810":
            return self._submit_invoice(po_number, x12_string)

        return SubmitResult("", ok=False, error=f"DscoConnector: unsupported doc type {doc_type}")

    def fetch_inbound(self, doc_type: str = "850", status: str = "new") -> list[InboundDocument]:
        """Fetch new inbound POs from Dsco.

        Prefers stream-based polling if order_stream_id is configured;
        falls back to GET /orders with a recent date range.
        """
        if not self.configured:
            return []

        stream_id = self.cfg.get("order_stream_id", "")
        if stream_id:
            return self._fetch_stream_events(stream_id)
        return self._fetch_orders_poll()

    def acknowledge(self, transaction_id: str, status: str = "accepted") -> bool:
        """Mark a Dsco order as acknowledged."""
        if not self.configured:
            return False
        try:
            payload = {"acknowledgementType": status.lower()}
            self._post(f"/order/{transaction_id}/acknowledge", payload)
            return True
        except Exception as exc:
            log.error(f"DscoConnector.acknowledge error: {exc}")
            return False

    # ------------------------------------------------------------------
    # Document submission helpers
    # ------------------------------------------------------------------

    def _submit_acknowledgment(self, po_number: str, x12_855: str) -> SubmitResult:
        """POST 855 PO acknowledgment to Dsco."""
        # Parse line items from 855 to build the acknowledgment payload
        dsco_order_id = self._resolve_dsco_order_id(po_number)
        if not dsco_order_id:
            return SubmitResult("", ok=False,
                                error=f"Could not resolve Dsco order ID for PO {po_number}")
        try:
            lines = _parse_855_lines(x12_855)
            payload = {
                "lines": [
                    {
                        "lineItemId": line.get("line_id", ""),
                        "acknowledgementType": line.get("ack_type", "accept"),
                        "quantity": line.get("quantity"),
                        "unitCost": line.get("unit_cost"),
                    }
                    for line in lines
                ] if lines else [],
                "poNumber": po_number,
            }
            resp = self._post(f"/order/{dsco_order_id}/acknowledge", payload)
            tx_id = str(resp.get("requestId") or resp.get("id") or f"DSCO-855-{uuid.uuid4().hex[:10]}")
            return SubmitResult(tx_id, ok=True, raw=resp)
        except Exception as exc:
            return SubmitResult("", ok=False, error=str(exc))

    def _submit_shipment(self, po_number: str, x12_856: str) -> SubmitResult:
        """POST 856 ship notice / ASN to Dsco."""
        try:
            fields = _parse_856_fields(x12_856)
            dsco_order_id = self._resolve_dsco_order_id(po_number)
            overrides = self.cfg.get("field_overrides", {})

            payload = {
                "poNumber": po_number,
                "dscoOrderId": dsco_order_id or "",
                "shipDate": fields.get("ship_date", ""),
                "carrier": fields.get("carrier_code", ""),
                "trackingNumber": fields.get("tracking_number", ""),
                "trackingUrl": fields.get("tracking_url", ""),
                "shipFrom": {
                    "name":  overrides.get("ship_from_name", "Jeffco Fibres"),
                    "address1": overrides.get("ship_from_address1", ""),
                    "city":  overrides.get("ship_from_city", ""),
                    "state": overrides.get("ship_from_state", ""),
                    "zip":   overrides.get("ship_from_zip", ""),
                    "country": overrides.get("ship_from_country", "US"),
                },
                "packages": [
                    {
                        "trackingNumber": fields.get("tracking_number", ""),
                        "carrier": fields.get("carrier_code", ""),
                    }
                ],
                "lineItems": _parse_856_line_items(x12_856),
            }
            resp = self._post("/shipment", payload)
            tx_id = str(resp.get("requestId") or resp.get("shipmentId") or f"DSCO-856-{uuid.uuid4().hex[:10]}")
            return SubmitResult(tx_id, ok=True, raw=resp)
        except Exception as exc:
            return SubmitResult("", ok=False, error=str(exc))

    def _submit_invoice(self, po_number: str, x12_810: str) -> SubmitResult:
        """POST 810 invoice to Dsco."""
        try:
            fields = _parse_810_fields(x12_810)
            dsco_order_id = self._resolve_dsco_order_id(po_number)
            overrides = self.cfg.get("field_overrides", {})

            payload = {
                "poNumber": po_number,
                "dscoOrderId": dsco_order_id or "",
                "invoiceNumber": fields.get("invoice_number", ""),
                "invoiceDate":   fields.get("invoice_date", ""),
                "invoiceTotal":  fields.get("total_amount", ""),
                "seller": {
                    "name": overrides.get("ship_from_name", "Jeffco Fibres"),
                },
                "lineItems": _parse_810_line_items(x12_810),
            }
            resp = self._post("/invoice", payload)
            tx_id = str(resp.get("requestId") or resp.get("invoiceId") or f"DSCO-810-{uuid.uuid4().hex[:10]}")
            return SubmitResult(tx_id, ok=True, raw=resp)
        except Exception as exc:
            return SubmitResult("", ok=False, error=str(exc))

    # ------------------------------------------------------------------
    # Inbound order polling
    # ------------------------------------------------------------------

    def _fetch_orders_poll(self) -> list[InboundDocument]:
        """GET /orders — poll for POs created in the last 24 hours."""
        try:
            now   = datetime.now(timezone.utc)
            start = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            resp  = self._get("/orders", params={
                "startCreateDate": start,
                "endCreateDate":   end,
                "ordersPerPage":   100,
            })
            orders = resp.get("orders") or resp.get("data") or []
        except Exception as exc:
            log.error(f"DscoConnector.fetch_orders error: {exc}")
            return []

        docs = []
        for order in orders:
            po   = order.get("poNumber") or order.get("purchaseOrderNumber") or ""
            tx_id = str(order.get("dscoOrderId") or order.get("id") or uuid.uuid4().hex)
            # Prefer raw EDI if Dsco provides it; otherwise synthesize an X12 stub
            raw_x12 = order.get("ediPayload") or _json_order_to_x12_stub(order)
            docs.append(InboundDocument(
                transaction_id=tx_id,
                x12=raw_x12,
                doc_type="850",
                raw=order,
            ))
        return docs

    def _fetch_stream_events(self, stream_id: str) -> list[InboundDocument]:
        """GET /stream/{id}/events — pull from a pre-configured Dsco order stream."""
        try:
            resp   = self._get(f"/stream/{stream_id}/events", params={"maxEvents": 50})
            events = resp.get("events") or []
        except Exception as exc:
            log.error(f"DscoConnector.stream_events error: {exc}")
            return []

        docs = []
        for event in events:
            data  = event.get("data") or event.get("object") or {}
            po    = data.get("poNumber") or data.get("purchaseOrderNumber") or ""
            tx_id = str(data.get("dscoOrderId") or event.get("eventId") or uuid.uuid4().hex)
            raw_x12 = data.get("ediPayload") or _json_order_to_x12_stub(data)
            docs.append(InboundDocument(
                transaction_id=tx_id,
                x12=raw_x12,
                doc_type="850",
                raw=data,
            ))

        # Advance stream cursor so we don't re-process these events
        if events:
            last_pos = events[-1].get("eventId") or events[-1].get("position")
            if last_pos:
                try:
                    self._put(f"/stream/{stream_id}/position", {"position": last_pos})
                except Exception:
                    pass

        return docs

    def _resolve_dsco_order_id(self, po_number: str) -> str | None:
        """Look up the Dsco internal order ID from a PO number."""
        try:
            resp = self._get("/orders", params={"poNumber": po_number, "ordersPerPage": 1})
            orders = resp.get("orders") or []
            if orders:
                return str(orders[0].get("dscoOrderId") or orders[0].get("id") or "")
        except Exception:
            pass
        return None

    def ping(self) -> bool:
        """Test connectivity and auth."""
        try:
            resp = self._get("/hello")
            return bool(resp)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        if self.cfg.get("staging"):
            return _STAGING_BASE
        return self.cfg.get("base_url", _PROD_BASE).rstrip("/")

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(
            self._base_url() + path,
            headers=self._auth_headers(),
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise DscoError(f"Dsco POST {path} → {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            self._base_url() + path,
            headers=self._auth_headers(),
            params=params or {},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise DscoError(f"Dsco GET {path} → {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else {}

    def _put(self, path: str, body: dict) -> dict:
        resp = requests.put(
            self._base_url() + path,
            headers=self._auth_headers(),
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise DscoError(f"Dsco PUT {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"bearer {self._get_token()}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        auth = self.cfg.get("auth", {})
        token_url = auth.get("token_url", "https://api.dsco.io/api/v3/oauth2/token")

        resp = requests.post(
            token_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     auth.get("client_id", ""),
                "client_secret": auth.get("client_secret", ""),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise DscoError(
                f"Dsco token request failed {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        self._token         = data.get("access_token") or data.get("accessToken") or ""
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token or ""


class DscoError(Exception):
    """Dsco API error."""


# ------------------------------------------------------------------
# X12 → Dsco JSON converters
# ------------------------------------------------------------------

def _extract_po_number(x12: str) -> str:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "BEG" and len(elems) > 3:
            return elems[3].strip()
        if tag == "BIG" and len(elems) > 3:
            return elems[3].strip()
        if tag == "BSN" and len(elems) > 2:
            return elems[2].strip()
    return ""


def _parse_855_lines(x12: str) -> list[dict]:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    lines = []
    current: dict = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "PO1" and len(elems) > 4:
            if current:
                lines.append(current)
            current = {
                "line_id":   elems[1].strip() if len(elems) > 1 else "",
                "quantity":  elems[2].strip() if len(elems) > 2 else "",
                "unit_cost": elems[4].strip() if len(elems) > 4 else "",
                "ack_type":  "accept",
            }
        elif tag == "ACK" and len(elems) > 2:
            status_map = {"IA": "accept", "IB": "backorder", "IR": "reject",
                          "IC": "accept", "ID": "reject"}
            current["ack_type"] = status_map.get(elems[1].upper(), "accept")
            if len(elems) > 2:
                current["quantity"] = elems[2].strip()
    if current:
        lines.append(current)
    return lines


def _parse_856_fields(x12: str) -> dict:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    fields: dict[str, str] = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "BSN" and len(elems) > 2:
            fields["ship_date"] = elems[2].strip()
        elif tag == "TD5" and len(elems) > 2:
            fields["carrier_code"] = elems[2].strip()
        elif tag == "REF" and len(elems) > 2:
            q = elems[1].upper()
            if q in ("BM", "CN", "PRO", "AO"):
                fields["tracking_number"] = elems[2].strip()
            elif q == "TN":
                fields["tracking_url"] = elems[2].strip()
    return fields


def _parse_856_line_items(x12: str) -> list[dict]:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    items = []
    current: dict = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "LIN" and len(elems) > 2:
            if current:
                items.append(current)
            current = {"lineItemId": elems[1].strip()}
            if len(elems) > 3:
                current["sku"] = elems[3].strip()
        elif tag == "SN1" and len(elems) > 2 and current:
            current["quantityShipped"] = elems[2].strip()
    if current:
        items.append(current)
    return items


def _parse_810_fields(x12: str) -> dict:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    fields: dict[str, str] = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "BIG" and len(elems) > 3:
            fields["invoice_date"]   = elems[1].strip()
            fields["invoice_number"] = elems[2].strip()
        elif tag == "TDS" and len(elems) > 1:
            # TDS01 is total invoice amount in cents → convert
            raw = elems[1].strip()
            try:
                fields["total_amount"] = str(int(raw) / 100)
            except ValueError:
                fields["total_amount"] = raw
    return fields


def _parse_810_line_items(x12: str) -> list[dict]:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    items = []
    current: dict = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "IT1" and len(elems) > 4:
            if current:
                items.append(current)
            current = {
                "lineItemId":  elems[1].strip(),
                "quantity":    elems[2].strip(),
                "unitOfMeasure": elems[3].strip(),
                "unitPrice":   elems[4].strip(),
            }
            # IT1-07 onward may have qualifier/value pairs
            i = 5
            while i + 1 < len(elems):
                q, v = elems[i].upper(), elems[i + 1].strip()
                if q == "BP":
                    current["buyerSku"] = v
                elif q == "VP":
                    current["supplierSku"] = v
                elif q == "UP":
                    current["upc"] = v
                i += 2
    if current:
        items.append(current)
    return items


def _json_order_to_x12_stub(order: dict) -> str:
    """Convert a Dsco JSON order object to a minimal X12 850 stub.

    Used when Dsco doesn't return raw EDI. The brain's spec-generator
    will refine this on the first live transaction.
    """
    po   = order.get("poNumber") or order.get("purchaseOrderNumber") or "UNKNOWN"
    date = (order.get("createDate") or order.get("orderDate") or "2025-01-01")
    date = date.replace("-", "")[:8]
    lines = order.get("lineItems") or order.get("items") or []
    po1_segs = ""
    for i, item in enumerate(lines[:50], 1):
        qty   = item.get("quantity", 1)
        price = item.get("expectedCost") or item.get("unitCost") or "0"
        sku   = item.get("supplierSku") or item.get("sku") or ""
        upc   = item.get("upc") or ""
        po1_segs += f"PO1*{i}*{qty}*EA*{price}*PE*VP*{sku}*UP*{upc}~"

    return (
        f"ISA*00*          *00*          *ZZ*DSCO           *ZZ*JEFFCOFIBRES   "
        f"*{date[:6]}*1200*U*00401*000000001*0*P*>~"
        f"GS*PO*DSCO*JEFFCOFIBRES*{date}*1200*1*X*004010~"
        f"ST*850*0001~"
        f"BEG*00*SA*{po}**{date}~"
        + po1_segs +
        f"CTT*{len(lines)}~SE*{4 + len(lines)}*0001~GE*1*1~IEA*1*000000001~"
    )
