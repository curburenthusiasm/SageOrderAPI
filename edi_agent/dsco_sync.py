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

from . import agent
from .core.models import LineItem, Order, Party
from .connectors.roi_insynch import RoiInsynchClient

log = logging.getLogger("dsco_sync")

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
        if include_test:
            params["includeTestOrders"] = "true"

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
        payload = [{"id": dsco_order_id, "idType": "dscoOrderId"}]
        return self._post("/order/acknowledge", payload)

    def acknowledge_by_po(self, po_number: str) -> dict:
        payload = [{"id": po_number, "idType": "poNumber"}]
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
    ) -> dict:
        """POST /order/singleShipment — submit ASN."""
        shipment = {
            "trackingNumber": tracking_number,
            "carrier": carrier,
            "shipDate": ship_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
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
        body: dict[str, Any] = {
            "poNumber": po_number,
            "invoiceNumber": invoice_number,
            "invoiceDate": invoice_date,
        }
        if total_amount:
            body["invoiceTotal"] = total_amount
        if line_items:
            body["lineItems"] = line_items
        if dsco_order_id:
            body["dscoOrderId"] = dsco_order_id
        return self._post("/invoice", body)


# ---------------------------------------------------------------------------
# DSCO JSON -> internal Order model
# ---------------------------------------------------------------------------

def _dsco_order_to_internal(dsco_order: dict) -> Order:
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

    dsco_orders = dsco.get_orders(
        created_since=since,
        until=until_ts,
        lifecycle=["created"],  # only new, unacknowledged orders
        include_test=include_test,
    )
    log.info("  DSCO returned %d order(s) in lifecycle=created", len(dsco_orders))

    imported = 0
    for dsco_order in dsco_orders:
        po = dsco_order.get("poNumber") or "UNKNOWN"
        dsco_order_id = str(dsco_order.get("dscoOrderId") or "")

        # Idempotency check
        existing = agent._state.get(po)
        if existing and existing.get("sage_imported"):
            log.info("  PO %s already imported — skipping", po)
            continue

        # Convert to internal Order
        try:
            order = _dsco_order_to_internal(dsco_order)
        except Exception as exc:
            log.error("  PO %s: failed to parse DSCO order: %s", po, exc)
            continue

        # Create session
        session = agent.OrderSession(order)
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

        agent._SESSIONS[po] = session
        agent._state.record_received(po, parsed_ok=True)

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
                agent._state.update(po, sage_imported=True,
                                    sage_order_no=session.sage_order_no)
            except Exception as exc:
                agent._state.log_error(po, f"sage import: {exc}")
                log.error("  PO %s Sage import failed: %s", po, exc)
                continue

        # Acknowledge back to DSCO
        if not dry_run and not is_dry:
            try:
                dsco.acknowledge_by_po(po)
                log.info("  PO %s: acknowledged on DSCO", po)
            except Exception as exc:
                log.warning("  PO %s: DSCO acknowledge failed (non-fatal): %s", po, exc)

        imported += 1

    log.info("Phase 1 complete — %d order(s) imported from DSCO", imported)
    agent._emit("dsco_sync", "edi", f"DSCO import: {imported} order(s) imported",
                outcome="resolved", decision="self_heal", imported=imported)
    return imported


# ──────────────────────────────────────────────────────────────────────────
# Phase 2 — 856 ASN (ship notice to DSCO)
# ──────────────────────────────────────────────────────────────────────────

def phase_asn(dsco: _DscoApi, dry_run: bool = False) -> int:
    """Send ship notices to DSCO for orders that have shipped (per ShipStation)."""
    log.info("PHASE 2 — 856 ASN to DSCO")
    sent = 0
    for st in agent._state.list_orders():
        if not st.get("sage_imported") or st.get("doc_856_sent"):
            continue
        if st.get("source_platform") != "dsco":
            continue  # only process DSCO-sourced orders
        po = st["po_number"]

        # Find shipment info from ShipStation
        shipment = {}
        if agent._shipping.configured:
            try:
                shipment = agent._shipping.get_shipment(po) or {}
            except Exception as exc:
                log.warning("  PO %s ShipStation error: %s", po, exc)
        if not shipment.get("tracking_numbers"):
            log.debug("  PO %s: not yet shipped — skipping", po)
            continue

        tracking = shipment["tracking_numbers"][0] if shipment.get("tracking_numbers") else ""
        carrier = shipment.get("carrier_code", "")
        ship_date = shipment.get("ship_date", "")
        dsco_order_id = st.get("dsco_order_id", "")

        if not dry_run:
            try:
                dsco.create_shipment(
                    po_number=po,
                    tracking_number=tracking,
                    carrier=carrier,
                    ship_date=ship_date,
                    dsco_order_id=dsco_order_id,
                )
                agent._state.update(po, doc_856_sent=True)
                log.info("  PO %s: ASN sent to DSCO (tracking: %s)", po, tracking)
                sent += 1
            except Exception as exc:
                log.error("  PO %s: DSCO shipment failed: %s", po, exc)
        else:
            log.info("  PO %s: would send ASN (dry run)", po)
            sent += 1

    log.info("Phase 2 complete — %d ASN(s) sent to DSCO", sent)
    agent._emit("dsco_sync", "edi", f"DSCO ASN: {sent} shipment(s) sent",
                outcome="resolved", decision="self_heal", asn_sent=sent)
    return sent


# ──────────────────────────────────────────────────────────────────────────
# Phase 3 — 810 INVOICE (Sage AR -> DSCO)
# ──────────────────────────────────────────────────────────────────────────

def phase_invoice(dsco: _DscoApi, dry_run: bool = False) -> int:
    """Send invoices to DSCO for orders that have a Sage invoice."""
    log.info("PHASE 3 — 810 INVOICE to DSCO")
    sent = 0
    for st in agent._state.list_orders():
        if not st.get("sage_imported") or st.get("doc_810_sent"):
            continue
        if st.get("source_platform") != "dsco":
            continue
        po = st["po_number"]

        # Pull Sage invoice
        if not agent._sql.available:
            log.debug("  PO %s: SQL not available — skipping invoice", po)
            continue
        invoice = agent._sql.get_invoice(po)
        if not invoice:
            log.debug("  PO %s: no Sage invoice yet — skipping", po)
            continue

        invoice_no = invoice.get("invoice_no", "")
        invoice_date = str(invoice.get("invoice_date", ""))
        total = str(invoice.get("total_amount", ""))
        dsco_order_id = st.get("dsco_order_id", "")

        if not dry_run:
            try:
                dsco.create_invoice(
                    po_number=po,
                    invoice_number=invoice_no,
                    invoice_date=invoice_date,
                    total_amount=total,
                    dsco_order_id=dsco_order_id,
                )
                agent._state.update(po, doc_810_sent=True)
                log.info("  PO %s: invoice sent to DSCO (%s)", po, invoice_no)
                sent += 1
            except Exception as exc:
                log.error("  PO %s: DSCO invoice failed: %s", po, exc)
        else:
            log.info("  PO %s: would send invoice (dry run)", po)
            sent += 1

    log.info("Phase 3 complete — %d invoice(s) sent to DSCO", sent)
    agent._emit("dsco_sync", "edi", f"DSCO invoice: {sent} invoice(s) sent",
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
    roi = agent._roi

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
