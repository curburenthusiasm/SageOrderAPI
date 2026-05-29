#!/usr/bin/env python3
"""orderful_sync.py — Orderful <-> Sage 100 <-> ShipStation order lifecycle.

The Orderful equivalent of logicbroker_sync.py. Three phases, run together (the
normal scheduled use) or individually via --phase:

  Phase 1 — IMPORT (inbound 850 -> Sage)
    * Pull new inbound 850s from Orderful (or read a sample file with --sample-850)
    * Parse each, fire the 997 acknowledgment back to Orderful
    * Create the Sage 100 sales order via the ROI InSynch API
    * Send the 855 PO acknowledgment
    * Record per-PO state so orders are never re-imported

  Phase 2 — 856 ASN (shipped -> Orderful)
    * For imported orders not yet ASN'd, find ship data from ShipStation
    * Generate the 856 (spec-guided when a partner spec + API key exist) and
      submit it to Orderful

  Phase 3 — 810 INVOICE (Sage AR -> Orderful)
    * For imported orders not yet invoiced, pull the Sage invoice (SQL) when
      available, generate the 810, and submit it to Orderful

All document generation, validation, spec-conforming, submission, and state
tracking reuse the edi_agent pipeline (agent.py). Everything runs in a safe
dry mode when the corresponding credentials aren't configured.

Usage
-----
  python -m edi_agent.orderful_sync                 # all phases
  python -m edi_agent.orderful_sync --phase import
  python -m edi_agent.orderful_sync --phase import --sample-850 edi_agent/tests/sample_850.edi
  python -m edi_agent.orderful_sync --dry-run
"""
from __future__ import annotations

import argparse
import logging
from typing import List, Optional

from . import agent
from .core.models import LineItem, Order, Party
from .core.parser import EDIParseError, parse

log = logging.getLogger("orderful_sync")


# ──────────────────────────────────────────────────────────────────────────
# Phase 1 — IMPORT inbound 850s into Sage
# ──────────────────────────────────────────────────────────────────────────

def phase_import(dry_run: bool = False, sample_orders: Optional[List[str]] = None) -> int:
    """Import new inbound 850s: parse -> 997 -> Sage (ROI) -> 855. Idempotent."""
    log.info("PHASE 1 — IMPORT inbound 850s")

    if sample_orders is not None:
        raw_orders = sample_orders
    else:
        inbound = agent._orderful.fetch_inbound("850")
        raw_orders = [tx["x12"] for tx in inbound]
        log.info("  Orderful returned %d inbound 850(s)", len(raw_orders))

    imported = 0
    for raw in raw_orders:
        try:
            order = parse(raw)
        except EDIParseError as exc:
            log.error("  Could not parse an inbound 850: %s", exc)
            continue

        po = order.po_number
        existing = agent._state.get(po)
        if existing and existing.get("doc_997_sent"):
            log.info("  PO %s already imported — skipping", po)
            continue

        session = agent.OrderSession(order)
        agent._SESSIONS[po] = session
        agent._state.record_received(po, parsed_ok=True)

        # 997 acknowledgment (always, immediately)
        agent._generate(session, "997")
        if not dry_run:
            agent._submit(session, "997")

        # Create the Sage sales order
        try:
            result = agent._roi.import_sales_order(order, session.mappings)
            session.sage_order_no = result.get("sales_order_no")
            log.info("  PO %s -> Sage SO %s%s", po, session.sage_order_no,
                     " (dry)" if result.get("dry_run") else "")
        except Exception as exc:  # noqa: BLE001
            agent._state.log_error(po, f"sage import: {exc}")
            log.error("  PO %s Sage import failed: %s", po, exc)
            continue

        # 855 PO acknowledgment
        agent._generate(session, "855")
        if not dry_run:
            agent._submit(session, "855")

        imported += 1

    log.info("Phase 1 complete — %d order(s) imported", imported)
    return imported


# ──────────────────────────────────────────────────────────────────────────
# Phase 2 — 856 ASN
# ──────────────────────────────────────────────────────────────────────────

def phase_asn(dry_run: bool = False) -> int:
    """Send 856 ASNs for imported orders that have shipped (per ShipStation)."""
    log.info("PHASE 2 — 856 ASN")
    sent = 0
    for st in agent._state.list_orders():
        if not st.get("doc_997_sent") or st.get("doc_856_sent"):
            continue
        po = st["po_number"]
        session = _session_for(po)
        if session is None:
            log.debug("  PO %s: no order data available (need SQL or in-memory) — skipping", po)
            continue

        shipment = {}
        if agent._shipping.configured:
            try:
                shipment = agent._shipping.get_shipment(po) or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("  PO %s ShipStation error: %s", po, exc)
        if not shipment.get("tracking_numbers"):
            log.debug("  PO %s: not yet shipped in ShipStation — skipping", po)
            continue

        _apply_shipment(session, shipment)
        agent._generate(session, "856")
        if not dry_run:
            agent._submit(session, "856")
        log.info("  PO %s: 856 ASN sent", po)
        sent += 1

    log.info("Phase 2 complete — %d ASN(s) sent", sent)
    return sent


# ──────────────────────────────────────────────────────────────────────────
# Phase 3 — 810 INVOICE
# ──────────────────────────────────────────────────────────────────────────

def phase_invoice(dry_run: bool = False) -> int:
    """Send 810 invoices for imported orders that have a Sage invoice."""
    log.info("PHASE 3 — 810 INVOICE")
    sent = 0
    for st in agent._state.list_orders():
        if not st.get("doc_997_sent") or st.get("doc_810_sent"):
            continue
        po = st["po_number"]
        session = _session_for(po)
        if session is None:
            continue

        # Prefer the Sage invoice as the source of truth when SQL is available.
        if agent._sql.available:
            invoice = agent._sql.get_invoice(po)
            if not invoice:
                log.debug("  PO %s: no Sage invoice yet — skipping", po)
                continue
            session.mappings["invoice_number"] = invoice.get("invoice_no")
            if invoice.get("freight_amt"):
                session.mappings["freight_amount"] = invoice["freight_amt"]

        agent._generate(session, "810")
        if not dry_run:
            agent._submit(session, "810")
        log.info("  PO %s: 810 invoice sent", po)
        sent += 1

    log.info("Phase 3 complete — %d invoice(s) sent", sent)
    return sent


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _session_for(po: str):
    """Use the in-memory session if present, else rebuild the order from Sage."""
    session = agent._SESSIONS.get(po)
    if session is not None:
        return session
    order = _order_from_sage(po)
    if order is None:
        return None
    session = agent.OrderSession(order)
    agent._SESSIONS[po] = session
    return session


def _order_from_sage(po: str) -> Optional[Order]:
    """Reconstruct an Order from Sage (post-import source of truth). None if no SQL."""
    if not agent._sql.available:
        return None
    header = agent._sql.get_order(po)
    if not header:
        return None
    o = Order(po_number=po, po_date=str(header.get("order_date") or ""))
    st = header.get("ship_to") or {}
    bt = header.get("bill_to") or {}
    o.ship_to = Party(name=st.get("name", ""), address=st.get("address", ""),
                      city=st.get("city", ""), state=st.get("state", ""), zip=st.get("zip", ""))
    o.bill_to = Party(name=bt.get("name", ""), address=bt.get("address", ""),
                      city=bt.get("city", ""), state=bt.get("state", ""), zip=bt.get("zip", ""))
    for line in agent._sql.get_order_lines(header.get("sales_order_no") or ""):
        o.lines.append(LineItem(
            line_num=line.get("line_num", ""),
            po_number=po,
            vendor_part=line.get("item_code", ""),
            qty_ordered=line.get("qty_ordered", 0),
            qty_acknowledged=line.get("qty_shipped") or line.get("qty_ordered", 0),
            unit_price=line.get("unit_price", 0.0),
            uom="EA",
            description=line.get("description", ""),
            upc=line.get("upc", "") or "",
        ))
    return o


def _apply_shipment(session, shipment: dict) -> None:
    m = session.mappings
    for key in ("ship_date", "ship_time", "carrier_code", "service_level"):
        if shipment.get(key):
            m[key] = shipment[key]
    if shipment.get("tracking_numbers"):
        m["tracking_numbers"] = list(shipment["tracking_numbers"])
    if shipment.get("packages"):
        m["packages"] = shipment["packages"]


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def run(phase: str = "all", dry_run: bool = False,
        sample_850: Optional[str] = None) -> dict:
    totals = {"imported": 0, "asn": 0, "invoice": 0}
    sample_orders = None
    if sample_850:
        with open(sample_850) as fh:
            sample_orders = [fh.read()]
    if phase in ("import", "all"):
        totals["imported"] = phase_import(dry_run=dry_run, sample_orders=sample_orders)
    if phase in ("asn", "all"):
        totals["asn"] = phase_asn(dry_run=dry_run)
    if phase in ("invoice", "all"):
        totals["invoice"] = phase_invoice(dry_run=dry_run)
    return totals


def main():
    parser = argparse.ArgumentParser(description="Orderful <-> Sage 100 sync")
    parser.add_argument("--phase", choices=["import", "asn", "invoice", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only — no submits to Orderful or writes to Sage")
    parser.add_argument("--sample-850", metavar="FILE", default=None,
                        help="Import this 850 file instead of polling Orderful (for testing)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
    log.info("Orderful sync  %sphase=%s", "DRY RUN -- " if args.dry_run else "", args.phase)

    totals = run(phase=args.phase, dry_run=args.dry_run, sample_850=args.sample_850)

    log.info("SYNC COMPLETE — imported=%d  asn=%d  invoice=%d",
             totals["imported"], totals["asn"], totals["invoice"])


if __name__ == "__main__":
    main()
