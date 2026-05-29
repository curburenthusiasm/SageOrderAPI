"""Phase 2 tests: connectors (dry mode), ship webhook, and LLM fallback."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent import conversation                       # noqa: E402
from edi_agent.connectors.shipping import ShippingClient  # noqa: E402
from edi_agent.connectors.sql_reader import SqlReader     # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def _client():
    # Force the deterministic path so tests never reach the network.
    os.environ["EDI_AGENT_LLM"] = "0"
    from edi_agent import agent
    return TestClient(agent.app)


def _load_sample():
    with open(SAMPLE) as fh:
        return fh.read()


def test_sql_reader_dry_mode():
    reader = SqlReader(conn_string="")
    assert reader.available is False
    assert reader.get_product("ABC123") is None
    assert reader.get_inventory("ABC123") is None

    class _Line:
        vendor_part, buyer_part, upc = "ABC123", "", ""

    class _Order:
        lines = [_Line()]

    mappings = {"sku_prices": {}}
    # No-op when unavailable -- must not raise and must not invent prices.
    out = reader.enrich_mappings(_Order(), mappings)
    assert out["sku_prices"] == {}


def test_shipping_dry_mode():
    client = ShippingClient(api_key="", base_url="")
    assert client.configured is False
    assert client.get_shipment("PO1") == {}
    assert client.get_tracking("PO1") == []


def test_shipping_normalize_variants():
    data = {"shipDate": "20250605", "carrier": "UPSN", "trackingNumbers": ["1Z1"]}
    out = ShippingClient._normalize(data)
    assert out["ship_date"] == "20250605"
    assert out["carrier_code"] == "UPSN"
    assert out["tracking_numbers"] == ["1Z1"]


def test_llm_unavailable_without_key():
    os.environ.pop("ANTHROPIC_API_KEY", None)
    assert conversation.llm_available() is False


def test_health_reports_connectors():
    c = _client()
    d = c.get("/health").json()
    assert "shipping_configured" in d
    assert "sql_configured" in d
    assert "llm_enabled" in d
    assert d["llm_enabled"] is False  # forced off above


def test_ship_webhook_triggers_856_810():
    c = _client()
    # Ingest an 850 first.
    r = c.post("/850/inbound", json={"edi": _load_sample()})
    assert r.status_code == 200
    po = r.json()["po_number"]

    # Fire the ship webhook with explicit ship data.
    r = c.post("/webhook/ship", json={
        "po_number": po,
        "ship_date": "20250605",
        "carrier_code": "UPSN",
        "ship_method": "GROUND",
        "tracking_numbers": ["1Z999AA10123456784"],
        "submit": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "ST*856*" in body["ship_notice_856"]
    assert "ST*810*" in body["invoice_810"]
    assert set(body["submissions"]) == {"856", "810"}
    assert "856" in body["status"]["documents_generated"]
    assert "810" in body["status"]["documents_generated"]


def test_ship_webhook_requires_ship_data():
    c = _client()
    r = c.post("/850/inbound", json={"edi": _load_sample()})
    po = r.json()["po_number"]
    # No ship data anywhere -> 422.
    r = c.post("/webhook/ship", json={"po_number": po, "submit": False})
    assert r.status_code == 422


def test_enrich_prices_requires_sql():
    c = _client()
    r = c.post("/850/inbound", json={"edi": _load_sample()})
    po = r.json()["po_number"]
    r = c.post(f"/order/{po}/enrich-prices")
    assert r.status_code == 503  # SQL not configured in tests


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All phase 2 tests passed.")
