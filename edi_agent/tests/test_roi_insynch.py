"""Tests for the ROI InSynch (Sage 100 import) connector + endpoint."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent.connectors.roi_insynch import RoiInsynchClient  # noqa: E402
from edi_agent.core.parser import parse                         # noqa: E402
from edi_agent.mappings import merge_mappings                   # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def _order():
    with open(SAMPLE) as fh:
        return parse(fh.read())


def _sample():
    with open(SAMPLE) as fh:
        return fh.read()


def _client():
    from edi_agent import agent
    return TestClient(agent.app)


def test_roi_dry_mode_payload_mapping():
    roi = RoiInsynchClient()
    assert roi.configured is False  # no AZURE_CLIENT_* in tests
    order = _order()
    mappings = merge_mappings({
        "sku_prices": {"ABC123": 31.50, "DEF456": 52.00},
        "sku_to_item": {"ABC123": "FIBER-A", "DEF456": "FIBER-B"},
        "sage_customer_no": "0402025",
    })
    result = roi.import_sales_order(order, mappings)
    assert result["ok"] and result["dry_run"] is True
    p = result["payload"]
    assert p["OrderType"] == "S"
    assert p["CompanyCode"] == "JEF"
    assert p["CustomerPONo"] == "4500012345"
    assert p["CustomerNo"] == "0402025"
    assert p["ShipToName"] == "ACME RETAIL STORE #12"
    assert len(p["SalesOrderDetails"]) == 2
    d0 = p["SalesOrderDetails"][0]
    assert d0["ItemCode"] == "FIBER-A"          # alias resolved to Sage item
    assert d0["QuantityOrdered"] == 100
    assert d0["UnitPrice"] == 31.50             # mapped price applied
    # _strip drops empty fields
    assert "RMANo" not in p


def test_import_to_sage_endpoint_dry_run():
    c = _client()
    po = c.post("/850/inbound", json={"edi": _sample()}).json()["data"]["po_number"]
    c.post(f"/order/{po}/mappings", json={"mappings": {"sage_customer_no": "0402025"}})
    r = c.post(f"/order/{po}/import-to-sage")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["sage_import"]["dry_run"] is True
    assert data["sage_import"]["sales_order_no"].startswith("SIMULATED-SO-")
    assert data["status"]["sage_order_no"].startswith("SIMULATED-SO-")


def test_health_reports_roi():
    d = _client().get("/health").json()["data"]
    assert d["roi_configured"] is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All ROI InSynch tests passed.")
