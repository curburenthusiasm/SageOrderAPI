"""connectors/rithum.py — Rithum (formerly CommerceHub) drop-ship connector.

Rithum is a retail supply-chain platform used by Target, Wayfair, and others.
Suppliers submit ASNs and invoices through their API after receiving POs.

Auth: OAuth2 client_credentials
Docs: https://developer.rithum.com  (get your client_id/secret from
      your Rithum supplier portal → Settings → API Credentials)

Configuration (stored in partner registry connector_config JSONB):
{
    "supplier_id": "your-rithum-supplier-id",
    "auth": {
        "type":          "oauth2_client_credentials",
        "client_id":     "...",
        "client_secret": "...",
        "token_url":     "https://auth.rithum.com/oauth/token"
    },
    "base_url": "https://api.rithum.com/v1",
    "retailer_id": "target",                  // or wayfair, walmart, etc.
    "doc_types": ["850", "856", "810"],
    // Optional field overrides per-partner
    "field_overrides": {
        "ship_from_name": "Jeffco Fibres",
        "ship_from_gln":  "0012345678901"
    }
}
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import requests

from .base import ConnectorBase, InboundDocument, SubmitResult

log = logging.getLogger(__name__)

# Default Rithum API base — may differ by environment (staging vs prod)
_DEFAULT_BASE_URL = "https://api.rithum.com/v1"
_DEFAULT_TOKEN_URL = "https://auth.rithum.com/oauth/token"


class RithumConnector(ConnectorBase):
    """Rithum drop-ship REST API connector."""

    PLATFORM = "rithum"

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
        return bool(
            self.cfg.get("supplier_id")
            and auth.get("client_id")
            and auth.get("client_secret")
        )

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> SubmitResult:
        """Submit an outbound document to Rithum.

        - 997 → skip (Rithum handles functional ACKs internally)
        - 855 → POST /orders/{po}/acknowledgment
        - 856 → POST /orders/{po}/fulfillments
        - 810 → POST /orders/{po}/invoices
        """
        if not self.configured:
            return SubmitResult("", ok=False,
                                error="RithumConnector not configured — set supplier_id + auth creds")

        doc_type = doc_type.upper()

        if doc_type == "997":
            # Rithum manages functional ACKs internally; nothing to submit
            return SubmitResult(f"RITHUM-997-NOOP-{uuid.uuid4().hex[:8]}", ok=True,
                                raw={"note": "997 not required — Rithum handles ACKs internally"})

        # Parse minimal fields from X12 for routing
        po_number = _extract_po_number(x12_string)
        if not po_number:
            return SubmitResult("", ok=False, error="Could not extract PO number from X12 payload")

        if doc_type == "855":
            return self._submit_acknowledgment(po_number, x12_string)
        elif doc_type == "856":
            return self._submit_fulfillment(po_number, x12_string)
        elif doc_type == "810":
            return self._submit_invoice(po_number, x12_string)
        else:
            return SubmitResult("", ok=False, error=f"Rithum connector: unsupported doc type {doc_type}")

    def fetch_inbound(self, doc_type: str = "850", status: str = "new") -> list[InboundDocument]:
        """Poll Rithum for new inbound purchase orders."""
        if not self.configured:
            return []

        try:
            resp = self._get(
                "/orders",
                params={"status": status, "retailer": self.cfg.get("retailer_id", "")},
            )
            orders = resp.get("orders") or resp.get("data") or []
        except Exception as exc:
            log.error(f"RithumConnector.fetch_inbound error: {exc}")
            return []

        docs = []
        for order in orders:
            # Rithum can return either raw X12 or JSON order objects.
            # Raw X12 is under `edi_payload`; JSON orders are converted to X12 on the fly.
            raw_x12 = order.get("edi_payload") or order.get("x12_850") or ""
            if not raw_x12:
                raw_x12 = _json_order_to_x12_stub(order)

            docs.append(InboundDocument(
                transaction_id=str(order.get("id") or order.get("order_id") or uuid.uuid4().hex),
                x12=raw_x12,
                doc_type="850",
                raw=order,
            ))
        return docs

    def acknowledge(self, transaction_id: str, status: str = "ACCEPTED") -> bool:
        """Mark an inbound PO as acknowledged."""
        if not self.configured:
            return False
        try:
            self._post(f"/orders/{transaction_id}/status", {"status": status.lower()})
            return True
        except Exception as exc:
            log.error(f"RithumConnector.acknowledge error: {exc}")
            return False

    def supports_workflow_creation(self) -> bool:
        return False  # Rithum workflows are native Python, not Rithum-side configs

    # ------------------------------------------------------------------
    # Document submission helpers
    # ------------------------------------------------------------------

    def _submit_acknowledgment(self, po_number: str, x12_855: str) -> SubmitResult:
        """POST 855 PO acknowledgment to Rithum."""
        try:
            payload = _build_acknowledgment_payload(po_number, x12_855, self.cfg)
            resp = self._post(f"/orders/{po_number}/acknowledgment", payload)
            tx_id = str(resp.get("acknowledgment_id") or resp.get("id") or f"RITHUM-855-{uuid.uuid4().hex[:10]}")
            return SubmitResult(tx_id, ok=True, raw=resp)
        except Exception as exc:
            return SubmitResult("", ok=False, error=str(exc))

    def _submit_fulfillment(self, po_number: str, x12_856: str) -> SubmitResult:
        """POST 856 ship notice (ASN) to Rithum."""
        try:
            payload = _build_fulfillment_payload(po_number, x12_856, self.cfg)
            resp = self._post(f"/orders/{po_number}/fulfillments", payload)
            tx_id = str(resp.get("fulfillment_id") or resp.get("id") or f"RITHUM-856-{uuid.uuid4().hex[:10]}")
            return SubmitResult(tx_id, ok=True, raw=resp)
        except Exception as exc:
            return SubmitResult("", ok=False, error=str(exc))

    def _submit_invoice(self, po_number: str, x12_810: str) -> SubmitResult:
        """POST 810 invoice to Rithum."""
        try:
            payload = _build_invoice_payload(po_number, x12_810, self.cfg)
            resp = self._post(f"/orders/{po_number}/invoices", payload)
            tx_id = str(resp.get("invoice_id") or resp.get("id") or f"RITHUM-810-{uuid.uuid4().hex[:10]}")
            return SubmitResult(tx_id, ok=True, raw=resp)
        except Exception as exc:
            return SubmitResult("", ok=False, error=str(exc))

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return self.cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(
            self._base_url() + path,
            headers=self._auth_headers(),
            json=body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RithumError(
                f"Rithum {path} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            self._base_url() + path,
            headers=self._auth_headers(),
            params=params or {},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RithumError(
                f"Rithum GET {path} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "X-Rithum-Supplier-Id": self.cfg.get("supplier_id", ""),
        }

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        auth = self.cfg.get("auth", {})
        token_url = auth.get("token_url", _DEFAULT_TOKEN_URL)

        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": auth.get("client_id", ""),
                "client_secret": auth.get("client_secret", ""),
                "scope": auth.get("scope", "supplier:read supplier:write"),
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            raise RithumError(
                f"Rithum token request failed {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        self._token = data.get("access_token", "")
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token or ""


class RithumError(Exception):
    """Rithum API error."""


# ------------------------------------------------------------------
# Payload builders  (X12 → Rithum JSON)
# ------------------------------------------------------------------

def _build_acknowledgment_payload(po_number: str, x12_855: str, cfg: dict) -> dict:
    """Convert 855 X12 string to Rithum acknowledgment JSON."""
    overrides = cfg.get("field_overrides", {})
    return {
        "purchase_order_number": po_number,
        "acknowledgment_type": "accepted",
        "supplier_id": cfg.get("supplier_id", ""),
        "retailer_id": cfg.get("retailer_id", ""),
        "x12_payload": x12_855,  # Rithum accepts raw X12 as fallback
        **{k: v for k, v in overrides.items() if k.startswith("ack_")},
    }


def _build_fulfillment_payload(po_number: str, x12_856: str, cfg: dict) -> dict:
    """Convert 856 X12 to Rithum fulfillment/ASN JSON."""
    overrides = cfg.get("field_overrides", {})
    # Extract key fields from the 856
    fields = _extract_856_fields(x12_856)
    return {
        "purchase_order_number": po_number,
        "supplier_id": cfg.get("supplier_id", ""),
        "retailer_id": cfg.get("retailer_id", ""),
        "ship_date": fields.get("ship_date", ""),
        "carrier_code": fields.get("carrier_code", ""),
        "tracking_number": fields.get("tracking_number", ""),
        "ship_from": {
            "name": overrides.get("ship_from_name", cfg.get("ship_from_name", "Jeffco Fibres")),
            "gln": overrides.get("ship_from_gln", ""),
        },
        "x12_payload": x12_856,
    }


def _build_invoice_payload(po_number: str, x12_810: str, cfg: dict) -> dict:
    """Convert 810 X12 to Rithum invoice JSON."""
    overrides = cfg.get("field_overrides", {})
    fields = _extract_810_fields(x12_810)
    return {
        "purchase_order_number": po_number,
        "supplier_id": cfg.get("supplier_id", ""),
        "retailer_id": cfg.get("retailer_id", ""),
        "invoice_number": fields.get("invoice_number", ""),
        "invoice_date": fields.get("invoice_date", ""),
        "total_amount": fields.get("total_amount", ""),
        "x12_payload": x12_810,
    }


# ------------------------------------------------------------------
# X12 mini-parsers (just the fields Rithum payloads need)
# ------------------------------------------------------------------

def _extract_po_number(x12: str) -> str:
    """Pull PO number from 850/855/856/810 BEG or BIG segment."""
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    seg_sep = "~"
    for seg in x12.split(seg_sep):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "BEG" and len(elems) > 3:
            return elems[3].strip()
        if tag == "BIG" and len(elems) > 3:
            return elems[3].strip()
        if tag == "BSN" and len(elems) > 2:
            return elems[2].strip()
    return ""


def _extract_856_fields(x12: str) -> dict:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    fields: dict[str, str] = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "BSN" and len(elems) > 2:
            fields["ship_date"] = elems[2]
        elif tag == "TD5" and len(elems) > 2:
            fields["carrier_code"] = elems[2]
        elif tag == "REF" and len(elems) > 2 and elems[1].upper() in ("BM", "CN", "PRO"):
            fields["tracking_number"] = elems[2]
    return fields


def _extract_810_fields(x12: str) -> dict:
    sep = x12[3] if x12.startswith("ISA") and len(x12) > 3 else "*"
    fields: dict[str, str] = {}
    for seg in x12.split("~"):
        elems = seg.strip().split(sep)
        tag = elems[0].upper() if elems else ""
        if tag == "BIG" and len(elems) > 3:
            fields["invoice_date"] = elems[1]
            fields["invoice_number"] = elems[2]
        elif tag == "TDS" and len(elems) > 1:
            fields["total_amount"] = elems[1]
    return fields


def _json_order_to_x12_stub(order: dict) -> str:
    """Best-effort JSON → X12 850 stub when Rithum sends JSON instead of EDI.

    Only used when Rithum doesn't return raw X12. The brain's spec-generator
    will refine this on the first correction pass.
    """
    po = order.get("purchase_order_number") or order.get("po_number") or "UNKNOWN"
    date = (order.get("order_date") or "20250101").replace("-", "")[:8]
    return (
        f"ISA*00*          *00*          *ZZ*RITHUM         *ZZ*JEFFCOFIBRES   "
        f"*{date[:6]}*1200*U*00401*000000001*0*P*>~"
        f"GS*PO*RITHUM*JEFFCOFIBRES*{date}*1200*1*X*004010~"
        f"ST*850*0001~"
        f"BEG*00*SA*{po}**{date}~"
        f"CTT*0~SE*4*0001~GE*1*1~IEA*1*000000001~"
    )
