"""Phase 2 tests: connectors (dry mode), state machine, endpoints + envelope."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent import conversation                          # noqa: E402
from edi_agent.connectors.shipping import ShippingConnector, CARRIER_MAP  # noqa: E402
from edi_agent.connectors.sql_reader import SQLReader        # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def _client():
    from edi_agent import agent
    return TestClient(agent.app)


def _load_sample():
    with open(SAMPLE) as fh:
        return fh.read()


def _inbound(c):
    r = c.post("/850/inbound", json={"edi": _load_sample()})
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["success"] is True
    return env["data"]["po_number"]


# --- Connectors (dry mode) ------------------------------------------------

def test_sql_reader_dry_mode():
    reader = SQLReader(conn_string="")
    assert reader.available is False
    assert reader.get_order("PO1") is None
    assert reader.get_order_lines("SO1") == []
    assert reader.get_invoice("PO1") is None
    assert reader.get_invoice_lines("INV1") == []
    assert reader.get_freight("INV1") == 0.0

    class _Order:
        po_number = "PO1"
        lines = []
    assert reader.enrich_mappings(_Order(), {"sku_prices": {}})["sku_prices"] == {}


def test_shipping_dry_mode_and_carrier_map():
    client = ShippingConnector(api_key="", api_secret="")
    assert client.configured is False
    assert client.get_shipment("PO1") is None
    assert client.get_tracking("PO1") == []
    assert CARRIER_MAP["ups"] == "UPSN"
    assert CARRIER_MAP["fedex"] == "FDXG"


def test_shipping_normalize_shipstation_payload():
    shipments = [{
        "shipDate": "2026-05-28T14:00:00",
        "carrierCode": "ups",
        "serviceCode": "ground",
        "trackingNumber": "1Z999AA10123456784",
        "weight": {"value": 12.5},
        "shipmentItems": [{"lineItemKey": "1", "quantity": 10}],
    }]
    out = ShippingConnector._normalize(shipments)
    assert out["ship_date"] == "20260528"
    assert out["ship_time"] == "1400"
    assert out["carrier_code"] == "UPSN"
    assert out["service_level"] == "GROUND"
    assert out["tracking_numbers"] == ["1Z999AA10123456784"]
    assert out["packages"][0]["weight_lbs"] == 12.5
    assert out["packages"][0]["lines"] == [{"line_num": "1", "qty_shipped": 10}]


def test_llm_unavailable_without_key():
    assert conversation.llm_available() is False  # forced off in conftest


# --- API envelope + endpoints ---------------------------------------------

def test_health_envelope():
    d = _client().get("/health").json()
    assert d["success"] is True
    data = d["data"]
    assert {"shipping_configured", "sql_configured", "llm_enabled"} <= data.keys()
    assert data["llm_enabled"] is False


def test_inbound_envelope_and_state():
    c = _client()
    po = _inbound(c)
    # State machine recorded the receipt + 997.
    st = c.get(f"/order/{po}/status").json()
    assert st["success"] is True
    assert st["data"]["state"]["doc_997_sent"] is True
    assert st["data"]["state"]["phase"] == "997_SENT"


def test_ship_endpoint_triggers_856_810_and_state():
    c = _client()
    po = _inbound(c)
    r = c.post(f"/order/{po}/ship", json={
        "ship_date": "20250605",
        "carrier_code": "UPSN",
        "service_level": "GROUND",
        "tracking_numbers": ["1Z999AA10123456784"],
        "submit": True,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "ST*856*" in data["ship_notice_856"]
    assert "ST*810*" in data["invoice_810"]
    assert set(data["submissions"]) == {"856", "810"}

    st = c.get(f"/order/{po}/status").json()["data"]["state"]
    assert st["doc_856_sent"] and st["doc_810_sent"]
    assert st["phase"] == "INVOICED"


def test_ship_with_packages_split():
    c = _client()
    po = _inbound(c)
    r = c.post(f"/order/{po}/ship", json={
        "ship_date": "20250605", "carrier_code": "UPSN",
        "tracking_numbers": ["1Z1", "1Z2"],
        "packages": [
            {"tracking": "1Z1", "weight_lbs": 12.5, "lines": [{"line_num": "1", "qty_shipped": 100}]},
            {"tracking": "1Z2", "weight_lbs": 8.0, "lines": [{"line_num": "2", "qty_shipped": 50}]},
        ],
        "submit": False,
    })
    assert r.status_code == 200, r.text
    asn = r.json()["data"]["ship_notice_856"]
    assert "HL*1**S" in asn and "HL*4**S" in asn


def test_ship_requires_ship_data():
    c = _client()
    po = _inbound(c)
    r = c.post(f"/order/{po}/ship", json={"submit": False})
    assert r.status_code == 422
    assert r.json()["success"] is False
    assert r.json()["error"]["status"] == 422


def test_manual_invoice_endpoint():
    c = _client()
    po = _inbound(c)
    r = c.post(f"/order/{po}/invoice", json={}, params={"submit": True})
    assert r.status_code == 200, r.text
    assert "ST*810*" in r.json()["data"]["invoice_810"]


def test_orders_listing():
    c = _client()
    po = _inbound(c)
    r = c.get("/orders")
    assert r.status_code == 200
    pos = [o["po_number"] for o in r.json()["data"]["orders"]]
    assert po in pos


def test_webhook_ship():
    c = _client()
    po = _inbound(c)
    r = c.post("/webhook/ship", json={
        "po_number": po, "ship_date": "20250605",
        "carrier_code": "UPSN", "tracking_numbers": ["1Z999AA10123456784"],
    })
    assert r.status_code == 200, r.text
    assert "ST*856*" in r.json()["data"]["ship_notice_856"]


def test_unknown_order_envelope():
    r = _client().get("/order/NOPE/status")
    assert r.status_code == 404
    assert r.json()["success"] is False


def test_enrich_prices_requires_sql():
    c = _client()
    po = _inbound(c)
    r = c.post(f"/order/{po}/enrich-prices")
    assert r.status_code == 503
    assert r.json()["success"] is False


def test_agent_message_entry_point():
    c = _client()
    # Paste an 850 through the OpenClaw-facing entry point.
    r = c.post("/agent/message", json={"message": _load_sample()})
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["success"] is True
    assert env["data"]["po_number"] == "4500012345"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All phase 2 tests passed.")
