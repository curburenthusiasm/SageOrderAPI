"""connectors/base.py — Abstract connector interface.

Every transport (Orderful, Tray.io, direct REST, AS2, SFTP) must implement
this interface so the brain can swap connectors per-partner without knowing
the underlying transport.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubmitResult:
    """Result of a document submission attempt."""
    transaction_id: str
    ok: bool = True
    error: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class InboundDocument:
    """Inbound document fetched from a partner channel."""
    transaction_id: str
    x12: str
    doc_type: str = "850"
    raw: dict = field(default_factory=dict)


class ConnectorBase(ABC):
    """Abstract transport connector. One instance per partner."""

    # Human-readable label used in logs and dashboards
    PLATFORM: str = "unknown"

    # ---------------------------------------------------------------------------
    # Required — every connector must implement
    # ---------------------------------------------------------------------------

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Return True if the connector has enough credentials/config to go live."""

    @abstractmethod
    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> SubmitResult:
        """Send an outbound X12 document.

        :param x12_string: Raw X12 payload.
        :param trading_partner: Partner identifier (ISA qualifier or internal ID).
        :param doc_type: One of "997", "855", "856", "810".
        :returns: SubmitResult with transaction_id on success.
        """

    # ---------------------------------------------------------------------------
    # Optional — override when the transport supports inbound polling
    # ---------------------------------------------------------------------------

    def fetch_inbound(self, doc_type: str = "850", status: str = "received") -> list[InboundDocument]:
        """Pull new inbound documents. Returns [] if not supported or unconfigured."""
        return []

    def acknowledge(self, transaction_id: str, status: str = "DELIVERED") -> bool:
        """Mark an inbound transaction as processed. No-op by default."""
        return False

    # ---------------------------------------------------------------------------
    # Optional — override for connectors that can build their own workflow
    # ---------------------------------------------------------------------------

    def supports_workflow_creation(self) -> bool:
        """Return True if this connector can autonomously create a workflow from a spec."""
        return False

    def create_workflow(self, partner_name: str, spec: dict, doc_types: list[str]) -> dict[str, Any]:
        """Create and deploy a workflow for a new partner.

        :returns: Dict with at minimum {'ok': bool, 'workflow_id': str, 'webhook_url': str}.
        """
        return {"ok": False, "error": "Not supported by this connector"}
