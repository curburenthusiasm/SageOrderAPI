#!/usr/bin/env python3
"""dsco_sync.py — DSCO/Rithum <-> Sage 100 order lifecycle sync.

Mirrors orderful_sync.py but pulls from the DSCO V3 API instead of Orderful.

  Phase 1 — IMPORT (inbound DSCO orders -> Sage)
    * Pull new orders from DSCO (GET /order/page or stream)
    * Convert DSCO JSON to internal Order model
    * Create the Sage 100 sales order via the ROI InSynch API
    * Acknowledge the order back to DSCO
    * Record per-PO state so orders are never re-imported

  Phase 2 — 856 ASN (shipped -> DSCO)
    * For imported orders not yet ASN'd, find ship data from ShipStation
    * Submit shipment to DSCO via POST /order/singleShipment

  Phase 3 — 810 INVOICE (Sage AR -> DSCO)
    * For imported orders not yet invoiced, pull the Sage invoice and
      submit to DSCO via POST /invoice

Usage
-----
  python -m edi_agent.dsco_sync                   # all phases
  python -m edi_agent.dsco_sync --phase import
  python -m edi_agent.dsco_sync --dry-run
  python -m edi_agent.dsco_sync --include-test     # include DSCO test orders
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the parent package is importable when run as a script
_here = Path(__file__).resolve().parent
if str(_here.parent) not in sys.path:
    sys.path.insert(0, str(_here.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(_here.parent / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("dsco_sync")

# Late imports — avoid circular dependency with agent.py
_agent_mod = None
_roi_inst = None

def _get_agent():
    global _agent_mod
    if _agent_mod is None:
        from . import agent as _a
        _agent_mod = _a
    return _agent_mod

def _get_roi():
    global _roi_inst
    if _roi_inst is None:
        from .connectors.roi_insynch import RoiInsynchClient
        _roi_inst = RoiInsynchClient()
    return _roi_inst

# ---------------------------------------------------------------------------
# DSCO API client (lightweight — reuses creds from env)
# ---------------------------------------------------------------------------

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore


class _DscoApi:
    """Minimal DSCO V3 API client for the sync pipeline."""

    BASE = "https://api.dsco.io/api/v3"

    def __init__(self):
        self.client_id = os.getenv("DSCO_CLIENT_ID", "")
        self.client_secret = os.getenv("DSCO_CLIENT_SECRET", "")
        self.base_url = os.getenv("DSCO_BASE_URL", self.BASE).rstrip("/")
        self._token: str = ""
        self._token_expires: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and _requests)

    def _ensure_token(self) -> bool:
        if self._token and time.time() < self._token_expires:
            return True
        try:
            resp = _requests.post(
                f"{self.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            if resp.status_code != 200:
                log.error("DSCO auth failed %d: %s", resp.status_code, resp.text[:300])
                return False
            data = resp.json()
            self._token = data.get("access_token", "")
            self._token_expires = time.time() + int(data.get("expires_in", 3600)) - 300
            return bool(self._token)
        except Exception as exc:
            log.error("DSCO auth error: %s", exc)
            return False

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        self._ensure_token()
        resp = _requests.get(
            f"{self.base_url}{path}", headers=self._headers(),
            params=params or {}, timeout=60,
        )
        if resp.status_code == 401:
            self._token = ""
            self._ensure_token()
            resp = _requests.get(
                f"{self.base_url}{path}", headers=self._headers(),
                params=params or {}, timeout=60,
            )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _post(self, path: str, body: Any) -> dict:
        self._ensure_token()
        resp = _requests.post(
            f"{self.base_url}{path}", headers=self._headers(),
            json=body, timeout=60,
        )
        if resp.status_code == 401:
            self._token = ""
            self._ensure_token()
            resp = _requests.post(
                f"{self.base_url}{path}", headers=self._headers(),
                json=body, timeout=60,
            )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # -- Order fetching ---------------------------------------------------

    def get_orders(
        self,
        created_since: str | None = None,
        updated_since: str | None = None,
        until: str | None = None,
        lifecycle: list[str] | None = None,
        include_test: bool = False,
        orders_per_page: int = 100,
    ) -> list[dict]:
        """Fetch orders from DSCO, auto-paginating."""
        params: dict[str, Any] = {"ordersPerPage": orders_per_page}
        if created_since:
            params["ordersCreatedSince"] = created_since
        if updated_since:
            params["ordersUpdatedSince"] = updated_since
        if until:
            params["until"] = until
        if lifecycle:
            params["lifecycle"] = lifecycle
        # NOTE: includeTestOrders is NOT a valid DSCO v3 param — omit it.
        # Test orders are included by default in v3.

        all_orders: list[dict] = []
        scroll_id = None
        for _ in range(50):  # max pages
            if scroll_id:
                page_params = {"scrollId": scroll_id}
            else:
                page_params = dict(params)
            data = self._get("/order/page", params=page_params)
            orders = data.get("orders", [])
            if not orders:
                break
            all_orders.extend(orders)
            scroll_id = data.get("scrollId")
            if not scroll_id:
                break
            time.sleep(0.2)

        return all_orders

    # -- Acknowledge ------------------------------------------------------

    def acknowledge_order(self, dsco_order_id: str) -> dict:
        """POST /order/acknowledge — mark order as acknowledged."""
        payload = [{"id": str(dsco_order_id), "type": "DSCO_ORDER_ID"}]
        return self._post("/order/acknowledge", payload)

    def acknowledge_by_po(self, po_number: str) -> dict:
        payload = [{"id": po_number, "type": "PO_NUMBER"}]
        return self._post("/order/acknowledge", payload)

    # -- Shipment ---------------------------------------------------------

    def create_shipment(
        self,
        po_number: str,
        tracking_number: str,
        carrier: str = "",
        ship_date: str = "",
        line_items: list[dict] | None = None,
        dsco_order_id: str = "",
        warehouse_code: str = "",
    ) -> dict:
        """POST /order/singleShipment — submit ASN.

        ``line_items`` should be DSCO-shaped: [{"dscoItemId": ..., "quantity": ...}].
        ``warehouse_code`` is required by some DSCO retailers.
        DSCO does NOT accept ``carrier``/``carrierCode`` on shipments.
        """
        shipment: dict[str, Any] = {
            "trackingNumber": tracking_number,
            "shipDate": ship_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if warehouse_code:
            shipment["warehouseCode"] = warehouse_code
        if line_items:
            shipment["lineItems"] = line_items
        body: dict[str, Any] = {"poNumber": po_number, "shipments": [shipment]}
        if dsco_order_id:
            body["dscoOrderId"] = dsco_order_id
        return self._post("/order/singleShipment", body)

    # -- Invoice ----------------------------------------------------------

    def create_invoice(
        self,
        po_number: str,
        invoice_number: str,
        invoice_date: str = "",
        total_amount: str = "",
        line_items: list[dict] | None = None,
        dsco_order_id: str = "",
    ) -> dict:
        """POST /invoice — submit invoice to DSCO.

        DSCO field names: ``invoiceId`` (not invoiceNumber),
        ``totalAmount`` (not invoiceTotal).
        Line items use ``sku``, ``quantity``, ``unitPrice`` (not ``amount``).
        """
        body: dict[str, Any] = {
            "poNumber": po_number,
            "invoiceId": invoice_number,
            "invoiceDate": invoice_date,
        }
        if total_amount:
            body["totalAmount"] = float(total_amount) if isinstance(total_amount, str) else total_amount
        if line_items:
            body["lineItems"] = line_items
        if dsco_order_id:
            body["dscoOrderId"] = dsco_order_id
        return self._post("/invoice", body)


# ---------------------------------------------------------------------------
# DSCO JSON -> internal Order model
# ---------------------------------------------------------------------------

def _dsco_order_to_internal(dsco_order: dict):
    """Convert a DSCO JSON order to our internal Order dataclass."""
    po = dsco_order.get("poNumber") or dsco_order.get("purchaseOrderNumber") or ""
    po_date = dsco_order.get("orderDate") or dsco_order.get("createDate") or ""
    if "T" in po_date:
        po_date = po_date[:10]  # strip time portion

    # Shipping address
    ship = dsco_order.get("shipping") or {}
    ship_addr = ship.get("address", [])
    if isinstance(ship_addr, str):
        ship_addr = [ship_addr]
    from .core.models import Party, LineItem, Order
    ship_to = Party(
        name=ship.get("name") or f"{ship.get('firstName', '')} {ship.get('lastName', '')}".strip(),
        address=ship_addr[0] if ship_addr else ship.get("address1", ""),
        city=ship.get("city", ""),
        state=ship.get("region") or ship.get("state", ""),
        zip=ship.get("postal", ""),
    )

    # Billing (DSCO may not always provide)
    bill = dsco_order.get("billing") or {}
    bill_to = None
    if bill:
        bill_addr = bill.get("address", [])
        if isinstance(bill_addr, str):
            bill_addr = [bill_addr]
        bill_to = Party(
            name=bill.get("name") or f"{bill.get('firstName', '')} {bill.get('lastName', '')}".strip(),
            address=bill_addr[0] if bill_addr else bill.get("address1", ""),
            city=bill.get("city", ""),
            state=bill.get("region") or bill.get("state", ""),
            zip=bill.get("postal", ""),
        )

    # Line items
    lines: list[LineItem] = []
    for i, li in enumerate(dsco_order.get("lineItems", []), 1):
        lines.append(LineItem(
            line_num=str(li.get("lineNumber") or i),
            po_number=po,
            vendor_part=li.get("sku") or li.get("supplierSku") or "",
            buyer_part=li.get("partnerSku") or "",
            qty_ordered=float(li.get("quantity") or 0),
            qty_acknowledged=float(li.get("quantity") or 0),
            unit_price=float(li.get("expectedCost") or li.get("unitCost") or 0),
            uom=li.get("unitOfMeasure") or "EA",
            description=li.get("title") or li.get("description") or "",
            upc=li.get("upc") or "",
        ))

    # Retailer info
    retailer_name = dsco_order.get("retailerName") or dsco_order.get("dscoRetailerId") or "DSCO"
    buyer = Party(name=retailer_name, id=str(dsco_order.get("dscoRetailerId") or ""))

    order = Order(
        po_number=po,
        po_date=po_date,
        ship_to=ship_to,
        bill_to=bill_to,
        buyer=buyer,
        lines=lines,
        partner_isa_id="DSCO",
        partner_isa_qualifier="ZZ",
        carrier=ship.get("shipMethod") or ship.get("carrier") or "",
    )
    return order


# ---------------------------------------------------------------------------
# Partner config lookup (uses partners/*.yaml)
# ---------------------------------------------------------------------------

def _get_conn_str() -> str:
    """Build the SQL Server connection string from env vars."""
    conn_str = os.getenv("SQL_SERVER_CONN") or os.getenv("DB_CONN_STR") or os.getenv("MAS_JEF_CONN_STR", "")
    if conn_str:
        return conn_str
    server = os.getenv("DB_HOST") or os.getenv("SAGE_DB_SERVER", "")
    db = os.getenv("DB_NAME") or os.getenv("SAGE_DB_NAME", "MAS_JEF")
    user = os.getenv("DB_USER") or os.getenv("SAGE_DB_USER", "")
    pwd = os.getenv("DB_PASSWORD") or os.getenv("SAGE_DB_PASS", "")
    if not server:
        return ""
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={server};DATABASE={db};UID={user};PWD={pwd};"
        f"Encrypt=yes;TrustServerCertificate=yes;"
    )


def _get_next_so_number() -> str | None:
    """Read the next available SalesOrderNo from Sage SO_Options."""
    try:
        import pyodbc
    except ImportError:
        return None

    conn_str = _get_conn_str()
    if not conn_str:
        return None

    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT NextSalesOrderNo FROM SO_Options")
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0].strip()
    except Exception as exc:
        log.warning("Failed to read NextSalesOrderNo: %s", exc)
    return None


def _lookup_customer_billto(customer_no: str) -> dict | None:
    """Pull bill-to address from Sage AR_Customer table."""
    try:
        import pyodbc
    except ImportError:
        log.debug("pyodbc not available — skipping AR_Customer lookup")
        return None

    conn_str = _get_conn_str()
    if not conn_str:
        return None

    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT CustomerName, AddressLine1, AddressLine2, City, State, ZipCode "
            "FROM AR_Customer WHERE CustomerNo = ?",
            (customer_no,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "name": (row.CustomerName or "").strip(),
            "address1": (row.AddressLine1 or "").strip(),
            "address2": (row.AddressLine2 or "").strip(),
            "city": (row.City or "").strip(),
            "state": (row.State or "").strip(),
            "zip": (row.ZipCode or "").strip(),
        }
    except Exception as exc:
        log.warning("AR_Customer lookup failed for %s: %s", customer_no, exc)
        return None


def _lookup_sku_mapping(customer_no: str) -> dict:
    """Build a DSCO SKU → Sage {item_code, price} mapping.

    Uses two Sage tables:
      - IM_AliasItem: AliasItemNo (DSCO SKU) → ItemCode (Sage master SKU)
      - IM_PriceCode: ItemCode → customer-specific pricing

    Returns dict keyed by AliasItemNo (the DSCO/retailer SKU).
    """
    try:
        import pyodbc
    except ImportError:
        log.debug("pyodbc not available — skipping SKU mapping")
        return {}

    conn_str = _get_conn_str()
    if not conn_str:
        return {}

    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()

        # Step 1: alias → master item code
        cursor.execute(
            "SELECT AliasItemNo, ItemCode FROM IM_AliasItem "
            "WHERE CustomerNo = ?",
            (customer_no,),
        )
        alias_map = {}
        for row in cursor.fetchall():
            alias = row.AliasItemNo.strip()
            master = row.ItemCode.strip()
            alias_map[alias] = master

        # Step 2: master item code → price
        cursor.execute(
            "SELECT ItemCode, DiscountMarkup1 FROM IM_PriceCode "
            "WHERE CustomerNo = ?",
            (customer_no,),
        )
        price_map = {}
        for row in cursor.fetchall():
            item_code = row.ItemCode.strip()
            price_map[item_code] = float(row.DiscountMarkup1 or 0)

        conn.close()

        # Merge: alias → {item_code, price}
        result = {}
        for alias, master in alias_map.items():
            result[alias] = {
                "item_code": master,
                "price": price_map.get(master, 0.0),
            }

        log.info("  Loaded %d alias mappings + %d price codes for customer %s",
                 len(alias_map), len(price_map), customer_no)
        return result
    except Exception as exc:
        log.warning("SKU mapping lookup failed for %s: %s", customer_no, exc)
        return {}


def _resolve_item_code(dsco_sku: str, sku_map: dict) -> dict | None:
    """Match a DSCO SKU to a Sage item code via IM_AliasItem mapping."""
    if not sku_map or not dsco_sku:
        return None
    return sku_map.get(dsco_sku)


def _get_partner_config(dsco_order: dict) -> dict:
    """Try to find Sage customer mappings from the partner YAML files.

    Matching strategy (in priority order):
    1. Partner YAML has dsco_retailer_id matching the order's dscoRetailerId
    2. Partner YAML has platform=dsco or platform=rithum AND name matches
       the dscoSupplierName (e.g. 'Jeffco Fibres - Target+' -> target)
    3. Loose name match against retailerName / dscoTradingPartnerName
    """
    try:
        import yaml
    except ImportError:
        log.warning("PyYAML not installed — cannot load partner configs")
        return {}

    dsco_retailer_id = str(dsco_order.get("dscoRetailerId") or "")
    dsco_onboarding_id = str(dsco_order.get("dscoOnboardingRetailerId") or "")
    supplier_name = (dsco_order.get("dscoSupplierName") or "").lower()
    retailer_name = (dsco_order.get("retailerName")
                     or dsco_order.get("dscoTradingPartnerName") or "").lower()
    partners_dir = _here.parent / "partners"

    def _extract(spec: dict) -> dict:
        return {
            "sage_customer_no": spec.get("sage_customer_no")
                                or (spec.get("sage") or {}).get("customer_no", ""),
            "ar_division_no": (spec.get("sage") or {}).get("ar_division_no", "00"),
            "warehouse_code": (spec.get("sage") or {}).get("warehouse_code", "000"),
            "sku_map": (spec.get("sage") or {}).get("sku_map", {}),
            "partner_name": spec.get("partner", ""),
        }

    specs: list[dict] = []
    for yf in sorted(partners_dir.glob("*.yaml")):
        try:
            with open(yf) as f:
                spec = yaml.safe_load(f) or {}
        except Exception:
            continue
        specs.append(spec)

    # Priority 1: exact dsco_retailer_id match
    for spec in specs:
        inbound = spec.get("inbound") or {}
        spec_retailer_id = str(inbound.get("dsco_retailer_id")
                               or inbound.get("rithum_retailer_id") or "")
        if spec_retailer_id and spec_retailer_id in (dsco_retailer_id, dsco_onboarding_id):
            return _extract(spec)

    # Priority 2: platform=dsco/rithum AND partner name appears in supplier_name
    for spec in specs:
        platform = (spec.get("platform") or "").lower()
        if platform not in ("dsco", "rithum"):
            continue
        partner_name = (spec.get("partner") or spec.get("slug") or "").lower()
        if partner_name and partner_name in supplier_name:
            return _extract(spec)

    # Priority 3: loose name match against retailer fields
    if retailer_name:
        for spec in specs:
            partner_name = (spec.get("partner") or spec.get("slug") or "").lower()
            if partner_name and (partner_name in retailer_name
                                or retailer_name in partner_name):
                return _extract(spec)

    return {}


# ──────────────────────────────────────────────────────────────────────────
# Phase 1 — IMPORT inbound DSCO orders into Sage
# ──────────────────────────────────────────────────────────────────────────

def phase_import(
    dsco: _DscoApi,
    roi: RoiInsynchClient,
    dry_run: bool = False,
    include_test: bool = False,
    lookback_hours: int = 48,
) -> int:
    """Import new DSCO orders: fetch -> convert -> Sage (ROI) -> acknowledge."""
    log.info("PHASE 1 — IMPORT inbound DSCO orders")

    if not dsco.configured:
        log.warning("  DSCO API not configured — skipping import")
        return 0

    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Query DSCO for orders in ALL pre-ship lifecycle stages.
    # Rithum/DSCO may auto-acknowledge orders before our import runs,
    # so filtering only "created" misses auto-acknowledged orders.
    # The idempotency check below (doc_997_sent) prevents double-imports.
    dsco_orders = dsco.get_orders(
        created_since=since,
        until=until_ts,
        lifecycle=None,  # all lifecycles — rely on state DB idempotency
        include_test=include_test,
    )
    # Filter out orders that are already fully shipped/completed on DSCO side
    # (no point importing those) but keep created + acknowledged.
    _importable = []
    for _o in dsco_orders:
        _lc = (_o.get("dscoLifecycle") or "").lower()
        if _lc in ("completed", "cancelled", "canceled"):
            continue
        _importable.append(_o)
    log.info("  DSCO returned %d order(s) (%d importable, filtered out completed/cancelled)",
             len(dsco_orders), len(_importable))
    dsco_orders = _importable

    # Get next available SO number from Sage
    next_so_str = _get_next_so_number()
    if next_so_str:
        try:
            so_counter = int(next_so_str)
        except ValueError:
            so_counter = None
            log.warning("  NextSalesOrderNo '%s' is not numeric — SO numbers must be assigned manually", next_so_str)
    else:
        so_counter = None
        log.warning("  Could not read NextSalesOrderNo — SO numbers must be assigned manually")

    imported = 0
    for dsco_order in dsco_orders:
        po = dsco_order.get("poNumber") or "UNKNOWN"
        dsco_order_id = str(dsco_order.get("dscoOrderId") or "")

        # Idempotency check
        existing = _get_agent()._state.get(po)
        if existing and existing.get("doc_997_sent"):
            log.info("  PO %s already imported — skipping", po)
            continue

        # Convert to internal Order
        try:
            order = _dsco_order_to_internal(dsco_order)
        except Exception as exc:
            log.error("  PO %s: failed to parse DSCO order: %s", po, exc)
            continue

        # Create session
        session = _get_agent().OrderSession(order)
        session.mappings["source_platform"] = "dsco"
        session.mappings["dsco_order_id"] = dsco_order_id

        # Apply partner-specific mappings (Sage customer#, etc.)
        partner_cfg = _get_partner_config(dsco_order)
        if partner_cfg.get("sage_customer_no"):
            session.mappings["sage_customer_no"] = partner_cfg["sage_customer_no"]
            log.info("  PO %s: matched partner %s (Sage cust %s)",
                     po, partner_cfg.get("partner_name"), partner_cfg["sage_customer_no"])
        if partner_cfg.get("ar_division_no"):
            session.mappings["ar_division_no"] = partner_cfg["ar_division_no"]
        if partner_cfg.get("warehouse_code"):
            session.mappings["warehouse_code"] = partner_cfg["warehouse_code"]
        if partner_cfg.get("sku_map"):
            session.mappings["sku_to_item"] = partner_cfg["sku_map"]

        # Pull bill-to from Sage AR_Customer (authoritative source)
        cust_no = session.mappings.get("sage_customer_no", "")
        if cust_no:
            billto = _lookup_customer_billto(cust_no)
            if billto:
                from .core.models import Party
                order.bill_to = Party(
                    name=billto["name"],
                    address=billto["address1"],
                    city=billto["city"],
                    state=billto["state"],
                    zip=billto["zip"],
                )
                log.info("  PO %s: bill-to from AR_Customer: %s", po, billto["name"])

        # Map DSCO SKUs to Sage item codes via IM_AliasItem + pricing from IM_PriceCode
        if cust_no:
            sku_mapping = _lookup_sku_mapping(cust_no)
            if sku_mapping:
                sku_map = {}
                for line in order.lines:
                    dsco_sku = line.vendor_part
                    match = _resolve_item_code(dsco_sku, sku_mapping)
                    if match:
                        sage_item = match["item_code"]
                        sage_price = match["price"]
                        sku_map[dsco_sku] = sage_item
                        line.vendor_part = sage_item
                        line.unit_price = sage_price
                        log.info("  PO %s: SKU %s -> %s @ $%.2f",
                                 po, dsco_sku, sage_item, sage_price)
                    else:
                        log.warning("  PO %s: SKU %s — no match in IM_PriceCode",
                                    po, dsco_sku)
                if sku_map:
                    session.mappings["sku_to_item"] = sku_map
                log.info("  PO %s: bill-to from AR_Customer: %s, %s %s",
                         po, billto["name"], billto["city"], billto["state"])

        _get_agent()._SESSIONS[po] = session
        _get_agent()._state.record_received(po, parsed_ok=True)

        # Assign Sage SO number
        if so_counter is not None:
            session.mappings["sales_order_no"] = f"{so_counter:07d}"
        
        # Import to Sage via ROI InSynch
        is_dry = dry_run
        if dry_run:
            log.info("  PO %s: would import to Sage (dry run) — customer=%s",
                     po, session.mappings.get("sage_customer_no", "UNKNOWN"))
            session.sage_order_no = f"DRY-{po[:12]}"
        else:
            try:
                result = roi.import_sales_order(order, session.mappings)
                session.sage_order_no = result.get("sales_order_no")
                is_dry = result.get("dry_run", False)
                log.info("  PO %s -> Sage SO %s%s", po, session.sage_order_no,
                         " (roi-dry)" if is_dry else "")
                # Store Sage SO# for ShipStation lookup in Phase 2
                if session.sage_order_no:
                    _get_agent()._state.set_fields(po, sage_order_no=session.sage_order_no)
                _get_agent()._state.mark_doc_sent(po, "997", f"DSCO-{dsco_order_id}")
                if so_counter is not None:
                    so_counter += 1
            except Exception as exc:
                _get_agent()._state.log_error(po, f"sage import: {exc}")
                log.error("  PO %s Sage import failed: %s", po, exc)
                continue

        # Acknowledge back to DSCO
        if not dry_run and not is_dry:
            try:
                if dsco_order_id:
                    dsco.acknowledge_order(dsco_order_id)
                else:
                    dsco.acknowledge_by_po(po)
                log.info("  PO %s: acknowledged on DSCO", po)
            except Exception as exc:
                log.warning("  PO %s: DSCO acknowledge failed (non-fatal): %s", po, exc)

        imported += 1

    log.info("Phase 1 complete — %d order(s) imported from DSCO", imported)
    _get_agent()._emit("dsco_sync", "edi", f"DSCO import: {imported} order(s) imported",
                outcome="resolved", decision="self_heal", imported=imported)
    return imported


# ──────────────────────────────────────────────────────────────────────────
# Phase 2 — 856 ASN (ship notice to DSCO)
# ──────────────────────────────────────────────────────────────────────────

def phase_asn(dsco: _DscoApi, dry_run: bool = False) -> int:
    """Send ship notices to DSCO for orders that have shipped (per ShipStation)."""
    log.info("PHASE 2 — 856 ASN to DSCO")

    shipping = _get_agent()._shipping
    if not shipping.configured:
        log.warning("  ShipStation not configured — skipping ASN phase")
        return 0

    sent = 0
    for st in _get_agent()._state.list_orders():
        if not st.get("doc_997_sent") or st.get("doc_856_sent"):
            continue
        po = st["po_number"]

        # ShipStation uses the Sage SO# as orderNumber, not the DSCO PO.
        # Use sage_order_no if available; fall back to the PO itself.
        ss_lookup = st.get("sage_order_no") or po
        if ss_lookup != po:
            log.info("  PO %s: using Sage SO# %s for ShipStation lookup", po, ss_lookup)

        # Query ShipStation for shipment data
        try:
            shipment = shipping.get_shipment(ss_lookup) or {}
        except Exception as exc:
            log.warning("  PO %s ShipStation error: %s", po, exc)
            continue
        if not shipment.get("tracking_numbers"):
            log.debug("  PO %s: not yet shipped — skipping", po)
            continue

        # Build DSCO shipment line items from ShipStation packages
        dsco_line_items = []
        for pkg in shipment.get("packages", []):
            for line in pkg.get("lines", []):
                dsco_line_items.append({
                    "lineNumber": line.get("line_num", ""),
                    "quantityShipped": line.get("qty_shipped", 0),
                })

        # If ShipStation has no line detail, pull from DSCO order and
        # build line items with dscoItemId (required by DSCO API).
        warehouse_code = ""
        dsco_order_data = None
        if not dsco_line_items:
            try:
                _now_utc = datetime.now(timezone.utc)
                dsco_orders = dsco.get_orders(
                    updated_since=(_now_utc - timedelta(days=55)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    until=(_now_utc + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                for dorder in dsco_orders:
                    if dorder.get("poNumber") == po:
                        dsco_order_data = dorder
                        for li in dorder.get("lineItems", []):
                            dsco_line_items.append({
                                "dscoItemId": li.get("dscoItemId", ""),
                                "quantity": li.get("quantity", li.get("quantityOrdered", 0)),
                            })
                            # Grab warehouseCode from the first line item
                            if not warehouse_code and li.get("warehouseCode"):
                                warehouse_code = li["warehouseCode"]
                        break
                if dsco_line_items:
                    log.info("  PO %s: pulled %d line item(s) from DSCO order (warehouse=%s)",
                             po, len(dsco_line_items), warehouse_code or "none")
            except Exception as exc:
                log.warning("  PO %s: could not fetch DSCO order for line items: %s", po, exc)

        # Fall back to partner config for warehouse code if DSCO order
        # didn't provide one (e.g. Target requires warehouseCode on ASN).
        if not warehouse_code and dsco_order_data:
            partner_cfg = _get_partner_config(dsco_order_data)
            if partner_cfg.get("warehouse_code"):
                warehouse_code = partner_cfg["warehouse_code"]
                log.info("  PO %s: using partner config warehouse=%s", po, warehouse_code)
        if not warehouse_code:
            warehouse_code = config.ROI_WAREHOUSE_CODE or "000"
            log.info("  PO %s: defaulting to warehouse=%s", po, warehouse_code)

        tracking = shipment["tracking_numbers"][0]
        carrier = shipment.get("carrier_code", "")
        ship_date = shipment.get("ship_date", "")
        # Convert CCYYMMDD to ISO for DSCO
        if ship_date and len(ship_date) == 8:
            ship_date = f"{ship_date[:4]}-{ship_date[4:6]}-{ship_date[6:8]}T00:00:00Z"

        log.info("  PO %s: ShipStation → tracking=%s carrier=%s lines=%d",
                 po, tracking, carrier, len(dsco_line_items))

        if not dry_run:
            try:
                dsco.create_shipment(
                    po_number=po,
                    tracking_number=tracking,
                    ship_date=ship_date,
                    line_items=dsco_line_items or None,
                    warehouse_code=warehouse_code,
                )
                _get_agent()._state.mark_doc_sent(po, "856", f"DSCO-ASN-{tracking[:20]}")
                log.info("  PO %s: ASN sent to DSCO ✅", po)
                sent += 1
            except Exception as exc:
                log.error("  PO %s: DSCO shipment failed: %s", po, exc)
        else:
            log.info("  PO %s: would send ASN (dry run)", po)
            sent += 1

    log.info("Phase 2 complete — %d ASN(s) sent to DSCO", sent)
    _get_agent()._emit("dsco_sync", "edi", f"DSCO ASN: {sent} shipment(s) sent",
                outcome="resolved", decision="self_heal", asn_sent=sent)
    return sent


# ──────────────────────────────────────────────────────────────────────────
# Phase 3 — 810 INVOICE (Sage AR -> DSCO)
# ──────────────────────────────────────────────────────────────────────────

def _lookup_invoice(po_number: str) -> dict | None:
    """Find a Sage invoice by CustomerPONo in AR_InvoiceHistoryHeader + Detail."""
    try:
        import pyodbc
    except ImportError:
        return None

    conn_str = _get_conn_str()
    if not conn_str:
        return None

    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        c = conn.cursor()

        # Match by CustomerPONo (which holds the DSCO PO number, truncated to 15 chars)
        po_match = po_number[:15]
        c.execute(
            "SELECT TOP 1 InvoiceNo, SalesOrderNo, InvoiceDate, CustomerPONo, "
            "TaxableSalesAmt, NonTaxableSalesAmt, FreightAmt, SalesTaxAmt "
            "FROM AR_InvoiceHistoryHeader "
            "WHERE CustomerPONo LIKE ? "
            "ORDER BY InvoiceDate DESC",
            (f"{po_match}%",),
        )
        row = c.fetchone()
        if not row:
            conn.close()
            return None

        invoice_no = row.InvoiceNo.strip()
        invoice_date = row.InvoiceDate
        total = float(row.TaxableSalesAmt or 0) + float(row.NonTaxableSalesAmt or 0)
        freight = float(row.FreightAmt or 0)
        tax = float(row.SalesTaxAmt or 0)

        # Get line items
        c.execute(
            "SELECT ItemCode, AliasItemNo, QuantityShipped, UnitPrice, ExtensionAmt, "
            "UnitOfMeasure, ItemCodeDesc "
            "FROM AR_InvoiceHistoryDetail WHERE InvoiceNo = ?",
            (invoice_no,),
        )
        lines = []
        for lr in c.fetchall():
            lines.append({
                "item_code": (lr.ItemCode or "").strip(),
                "alias_sku": (lr.AliasItemNo or "").strip(),
                "quantity": float(lr.QuantityShipped or 0),
                "unit_price": float(lr.UnitPrice or 0),
                "extension": float(lr.ExtensionAmt or 0),
                "uom": (lr.UnitOfMeasure or "EA").strip(),
                "description": (lr.ItemCodeDesc or "").strip(),
            })

        conn.close()

        return {
            "invoice_no": invoice_no,
            "sales_order_no": (row.SalesOrderNo or "").strip(),
            "invoice_date": invoice_date.strftime("%Y-%m-%dT%H:%M:%SZ") if invoice_date else "",
            "total": total,
            "freight": freight,
            "tax": tax,
            "grand_total": total + freight + tax,
            "lines": lines,
        }
    except Exception as exc:
        log.warning("Invoice lookup failed for PO %s: %s", po_number, exc)
        return None


def phase_invoice(dsco: _DscoApi, dry_run: bool = False) -> int:
    """Send invoices to DSCO for orders that have a Sage invoice."""
    log.info("PHASE 3 — 810 INVOICE to DSCO")
    sent = 0
    for st in _get_agent()._state.list_orders():
        if not st.get("doc_997_sent") or st.get("doc_810_sent"):
            continue
        po = st["po_number"]

        # Query AR_InvoiceHistoryHeader directly
        invoice = _lookup_invoice(po)
        if not invoice:
            log.debug("  PO %s: no Sage invoice yet — skipping", po)
            continue

        invoice_no = invoice["invoice_no"]
        invoice_date = invoice["invoice_date"]
        grand_total = invoice["grand_total"]

        # Build DSCO invoice line items
        dsco_lines = []
        for line in invoice.get("lines", []):
            dsco_lines.append({
                "sku": line["alias_sku"] or line["item_code"],
                "quantity": line["quantity"],
                "unitPrice": line["unit_price"],
            })

        log.info("  PO %s: invoice %s — $%.2f (%d lines)",
                 po, invoice_no, grand_total, len(dsco_lines))

        if not dry_run:
            try:
                dsco.create_invoice(
                    po_number=po,
                    invoice_number=invoice_no,
                    invoice_date=invoice_date,
                    total_amount=f"{grand_total:.2f}",
                    line_items=dsco_lines or None,
                )
                _get_agent()._state.mark_doc_sent(po, "810", f"DSCO-INV-{invoice_no}")
                log.info("  PO %s: invoice sent to DSCO ✅", po)
                sent += 1
            except Exception as exc:
                log.error("  PO %s: DSCO invoice failed: %s", po, exc)
        else:
            log.info("  PO %s: would send invoice (dry run)", po)
            sent += 1

    log.info("Phase 3 complete — %d invoice(s) sent to DSCO", sent)
    _get_agent()._emit("dsco_sync", "edi", f"DSCO invoice: {sent} invoice(s) sent",
                outcome="resolved", decision="self_heal", invoices_sent=sent)
    return sent


# ──────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────

def run(
    phase: str = "all",
    dry_run: bool = False,
    include_test: bool = False,
    lookback_hours: int = 48,
) -> dict:
    """Run DSCO sync phases. Returns totals dict."""
    dsco = _DscoApi()
    roi = _get_roi()

    if not dsco.configured:
        log.warning("DSCO not configured (DSCO_CLIENT_ID / DSCO_CLIENT_SECRET missing)")
        return {"imported": 0, "asn": 0, "invoice": 0, "error": "not configured"}

    totals = {"imported": 0, "asn": 0, "invoice": 0}

    if phase in ("import", "all"):
        totals["imported"] = phase_import(
            dsco, roi, dry_run=dry_run,
            include_test=include_test,
            lookback_hours=lookback_hours,
        )
    if phase in ("asn", "all"):
        totals["asn"] = phase_asn(dsco, dry_run=dry_run)
    if phase in ("invoice", "all"):
        totals["invoice"] = phase_invoice(dsco, dry_run=dry_run)

    return totals


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DSCO/Rithum <-> Sage 100 sync")
    parser.add_argument("--phase", choices=["import", "asn", "invoice", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only — no submissions to DSCO or writes to Sage")
    parser.add_argument("--include-test", action="store_true",
                        help="Include DSCO test orders (for onboarding)")
    parser.add_argument("--lookback", type=int, default=48,
                        help="Hours to look back for new orders (default: 48)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
    log.info("DSCO sync  %sphase=%s  lookback=%dh",
             "DRY RUN -- " if args.dry_run else "", args.phase, args.lookback)

    totals = run(
        phase=args.phase,
        dry_run=args.dry_run,
        include_test=args.include_test,
        lookback_hours=args.lookback,
    )
    log.info("DSCO SYNC COMPLETE — imported=%d  asn=%d  invoice=%d",
             totals["imported"], totals["asn"], totals["invoice"])


if __name__ == "__main__":
    main()
