"""ROI InSynch connector — write orders into Sage 100 (go-live import path).

This is the Phase-1 "import to Sage" step from the LogicBroker pattern, done
against the ROI InSynch REST API (POST /api/v2/sales_order_headers). It maps a
parsed 850 (our internal Order) into the ROI SalesOrderHeader payload and posts
it with an Azure AD client-credentials bearer token (same identity as main.py;
all values come from the environment).

Optional / safe: if AZURE_CLIENT_ID / AZURE_CLIENT_SECRET aren't set it runs in
a dry mode that returns the payload it *would* send, so the pipeline stays
exercisable without touching Sage.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from ..config import config
from ..mappings import price_for

logger = logging.getLogger(__name__)


class RoiInsynchError(Exception):
    """Raised on a non-2xx response from the ROI InSynch API."""


class RoiInsynchClient:
    def __init__(self):
        self.base_url = config.ROI_BASE_URL.rstrip("/")
        self.company_code = config.ROI_COMPANY_CODE
        self.scope = config.ROI_API_SCOPE
        self.token_url = config.ROI_TOKEN_URL
        self.client_id = config.AZURE_CLIENT_ID
        self.client_secret = config.AZURE_CLIENT_SECRET
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # --- auth -------------------------------------------------------------
    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        resp = requests.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RoiInsynchError(f"Token request failed {resp.status_code}: {resp.text}")
        data = resp.json()
        self._token = data.get("access_token", "")
        self._token_exp = now + int(data.get("expires_in", 3600))
        return self._token

    # --- payload mapping --------------------------------------------------
    def build_payload(self, order, mappings: dict) -> dict:
        """Map a parsed Order + human mappings into a ROI SalesOrderHeader."""
        ship = order.ship_to
        bill = order.bill_to or order.buyer
        sku_to_item = mappings.get("sku_to_item") or {}

        def item_code(line) -> str:
            for key in (line.vendor_part, line.buyer_part, line.upc):
                if key and key in sku_to_item:
                    return sku_to_item[key]
            return line.vendor_part or line.buyer_part or line.upc or ""

        details = []
        for line in order.lines:
            details.append(_strip({
                "CompanyCode": self.company_code,
                "SageOperationType": 1,
                "ItemCode": item_code(line),
                "ItemCodeDesc": line.description,
                "QuantityOrdered": float(line.qty_ordered or 0),
                "UnitOfMeasure": line.uom or "EA",
                "UnitPrice": float(price_for(mappings, line)),
                "WarehouseCode": mappings.get("warehouse_code") or config.ROI_WAREHOUSE_CODE,
            }))

        header = _strip({
            "ExternalProviderId": order.partner_isa_id or "ORDERFUL",
            "UpdateProvider": True,
            "SageOperationType": 1,                 # 1 = create
            "CompanyCode": self.company_code,
            "OrderType": "S",
            "OrderDate": order.po_date,
            "CustomerPONo": order.po_number,
            "ARDivisionNo": mappings.get("ar_division_no") or config.ROI_AR_DIVISION_NO,
            "CustomerNo": mappings.get("sage_customer_no") or config.ROI_CUSTOMER_NO,
            "WarehouseCode": mappings.get("warehouse_code") or config.ROI_WAREHOUSE_CODE,
            "ShipVia": mappings.get("ship_via") or order.carrier or "",
            "TermsCode": mappings.get("terms_code") or "",
            "BillToName": getattr(bill, "name", "") if bill else "",
            "BillToAddress1": getattr(bill, "address", "") if bill else "",
            "BillToCity": getattr(bill, "city", "") if bill else "",
            "BillToState": getattr(bill, "state", "") if bill else "",
            "BillToZipCode": getattr(bill, "zip", "") if bill else "",
            "ShipToName": getattr(ship, "name", "") if ship else "",
            "ShipToAddress1": getattr(ship, "address", "") if ship else "",
            "ShipToCity": getattr(ship, "city", "") if ship else "",
            "ShipToState": getattr(ship, "state", "") if ship else "",
            "ShipToZipCode": getattr(ship, "zip", "") if ship else "",
            "Comment": f"Imported from EDI 850 PO {order.po_number}",
            "SalesOrderDetails": details,
        })
        return header

    # --- import -----------------------------------------------------------
    def import_sales_order(self, order, mappings: dict) -> dict:
        """Create the Sage sales order. Dry-runs when no credentials are set."""
        payload = self.build_payload(order, mappings)
        if not self.configured:
            return {"ok": True, "dry_run": True,
                    "sales_order_no": f"SIMULATED-SO-{order.po_number}",
                    "payload": payload}

        url = f"{self.base_url}/api/v2/sales_order_headers"
        try:
            token = self._get_token()
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise RoiInsynchError(f"Network error importing PO {order.po_number}: {exc}") from exc
        if resp.status_code >= 400:
            raise RoiInsynchError(
                f"ROI InSynch {resp.status_code} for PO {order.po_number}: {resp.text}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return {"ok": True, "dry_run": False,
                "sales_order_no": data.get("SalesOrderNo") if isinstance(data, dict) else None,
                "response": data}


def _strip(d: dict) -> dict:
    """Drop empty/None values so we send a clean, minimal payload."""
    out = {}
    for k, v in d.items():
        if v in (None, "", []):
            continue
        out[k] = v
    return out
