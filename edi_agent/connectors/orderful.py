"""Orderful API connector -- submits outbound X12 documents.

Network calls are made only when an API key is configured. When unconfigured
(e.g. local development), :meth:`OrderfulClient.submit` returns a simulated
transaction id so the rest of the pipeline can be exercised end to end.

Implements ConnectorBase so it is interchangeable with Tray/REST connectors.
"""
from __future__ import annotations

import uuid

import requests

from ..config import config
from .base import ConnectorBase, InboundDocument, SubmitResult


class OrderfulError(Exception):
    """Raised on a 4xx/5xx response from the Orderful API."""


class OrderfulClient(ConnectorBase):
    PLATFORM = "orderful"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else config.ORDERFUL_API_KEY
        self.base_url = (base_url or config.ORDERFUL_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> SubmitResult:
        """POST an X12 document to Orderful. Returns the transaction id.

        :param doc_type: one of "997", "855", "856", "810".
        """
        if not self.configured:
            # Dry-run mode: no credentials, so simulate a successful submission.
            sim_id = f"SIMULATED-{doc_type}-{uuid.uuid4().hex[:10]}"
            return SubmitResult(transaction_id=sim_id, ok=True)

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
            tx_id = resp.text.strip() or "UNKNOWN"
            return SubmitResult(transaction_id=tx_id, ok=True)
        tx_id = str(data.get("id") or data.get("transactionId") or "UNKNOWN")
        return SubmitResult(transaction_id=tx_id, ok=True, raw=data)

    def fetch_inbound(self, doc_type: str = "850", status: str = "received") -> list[InboundDocument]:
        """Pull inbound X12 transactions of a type (e.g. new 850s).

        Returns a list of ``{"id": ..., "x12": ...}``. Empty in dry mode.

        NOTE: This is the one Orderful-specific adapter point — the exact query
        params and response field names depend on Orderful's /v2 transactions
        API. The defaults below are best-effort; adjust ``params`` / the field
        extraction once confirmed against the live API (the rest of the sync
        doesn't change).
        """
        if not self.configured:
            return []
        url = f"{self.base_url}/transactions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        params = {
            "transactionType": doc_type,
            "direction": "INBOUND",
            "deliveryStatus": status,
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            raise OrderfulError(f"Network error fetching inbound {doc_type}: {exc}") from exc
        if resp.status_code >= 400:
            raise OrderfulError(
                f"Orderful returned {resp.status_code} fetching {doc_type}: {resp.text}")
        data = resp.json() if resp.content else {}
        items = data.get("transactions") or data.get("data") or data if isinstance(data, list) else \
            (data.get("transactions") or data.get("data") or [])
        out = []
        for tx in items:
            x12 = tx.get("contents") or tx.get("x12") or tx.get("payload")
            if x12:
                out.append({"id": tx.get("id") or tx.get("transactionId"), "x12": x12})
        return out

    def acknowledge(self, transaction_id: str, status: str = "DELIVERED") -> bool:
        """Mark an inbound transaction as processed (best-effort, no-op in dry mode)."""
        if not self.configured or not transaction_id:
            return False
        url = f"{self.base_url}/transactions/{transaction_id}/status"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"deliveryStatus": status},
                timeout=30,
            )
            return resp.status_code < 400
        except requests.RequestException:
            return False
