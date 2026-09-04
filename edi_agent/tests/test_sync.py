"""Tests for the Orderful <-> Sage sync orchestrator (dry / no-credential mode)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from edi_agent import agent, orderful_sync  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def _raw_with_po(po: str) -> str:
    with open(SAMPLE) as fh:
        return fh.read().replace("4500012345", po)


def test_phase_import_creates_order_acks_and_sage_so():
    po = "SYNCIMP01"
    raw = _raw_with_po(po)
    n = orderful_sync.phase_import(dry_run=False, sample_orders=[raw])
    assert n == 1

    st = agent._state.get(po)
    assert st["doc_997_sent"] is True        # 997 fired
    assert st["doc_855_sent"] is True        # 855 acknowledgment sent
    session = agent._SESSIONS[po]
    assert (session.sage_order_no or "").startswith("SIMULATED-SO-")  # ROI import (dry)


def test_phase_import_is_idempotent():
    po = "SYNCIMP02"
    raw = _raw_with_po(po)
    assert orderful_sync.phase_import(sample_orders=[raw]) == 1
    # Second run sees it already imported.
    assert orderful_sync.phase_import(sample_orders=[raw]) == 0


def test_asn_skipped_without_shipstation():
    po = "SYNCASN01"
    orderful_sync.phase_import(sample_orders=[_raw_with_po(po)])
    # ShipStation isn't configured in tests, so no tracking -> no ASN for this PO.
    before = agent._state.get(po)["doc_856_sent"]
    orderful_sync.phase_asn()
    after = agent._state.get(po)["doc_856_sent"]
    assert before is False and after is False


def test_order_from_sage_none_without_sql():
    assert orderful_sync._order_from_sage("ANY") is None  # SQL not configured in tests


def test_run_returns_totals_dict():
    totals = orderful_sync.run(phase="import",
                               sample_850=SAMPLE)  # default PO, may already exist
    assert set(totals) == {"imported", "asn", "invoice"}


def test_sync_endpoint_import_phase():
    from fastapi.testclient import TestClient
    from edi_agent import agent
    c = TestClient(agent.app)
    raw = _raw_with_po("SYNCREST01")
    r = c.post("/sync/import", json={"sample_850": raw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["data"]["totals"]["imported"] == 1
    assert any(o["po_number"] == "SYNCREST01" for o in body["data"]["orders"])


def test_sync_endpoint_rejects_bad_phase():
    from fastapi.testclient import TestClient
    from edi_agent import agent
    c = TestClient(agent.app)
    r = c.post("/sync/nope", json={})
    assert r.status_code == 422
    assert r.json()["success"] is False


def test_sync_endpoint_no_body_ok():
    from fastapi.testclient import TestClient
    from edi_agent import agent
    c = TestClient(agent.app)
    r = c.post("/sync/asn")   # no body, no ShipStation -> 0 ASNs, still 200
    assert r.status_code == 200
    assert r.json()["data"]["totals"]["asn"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All sync tests passed.")
