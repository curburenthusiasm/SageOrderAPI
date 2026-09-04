"""EDI transport connectors — pluggable per-partner.

All connectors implement ConnectorBase:
  - orderful  : Orderful network (default; existing X12/EDI partners)
  - tray      : Tray.io workflow trigger via webhook + optional GraphQL creation
  - rest_api  : Direct REST API for partners with native HTTP endpoints
  - rithum    : Rithum drop-ship platform (generic)
  - dsco      : Rithum Dsco Platform (V3 API — Target, Wayfair, etc.)

To get the right connector for a registered partner:
    from edi_agent.partner_registry import get_registry
    connector = get_registry().get_connector(partner_id)
"""
from .base import ConnectorBase, SubmitResult, InboundDocument
from .orderful import OrderfulClient, OrderfulError
from .tray import TrayConnector
from .rest_api import RestApiConnector
from .rithum import RithumConnector, RithumError
from .dsco import DscoConnector, DscoError

__all__ = [
    "ConnectorBase", "SubmitResult", "InboundDocument",
    "OrderfulClient", "OrderfulError",
    "TrayConnector",
    "RestApiConnector",
    "RithumConnector", "RithumError",
    "DscoConnector", "DscoError",
]
