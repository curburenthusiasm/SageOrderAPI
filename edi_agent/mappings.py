"""Human-provided field mappings (the only values a human must supply).

These defaults are a starting template. At runtime the web UI lets Robert
override any of these per-order; :func:`merge_mappings` combines a per-order
override dict on top of these defaults.
"""
from copy import deepcopy

FIELD_MAPPINGS = {
    "default_unit_price": None,       # fallback if SKU not in sku_prices
    "sku_prices": {
        # "SKU123": 29.99,
    },
    "acknowledgment_code": "AC",      # AC=accepted, IA=item accepted, IQ=qty change
    "carrier_code": "UPSN",           # UPSN, FDXG, etc.
    "ship_method": "GROUND",
    "service_level": "",              # carrier service level (e.g. GROUND, 2DAY)
    "ship_date": None,                # YYYYMMDD -- set when ready to ship
    "ship_time": "1200",              # HHMM
    "tracking_numbers": [],           # list of tracking numbers for this PO
    "bill_of_lading": "",             # REF*BM on 856/810
    "packages": [],                   # [{tracking, weight_lbs, lines:[{line_num, qty_shipped}]}]
    "invoice_number": None,           # set when invoicing; auto-generated if None
    "invoice_date": None,             # YYYYMMDD
    "contract_number": "",            # REF*CO on 810 (only if present on 850)
    "freight_amount": None,           # dollars; emits SAC freight on 810 if > 0
    "payment_terms": "Net30",
    "payment_terms_days": 30,
    # --- Sage 100 import (ROI InSynch) ---
    "sku_to_item": {},                # partner SKU/alias -> Sage ItemCode
    "sage_customer_no": "",           # Sage CustomerNo for this trading partner
    "ar_division_no": "",             # Sage AR division (defaults from config)
    "warehouse_code": "",             # Sage warehouse (defaults from config)
    "ship_via": "",                   # Sage ShipVia
    "terms_code": "",                 # Sage TermsCode
    "vendor_isa_id": "JEFFCOFIBRES",
    "vendor_isa_qualifier": "ZZ",
    "trading_partner": "",            # Orderful trading partner identifier
}


def default_mappings() -> dict:
    """A deep copy of the default mappings template."""
    return deepcopy(FIELD_MAPPINGS)


def merge_mappings(overrides: dict | None) -> dict:
    """Merge a per-order override dict on top of the defaults (deep for dicts)."""
    merged = default_mappings()
    if not overrides:
        return merged
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def price_for(mappings: dict, line) -> float:
    """Resolve the unit price for a line item from the mappings.

    Order of precedence: explicit SKU price (vendor part or UPC) ->
    default_unit_price -> the price already parsed off the 850.
    """
    sku_prices = mappings.get("sku_prices") or {}
    for key in (line.vendor_part, line.buyer_part, line.upc):
        if key and key in sku_prices:
            return float(sku_prices[key])
    if mappings.get("default_unit_price") is not None:
        return float(mappings["default_unit_price"])
    return float(line.unit_price or 0.0)
