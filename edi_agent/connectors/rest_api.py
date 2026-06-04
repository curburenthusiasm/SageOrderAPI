"""connectors/rest_api.py — Direct REST API connector for partners with their own APIs.

When a trading partner exposes a REST/JSON API (instead of speaking raw X12 EDI),
this connector maps the internal Order model to their API schema and submits directly —
no Tray.io, no Orderful, no middleware.

Configuration (stored in partner registry connector_config JSONB):
{
    "base_url": "https://api.partner.com/v1",
    "auth": {
        "type": "bearer" | "api_key" | "basic" | "oauth2_client_credentials",
        "token": "...",           # for bearer / api_key
        "header": "X-API-Key",   # for api_key (default: Authorization)
        "client_id": "...",       # for oauth2
        "client_secret": "...",   # for oauth2
        "token_url": "..."        # for oauth2
    },
    "endpoints": {
        "submit_order": "POST /orders",
        "get_status":   "GET /orders/{order_id}",
        "acknowledge":  "POST /orders/{order_id}/ack"
    },
    "field_map": {
        // EDI field → partner API field mappings
        // e.g. "BEG03" → "purchase_order_number"
        "BEG03":   "purchase_order_number",
        "BEG05":   "order_date",
        "N102":    "buyer_name",
        "PO1_02":  "quantity",
        "PO1_04":  "unit_price",
        "PO1_07":  "item_id"
    },
    "inbound_poll": {             // optional — for pulling inbound docs
        "endpoint": "GET /orders/inbound",
        "status_filter": "pending",
        "x12_field": "edi_payload"   // field in response containing raw X12 (if any)
    }
}
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import requests

from .base import ConnectorBase, InboundDocument, SubmitResult

log = logging.getLogger(__name__)


class RestApiConnector(ConnectorBase):
    """Direct REST API transport connector for partners with native APIs."""

    PLATFORM = "rest_api"

    def __init__(self, connector_config: dict):
        self.cfg = connector_config or {}
        self._oauth_token: str | None = None
        self._oauth_expires: float = 0.0

    # ------------------------------------------------------------------
    # ConnectorBase interface
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(
            self.cfg.get("base_url")
            and self.cfg.get("auth")
            and self.cfg.get("endpoints")
        )

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> SubmitResult:
        """Map EDI order to partner API schema and submit."""
        if not self.configured:
            return SubmitResult("", ok=False, error="RestApiConnector not configured")

        endpoint_template = self.cfg.get("endpoints", {}).get("submit_order", "")
        if not endpoint_template:
            return SubmitResult("", ok=False, error="No submit_order endpoint configured")

        # Parse the X12 string into key fields for mapping
        fields = _extract_x12_fields(x12_string)
        payload = _map_fields(fields, self.cfg.get("field_map", {}))
        payload.setdefault("raw_x12", x12_string)  # include raw as fallback
        payload.setdefault("doc_type", doc_type)

        method, path = _parse_endpoint(endpoint_template)
        url = self.cfg["base_url"].rstrip("/") + "/" + path.lstrip("/")

        try:
            resp = requests.request(
                method,
                url,
                headers=self._build_headers(),
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            return SubmitResult("", ok=False, error=str(exc))

        if resp.status_code >= 400:
            return SubmitResult(
                "", ok=False,
                error=f"Partner API returned {resp.status_code}: {resp.text[:300]}"
            )

        try:
            raw = resp.json()
        except ValueError:
            raw = {"text": resp.text}

        tx_id = (
            str(raw.get("id") or raw.get("order_id") or raw.get("transaction_id"))
            or f"REST-{doc_type}-{uuid.uuid4().hex[:10]}"
        )
        return SubmitResult(transaction_id=tx_id, ok=True, raw=raw)

    def fetch_inbound(self, doc_type: str = "850", status: str = "received") -> list[InboundDocument]:
        poll_cfg = self.cfg.get("inbound_poll")
        if not poll_cfg or not self.configured:
            return []

        method, path = _parse_endpoint(poll_cfg.get("endpoint", "GET /inbound"))
        params = {}
        sf = poll_cfg.get("status_filter")
        if sf:
            params["status"] = sf

        url = self.cfg["base_url"].rstrip("/") + "/" + path.lstrip("/")
        try:
            resp = requests.request(
                method, url,
                headers=self._build_headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json()
            if isinstance(items, dict):
                items = items.get("orders") or items.get("data") or []
        except Exception as exc:
            log.error(f"RestApiConnector.fetch_inbound error: {exc}")
            return []

        x12_field = poll_cfg.get("x12_field", "edi_payload")
        docs = []
        for item in items:
            x12 = item.get(x12_field) or item.get("x12") or ""
            if x12:
                docs.append(InboundDocument(
                    transaction_id=str(item.get("id") or uuid.uuid4().hex),
                    x12=x12,
                    doc_type=doc_type,
                    raw=item,
                ))
        return docs

    def acknowledge(self, transaction_id: str, status: str = "DELIVERED") -> bool:
        ack_template = self.cfg.get("endpoints", {}).get("acknowledge", "")
        if not ack_template or not self.configured:
            return False

        method, path = _parse_endpoint(ack_template)
        path = path.replace("{order_id}", transaction_id).replace("{id}", transaction_id)
        url = self.cfg["base_url"].rstrip("/") + "/" + path.lstrip("/")
        try:
            resp = requests.request(
                method, url,
                headers=self._build_headers(),
                json={"status": status},
                timeout=15,
            )
            return resp.status_code < 400
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict:
        auth = self.cfg.get("auth", {})
        auth_type = auth.get("type", "bearer")
        headers = {"Content-Type": "application/json"}

        if auth_type == "bearer":
            token = auth.get("token", "")
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key":
            header_name = auth.get("header", "X-API-Key")
            headers[header_name] = auth.get("token", "")
        elif auth_type == "basic":
            import base64
            creds = base64.b64encode(
                f"{auth.get('username','')}:{auth.get('password','')}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {creds}"
        elif auth_type == "oauth2_client_credentials":
            token = self._get_oauth_token(auth)
            headers["Authorization"] = f"Bearer {token}"

        return headers

    def _get_oauth_token(self, auth: dict) -> str:
        if self._oauth_token and time.time() < self._oauth_expires - 60:
            return self._oauth_token

        token_url = auth.get("token_url", "")
        if not token_url:
            return ""

        try:
            resp = requests.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": auth.get("client_id", ""),
                    "client_secret": auth.get("client_secret", ""),
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._oauth_token = data.get("access_token", "")
            self._oauth_expires = time.time() + int(data.get("expires_in", 3600))
            return self._oauth_token or ""
        except Exception as exc:
            log.error(f"OAuth2 token fetch failed: {exc}")
            return ""


# ------------------------------------------------------------------
# Field extraction / mapping helpers
# ------------------------------------------------------------------

def _extract_x12_fields(x12_string: str) -> dict:
    """Minimal X12 field extractor — pulls the most-used EDI fields.

    For full parsing the brain uses edi_agent/core/parser.py; this
    lightweight version is used for REST API field mapping only.
    """
    fields: dict[str, str] = {}
    seg_sep = "~"
    elem_sep = "*"

    # Auto-detect separators from ISA segment
    if x12_string.startswith("ISA"):
        elem_sep = x12_string[3]
        seg_sep = x12_string[105] if len(x12_string) > 105 else "~"

    for segment in x12_string.split(seg_sep):
        segment = segment.strip()
        if not segment:
            continue
        elems = segment.split(elem_sep)
        tag = elems[0].upper()

        if tag == "BEG" and len(elems) > 5:
            fields["BEG02"] = elems[2] if len(elems) > 2 else ""  # order type
            fields["BEG03"] = elems[3] if len(elems) > 3 else ""  # PO number
            fields["BEG05"] = elems[5] if len(elems) > 5 else ""  # order date
        elif tag == "N1" and len(elems) > 2:
            qualifier = elems[1].upper()
            if qualifier == "BY":  # buyer
                fields["N102_BY"] = elems[2]
            elif qualifier == "ST":  # ship-to
                fields["N102_ST"] = elems[2]
            elif qualifier == "SE":  # seller
                fields["N102_SE"] = elems[2]
        elif tag == "PO1" and len(elems) > 4:
            # Store first PO1 for mapping; full line parsing via core/parser.py
            fields.setdefault("PO1_02", elems[2])   # quantity
            fields.setdefault("PO1_03", elems[3])   # unit of measure
            fields.setdefault("PO1_04", elems[4])   # price
            if len(elems) > 7:
                fields.setdefault("PO1_07", elems[7])  # item ID qualifier
                fields.setdefault("PO1_08", elems[8] if len(elems) > 8 else "")
        elif tag == "DTM" and len(elems) > 2:
            qualifier = elems[1].upper()
            fields[f"DTM_{qualifier}"] = elems[2]

    return fields


def _map_fields(x12_fields: dict, field_map: dict) -> dict:
    """Apply a partner field_map to extracted X12 fields."""
    payload = {}
    for x12_key, api_key in field_map.items():
        value = x12_fields.get(x12_key)
        if value is not None:
            # Support nested keys: "order.purchase_order_number"
            _set_nested(payload, api_key, value)
    return payload


def _set_nested(d: dict, dotted_key: str, value: Any) -> None:
    keys = dotted_key.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _parse_endpoint(template: str) -> tuple[str, str]:
    """Parse 'POST /orders' → ('POST', '/orders')."""
    parts = template.strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0].upper(), parts[1]
    return "POST", parts[0]
