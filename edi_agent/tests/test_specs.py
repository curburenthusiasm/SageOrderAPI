"""Tests for the partner spec library + integration onboarding."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent import spec_generator                # noqa: E402
from edi_agent.spec_store import SpecStore, public  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")
# A tiny valid PDF (header + minimal body) is enough to store/upload.
_FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _client():
    from edi_agent import agent
    return TestClient(agent.app)


def _sample():
    with open(SAMPLE) as fh:
        return fh.read()


def test_spec_generator_unavailable_without_key():
    # No ANTHROPIC_API_KEY in tests -> spec-guided pass is a no-op.
    assert spec_generator.available() is False


def test_spec_store_crud(tmp_path):
    store = SpecStore(db_path=str(tmp_path / "s.db"), specs_dir=str(tmp_path / "specs"))
    rec = store.save("WALMART", "856", "wm_856.pdf", _FAKE_PDF)
    assert rec["id"] and rec["doc_type"] == "856"
    assert os.path.exists(rec["file_path"])  # internal record keeps the path

    # find: exact partner match
    found = store.find("856", "WALMART")
    assert found and found["id"] == rec["id"]
    # find: no match for a different partner, and no generic fallback exists
    assert store.find("856", "TARGET") is None

    # generic spec fallback
    store.save("", "810", "generic_810.pdf", _FAKE_PDF)
    assert store.find("810", "ANYONE")["doc_type"] == "810"

    parts = store.partners()
    assert {"trading_partner": "WALMART", "doc_types": ["856"]} in parts

    # public() hides the disk path
    assert "file_path" not in public(rec)
    assert "excerpt_preview" in public(rec)

    assert store.delete(rec["id"]) is True
    assert store.find("856", "WALMART") is None


def test_upload_and_list_spec_endpoint():
    c = _client()
    r = c.post("/specs",
               data={"doc_type": "856", "trading_partner": "ACMERETAIL"},
               files={"file": ("acme_856.pdf", _FAKE_PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    spec_id = r.json()["data"]["id"]

    listed = c.get("/specs").json()["data"]["specs"]
    assert any(s["id"] == spec_id for s in listed)

    integrations = c.get("/integrations").json()["data"]["integrations"]
    assert any(i["trading_partner"] == "ACMERETAIL" and "856" in i["doc_types"]
               for i in integrations)

    assert c.delete(f"/specs/{spec_id}").json()["success"] is True


def test_upload_rejects_bad_doc_type():
    c = _client()
    r = c.post("/specs", data={"doc_type": "999", "trading_partner": ""},
               files={"file": ("x.pdf", _FAKE_PDF, "application/pdf")})
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_build_integration_from_sample_850():
    c = _client()
    # Onboard ACMERETAIL with an 856 + 810 spec.
    for dt in ("856", "810"):
        c.post("/specs", data={"doc_type": dt, "trading_partner": "ACMERETAIL"},
               files={"file": (f"acme_{dt}.pdf", _FAKE_PDF, "application/pdf")})

    r = c.post("/integrations/ACMERETAIL/build",
               json={"sample_850": _sample(), "submit": False})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # 997 always + the doc types we have specs for.
    assert set(data["doc_types_built"]) == {"997", "856", "810"}
    for dt in ("997", "856", "810"):
        assert data["report"][dt]["ok"] is True
        assert f"ST*{dt}*" in data["report"][dt]["edi"]
    # Spec on file but no API key -> baseline used, note recorded.
    assert "spec" in data["report"]["856"]["spec_guided"].lower()


def test_generate_with_spec_falls_back_without_key():
    c = _client()
    po = c.post("/850/inbound", json={"edi": _sample()}).json()["data"]["po_number"]
    # Partner of the sample is ACMERETAIL; upload an 855 spec for it.
    c.post("/specs", data={"doc_type": "855", "trading_partner": "ACMERETAIL"},
           files={"file": ("acme_855.pdf", _FAKE_PDF, "application/pdf")})
    r = c.post(f"/order/{po}/generate/855")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "ST*855*" in data["edi"]                      # still produced (baseline)
    assert "855" in data["status"]["spec_notes"]          # spec pass noted


def test_correct_document_uses_failure_message_and_spec(monkeypatch):
    c = _client()
    po = c.post("/850/inbound", json={"edi": _sample()}).json()["data"]["po_number"]
    c.post("/specs", data={"doc_type": "855", "trading_partner": "ACMERETAIL"},
           files={"file": ("acme_855.pdf", _FAKE_PDF, "application/pdf")})
    original = c.post(f"/order/{po}/generate/855").json()["data"]["edi"]

    seen = {}

    def fake_repair(doc_type, order, failed_x12, failure_message, spec_path):
        seen["doc_type"] = doc_type
        seen["po"] = order.po_number
        seen["failed_x12"] = failed_x12
        seen["failure_message"] = failure_message
        seen["spec_path"] = spec_path
        return failed_x12, True, "corrected from failure message using partner spec"

    monkeypatch.setattr(spec_generator, "repair_with_failure", fake_repair)

    r = c.post(f"/order/{po}/correct/855", json={
        "failure_message": "REF segment is required by the 855 companion guide",
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["edi"] == original
    assert seen["doc_type"] == "855"
    assert seen["failed_x12"] == original
    assert "REF segment" in seen["failure_message"]
    assert seen["spec_path"].endswith(".pdf")
    assert data["status"]["spec_notes"]["855"].startswith("corrected from failure")


def test_submit_retries_once_after_spec_correction(monkeypatch):
    from edi_agent import agent
    from edi_agent.connectors.orderful import OrderfulError

    c = _client()
    po = c.post("/850/inbound", json={"edi": _sample()}).json()["data"]["po_number"]
    c.post("/specs", data={"doc_type": "855", "trading_partner": "ACMERETAIL"},
           files={"file": ("acme_855.pdf", _FAKE_PDF, "application/pdf")})

    calls = []

    def fake_submit(x12, trading_partner, doc_type):
        calls.append((x12, trading_partner, doc_type))
        if len(calls) == 1:
            raise OrderfulError("Orderful rejected 855: missing REF*IA")
        return "TX-CORRECTED"

    def fake_repair(doc_type, order, failed_x12, failure_message, spec_path):
        assert "missing REF*IA" in failure_message
        return failed_x12, True, "corrected from failure message using partner spec"

    monkeypatch.setattr(agent._orderful, "submit", fake_submit)
    monkeypatch.setattr(spec_generator, "repair_with_failure", fake_repair)

    r = c.post(f"/order/{po}/generate/855", params={"submit": "true"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["submission"] == "TX-CORRECTED"
    assert len(calls) == 2
    assert data["status"]["spec_notes"]["855"].startswith("corrected from failure")


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames:
                import pathlib
                fn(pathlib.Path(tempfile.mkdtemp()))
            else:
                fn()
            print(f"PASS {name}")
    print("All spec tests passed.")
