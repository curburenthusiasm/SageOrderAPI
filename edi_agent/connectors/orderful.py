"""Orderful API connector -- submits outbound X12 documents.

Network calls are made only when an API key is configured. When unconfigured
(e.g. local development), :meth:`OrderfulClient.submit` returns a simulated
transaction id so the rest of the pipeline can be exercised end to end.
"""
from __future__ import annotations

import uuid

import requests

from ..config import config


class OrderfulError(Exception):
    """Raised on a 4xx/5xx response from the Orderful API."""


class OrderfulClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else config.ORDERFUL_API_KEY
        self.base_url = (base_url or config.ORDERFUL_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> str:
        """POST an X12 document to Orderful. Returns the transaction id.

        :param doc_type: one of "997", "855", "856", "810".
        """
        if not self.configured:
            # Dry-run mode: no credentials, so simulate a successful submission.
            return f"SIMULATED-{doc_type}-{uuid.uuid4().hex[:10]}"

        url = f"{self.base_url}/transactions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "tradingPartnerId": trading_partner,
            "transactionType": doc_type,
            "format": "X12",
            "contents": x12_string,
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
        except requests.RequestException as exc:
            raise OrderfulError(f"Network error submitting {doc_type}: {exc}") from exc

        if resp.status_code >= 400:
            raise OrderfulError(
                f"Orderful returned {resp.status_code} for {doc_type}: {resp.text}"
            )
        try:
            data = resp.json()
        except ValueError:
            return resp.text.strip() or "UNKNOWN"
        return str(data.get("id") or data.get("transactionId") or "UNKNOWN")
