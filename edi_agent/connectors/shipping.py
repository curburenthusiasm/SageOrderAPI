"""Shipping API connector — pull tracking / ASN data (Phase 2).

Generic HTTP client for whichever shipping/3PL API provides tracking numbers
and ship details per order. Runs in a dry mode (returns empty results) when
``SHIPPING_API_KEY`` isn't configured, so the pipeline stays exercisable
locally. Point ``SHIPPING_API_BASE_URL`` at the real endpoint to go live.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from ..config import config

logger = logging.getLogger(__name__)

SHIPPING_BASE_URL = os.environ.get("SHIPPING_API_BASE_URL", "")


class ShippingError(Exception):
    """Raised on a non-2xx response from the shipping API."""


class ShippingClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else config.SHIPPING_API_KEY
        self.base_url = (base_url if base_url is not None else SHIPPING_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    def get_shipment(self, po_number: str) -> dict:
        """Return ship details for a PO.

        Shape::

            {
              "ship_date": "YYYYMMDD",
              "ship_time": "HHMM",
              "carrier_code": "UPSN",
              "ship_method": "GROUND",
              "tracking_numbers": ["1Z..."],
            }

        Returns an empty dict in dry mode (unconfigured).
        """
        if not self.configured:
            return {}
        url = f"{self.base_url}/shipments/{po_number}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise ShippingError(f"Network error fetching shipment {po_number}: {exc}") from exc
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            raise ShippingError(f"Shipping API {resp.status_code} for {po_number}: {resp.text}")
        return self._normalize(resp.json())

    def get_tracking(self, po_number: str) -> list:
        """Return just the tracking numbers for a PO (empty list in dry mode)."""
        return list(self.get_shipment(po_number).get("tracking_numbers", []))

    @staticmethod
    def _normalize(data: dict) -> dict:
        """Map a provider payload to our internal shipment shape.

        Tolerant of common field-name variants so this works against more than
        one provider without code changes.
        """
        def pick(*keys):
            for k in keys:
                if data.get(k):
                    return data[k]
            return None

        tracking = pick("tracking_numbers", "trackingNumbers", "tracking") or []
        if isinstance(tracking, str):
            tracking = [tracking]
        return {
            "ship_date": pick("ship_date", "shipDate"),
            "ship_time": pick("ship_time", "shipTime") or "1200",
            "carrier_code": pick("carrier_code", "carrierCode", "carrier"),
            "ship_method": pick("ship_method", "shipMethod", "service"),
            "tracking_numbers": list(tracking),
        }
