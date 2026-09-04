"""Shipping connector — ShipStation REST API (Phase 2).

Pulls shipped/tracking data per PO and normalizes it to the internal shape the
856/810 generators expect (ship date/time, X12 carrier code, tracking numbers,
and per-package weights + line quantities for split shipments).

Auth is HTTP Basic with base64(``API_KEY:API_SECRET``). Runs in a dry mode
(``get_shipment`` returns ``None``) when keys aren't configured, so the
pipeline stays exercisable locally.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Callable, Optional

import requests

from ..config import config

logger = logging.getLogger(__name__)

# ShipStation carrierCode -> X12 (SCAC-style) carrier code.
CARRIER_MAP = {
    "ups": "UPSN",
    "ups_walleted": "UPSN",
    "fedex": "FDXG",
    "fedex_international": "FDXG",
    "usps": "USPS",
    "stamps_com": "USPS",
    "dhl_express": "DHLC",
    "ontrac": "ONTC",
}


class ShippingError(Exception):
    """Raised on a non-2xx response from ShipStation."""


class ShippingConnector:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else config.SHIPSTATION_API_KEY
        self.api_secret = api_secret if api_secret is not None else config.SHIPSTATION_API_SECRET
        self.base_url = (base_url or config.SHIPSTATION_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _headers(self) -> dict:
        token = base64.b64encode(f"{self.api_key}:{self.api_secret}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def get_shipment(self, po_number: str) -> Optional[dict]:
        """Return normalized ship data for a PO, or None if not shipped yet.

        Shape::

            {
              "ship_date": "YYYYMMDD",
              "ship_time": "HHMM",
              "carrier_code": "UPSN",          # X12 code via CARRIER_MAP
              "service_level": "GROUND",
              "tracking_numbers": ["1Z..."],
              "packages": [
                {"tracking": "1Z...", "weight_lbs": 12.5,
                 "lines": [{"line_num": "1", "qty_shipped": 10}]}
              ],
            }
        """
        if not self.configured:
            return None
        url = f"{self.base_url}/shipments"
        for attempt in range(4):
            try:
                resp = requests.get(url, headers=self._headers(),
                                    params={"orderNumber": po_number}, timeout=30)
            except requests.RequestException as exc:
                raise ShippingError(f"Network error fetching shipment {po_number}: {exc}") from exc
            if resp.status_code == 429:  # rate limited: back off and retry
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 16))
                continue
            if resp.status_code >= 400:
                raise ShippingError(
                    f"ShipStation {resp.status_code} for {po_number}: {resp.text}")
            shipments = (resp.json() or {}).get("shipments") or []
            shipped = [s for s in shipments if s.get("shipDate")]
            if not shipped:
                return None
            return self._normalize(shipped)
        raise ShippingError(f"ShipStation rate limit exceeded for {po_number}")

    def get_tracking(self, po_number: str) -> list:
        shipment = self.get_shipment(po_number)
        return list(shipment.get("tracking_numbers", [])) if shipment else []

    def watch_for_shipment(self, po_number: str, callback: Callable[[dict], None],
                           interval: int = 60, max_polls: int = 120) -> None:
        """Poll until the PO has a ship record, then fire ``callback(shipment)``.

        Intended to run in a background thread/task. Bounded by ``max_polls``.
        """
        for _ in range(max_polls):
            try:
                shipment = self.get_shipment(po_number)
            except ShippingError as exc:
                logger.warning("watch_for_shipment %s: %s", po_number, exc)
                shipment = None
            if shipment:
                callback(shipment)
                return
            time.sleep(interval)

    @staticmethod
    def _normalize(shipments: list) -> dict:
        """Map one or more ShipStation shipment records to the internal shape."""
        first = shipments[0]
        ship_date = _to_ccyymmdd(first.get("shipDate"))
        ship_time = _to_hhmm(first.get("shipDate"))
        carrier_code = CARRIER_MAP.get((first.get("carrierCode") or "").lower(), "")
        service_level = (first.get("serviceCode") or "").upper()

        tracking_numbers, packages = [], []
        for s in shipments:
            tracking = s.get("trackingNumber")
            if tracking:
                tracking_numbers.append(tracking)
            weight = (s.get("weight") or {}).get("value")
            lines = [
                {"line_num": str(item.get("lineItemKey") or i + 1),
                 "qty_shipped": item.get("quantity", 0)}
                for i, item in enumerate(s.get("shipmentItems") or [])
            ]
            packages.append({
                "tracking": tracking,
                "weight_lbs": float(weight) if weight is not None else None,
                "lines": lines,
            })

        return {
            "ship_date": ship_date,
            "ship_time": ship_time,
            "carrier_code": carrier_code,
            "service_level": service_level,
            "tracking_numbers": tracking_numbers,
            "packages": packages,
        }


def _to_ccyymmdd(value: Optional[str]) -> Optional[str]:
    """ShipStation dates look like '2026-05-28' or '2026-05-28T14:00:00'."""
    if not value:
        return None
    date_part = value.split("T")[0]
    return date_part.replace("-", "")[:8] or None


def _to_hhmm(value: Optional[str]) -> str:
    if value and "T" in value:
        t = value.split("T")[1]
        return (t[:2] + t[3:5]) if len(t) >= 5 else "1200"
    return "1200"
