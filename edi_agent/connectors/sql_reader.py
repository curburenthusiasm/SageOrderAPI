"""SQL Server connector — pull live product / inventory data (Phase 2).

Follows the connection style of the existing ``main.py`` Sage sync (pyodbc +
ODBC Driver 17). The connector is **optional**: if ``pyodbc`` isn't installed
or ``SQL_SERVER_CONN`` isn't configured, it runs in a dry mode where every
lookup returns ``None``/empty so the rest of the pipeline still works.

The product/inventory queries are driven by env-overridable SQL templates so
they can be pointed at the real MAS_JEF schema without code changes. Each
template must take a single ``?`` parameter (the item code) and is executed
with a parameterized query — never string-formatted — to avoid injection.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ..config import config

logger = logging.getLogger(__name__)

try:  # pyodbc is optional in dev / CI
    import pyodbc  # type: ignore
except Exception:  # noqa: BLE001 - any import failure means "not available"
    pyodbc = None


# Default queries target a typical Sage/MAS product master + inventory view.
# Override via env to match the live schema. Columns are referenced by the
# aliases below, so adjust the SELECT list rather than downstream code.
DEFAULT_PRODUCT_QUERY = os.environ.get(
    "SQL_PRODUCT_QUERY",
    """
    SELECT TOP 1
        ItemCode        AS item_code,
        ItemCodeDesc    AS description,
        StandardUnitPrice AS unit_price,
        SalesUnitOfMeasure AS uom,
        UDF_UPC         AS upc,
        UDF_VENDOR_PART AS vendor_part
    FROM CI_Item
    WHERE ItemCode = ?
    """,
)

DEFAULT_INVENTORY_QUERY = os.environ.get(
    "SQL_INVENTORY_QUERY",
    """
    SELECT ISNULL(SUM(QuantityOnHand - QuantityOnSalesOrder), 0) AS qty_available
    FROM IM_ItemWarehouse
    WHERE ItemCode = ?
    """,
)


class SqlReader:
    """Reads product and inventory data from SQL Server."""

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
            self._conn = pyodbc.connect(self.conn_string)
            logger.info("SqlReader connected to SQL Server")
        return self._conn

    def _fetch_one(self, query: str, item_code: str) -> Optional[dict]:
        conn = self._connect()
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute(query, (item_code,))  # parameterized — no string formatting
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [c[0] for c in cursor.description]
            return dict(zip(columns, row))
        except Exception as exc:  # noqa: BLE001 - surface as a soft failure
            logger.warning("SqlReader query failed for %s: %s", item_code, exc)
            return None

    def get_product(self, item_code: str) -> Optional[dict]:
        """Return product master fields for an item code (or None)."""
        if not item_code:
            return None
        return self._fetch_one(DEFAULT_PRODUCT_QUERY, item_code)

    def get_inventory(self, item_code: str) -> Optional[float]:
        """Return available quantity for an item code (or None if unknown)."""
        row = self._fetch_one(DEFAULT_INVENTORY_QUERY, item_code)
        if not row:
            return None
        val = row.get("qty_available")
        return float(val) if val is not None else None

    def enrich_mappings(self, order, mappings: dict) -> dict:
        """Fill in missing SKU prices from the product master.

        For each line whose price isn't already mapped, look the item up by
        vendor part / buyer part / UPC and set ``sku_prices``. Mutates and
        returns ``mappings``. A no-op when the connector is unavailable.
        """
        if not self.available:
            return mappings
        sku_prices = mappings.setdefault("sku_prices", {})
        for line in order.lines:
            for key in (line.vendor_part, line.buyer_part, line.upc):
                if not key or key in sku_prices:
                    continue
                product = self.get_product(key)
                if product and product.get("unit_price") is not None:
                    sku_prices[key] = float(product["unit_price"])
                    break
        return mappings

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
