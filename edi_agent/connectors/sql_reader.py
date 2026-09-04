"""SQL Server connector — live Sage 100 ERP data (Phase 2).

Pulls order, product, and invoice data from Sage 100 tables to enrich EDI
documents. Mirrors the connection style of the existing ``main.py`` (pyodbc,
ODBC Driver 17/18). Optional: if ``pyodbc`` isn't installed or
``SQL_SERVER_CONN`` isn't set, it runs in a dry mode where every lookup
returns ``None``/``[]`` so the pipeline still works.

The query stubs use standard Sage 100 column names. They are env-overridable
so the exact schema can be matched without code changes:
``SQL_*`` env vars below. All queries are parameterized (never string-formatted)
so item codes / PO numbers can't inject SQL.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from ..config import config

logger = logging.getLogger(__name__)

try:  # pyodbc is optional in dev / CI
    import pyodbc  # type: ignore
    pyodbc.pooling = True  # connection pooling (min 1 / max ~5 handled by driver)
except Exception:  # noqa: BLE001
    pyodbc = None

QUERY_TIMEOUT_S = 5

# --- Sage 100 query stubs (env-overridable). Single ? param each. ---------
GET_ORDER_HEADER = os.environ.get("SQL_ORDER_HEADER_QUERY", """
    SELECT SalesOrderNo, CustomerNo, OrderDate, CustomerPONo,
           ShipToName, ShipToAddress1, ShipToCity, ShipToState, ShipToZipCode,
           BillToName, BillToAddress1, BillToCity, BillToState, BillToZipCode
    FROM so_salesorderheader
    WHERE CustomerPONo = ?
""")

GET_ORDER_DETAIL = os.environ.get("SQL_ORDER_DETAIL_QUERY", """
    SELECT LineSeqNo, ItemCode, ItemDescription,
           QuantityOrdered, QuantityShipped, UnitPrice, UDF_UPC
    FROM so_salesorderdetail
    WHERE SalesOrderNo = ?
    ORDER BY LineSeqNo
""")

GET_ORDER_HEADER_HIST = GET_ORDER_HEADER.replace(
    "so_salesorderheader", "so_salesorderhistoryheader")
GET_ORDER_DETAIL_HIST = GET_ORDER_DETAIL.replace(
    "so_salesorderdetail", "so_salesorderhistorydetail")

GET_INVOICE_HEADER = os.environ.get("SQL_INVOICE_HEADER_QUERY", """
    SELECT InvoiceNo, InvoiceDate, CustomerPONo,
           FreightAmt, TaxAmt, NonTaxableAmt, TaxableAmt
    FROM ar_invoicehistoryheader
    WHERE CustomerPONo = ?
    ORDER BY InvoiceDate DESC
""")

GET_INVOICE_DETAIL = os.environ.get("SQL_INVOICE_DETAIL_QUERY", """
    SELECT InvoiceNo, LineSeqNo, ItemCode, ItemDescription,
           QuantityInvoiced, UnitPrice, ExtensionAmt
    FROM ar_invoicehistorydetail
    WHERE InvoiceNo = ?
    ORDER BY LineSeqNo
""")


class SQLReader:
    """Reads order / product / invoice data from Sage 100 on SQL Server."""

    def __init__(self, conn_string: Optional[str] = None):
        self.conn_string = conn_string if conn_string is not None else config.SQL_SERVER_CONN
        self._conn = None

    @property
    def available(self) -> bool:
        return bool(pyodbc) and bool(self.conn_string)

    def _connect(self):
        if not self.available:
            return None
        if self._conn is None:
            self._conn = pyodbc.connect(self.conn_string, timeout=QUERY_TIMEOUT_S)
            self._conn.timeout = QUERY_TIMEOUT_S
            logger.info("SQLReader connected to SQL Server")
        return self._conn

    def _rows(self, query: str, param: str) -> List[dict]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            cursor.execute(query, (param,))  # parameterized
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001 - soft failure
            logger.warning("SQLReader query failed (%s): %s", param, exc)
            return []

    # --- Orders -----------------------------------------------------------
    def get_order(self, po_number: str) -> Optional[dict]:
        """Active order header, falling back to history. None if not found."""
        rows = self._rows(GET_ORDER_HEADER, po_number) or self._rows(
            GET_ORDER_HEADER_HIST, po_number)
        if not rows:
            return None
        h = rows[0]
        return {
            "sales_order_no": h.get("SalesOrderNo"),
            "po_number": h.get("CustomerPONo"),
            "customer_id": h.get("CustomerNo"),
            "order_date": h.get("OrderDate"),
            "ship_to": {
                "name": h.get("ShipToName"), "address": h.get("ShipToAddress1"),
                "city": h.get("ShipToCity"), "state": h.get("ShipToState"),
                "zip": h.get("ShipToZipCode"),
            },
            "bill_to": {
                "name": h.get("BillToName"), "address": h.get("BillToAddress1"),
                "city": h.get("BillToCity"), "state": h.get("BillToState"),
                "zip": h.get("BillToZipCode"),
            },
        }

    def get_order_lines(self, sales_order_no: str) -> List[dict]:
        """Active order lines, falling back to history."""
        rows = self._rows(GET_ORDER_DETAIL, sales_order_no) or self._rows(
            GET_ORDER_DETAIL_HIST, sales_order_no)
        return [{
            "line_num": str(r.get("LineSeqNo")),
            "item_code": r.get("ItemCode"),
            "description": r.get("ItemDescription"),
            "qty_ordered": _num(r.get("QuantityOrdered")),
            "qty_shipped": _num(r.get("QuantityShipped")),
            "unit_price": _num(r.get("UnitPrice")),
            "upc": r.get("UDF_UPC"),
        } for r in rows]

    # --- Invoices ---------------------------------------------------------
    def get_invoice(self, po_number: str) -> Optional[dict]:
        rows = self._rows(GET_INVOICE_HEADER, po_number)
        if not rows:
            return None
        h = rows[0]
        return {
            "invoice_no": h.get("InvoiceNo"),
            "invoice_date": h.get("InvoiceDate"),
            "po_number": h.get("CustomerPONo"),
            "freight_amt": _num(h.get("FreightAmt")),
            "total_amt": _num(h.get("TaxableAmt")) + _num(h.get("NonTaxableAmt"))
            + _num(h.get("TaxAmt")),
        }

    def get_invoice_lines(self, invoice_no: str) -> List[dict]:
        rows = self._rows(GET_INVOICE_DETAIL, invoice_no)
        return [{
            "line_num": str(r.get("LineSeqNo")),
            "item_code": r.get("ItemCode"),
            "description": r.get("ItemDescription"),
            "qty_invoiced": _num(r.get("QuantityInvoiced")),
            "unit_price": _num(r.get("UnitPrice")),
            "extension_amt": _num(r.get("ExtensionAmt")),
        } for r in rows]

    def get_freight(self, invoice_no: str) -> float:
        """Freight amount from the invoice header (0.0 if none)."""
        for inv in self._rows(GET_INVOICE_HEADER, invoice_no):
            return _num(inv.get("FreightAmt"))
        return 0.0

    # --- Convenience ------------------------------------------------------
    def enrich_mappings(self, order, mappings: dict) -> dict:
        """Fill missing SKU prices from the live order lines. No-op when dry."""
        if not self.available:
            return mappings
        header = self.get_order(order.po_number)
        if not header:
            return mappings
        sku_prices = mappings.setdefault("sku_prices", {})
        lines = self.get_order_lines(header["sales_order_no"])
        by_item = {l["item_code"]: l for l in lines if l.get("item_code")}
        for line in order.lines:
            for key in (line.vendor_part, line.buyer_part, line.upc):
                if key and key in by_item and key not in sku_prices:
                    price = by_item[key].get("unit_price")
                    if price:
                        sku_prices[key] = float(price)
        return mappings

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def _num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
