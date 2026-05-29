"""Tests for envelope/count repair and workflow activation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent import spec_generator                 # noqa: E402
from edi_agent.core.validator import validate_document  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")
_FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _client():
    from edi_agent import agent
    return TestClient(agent.app)


def _sample():
    with open(SAMPLE) as fh:
        return fh.read()


def test_fix_counts_repairs_broken_envelope():
    """A tailored doc with wrong SE/CTT and drifted control numbers becomes valid."""
    isa = ("ISA*00*          *00*          *ZZ*A              *ZZ*B              "
           "*250529*1200*U*00401*000000007*0*P*>")
    broken = (
        f"{isa}~GS*PR*A*B*20250529*1200*7*X*004010~ST*855*0001~"
        "BAK*00*AC*PO1*20250529~PO1*1*5*EA~PO1*2*3*EA~"
        "CTT*99~"                 # wrong line count (should be 2)
        "SE*1*0001~"             # wrong segment count
        "GE*9*9~"                # GE02 drifted from GS06 (7)
        "IEA*9*999999999~"      # IEA02 drifted from ISA13 (000000007)
    )
    # Broken doc fails validation up front.
    assert validate_document(broken).ok is False

    fixed = spec_generator._fix_counts(broken)
    result = validate_document(fixed)
    assert result.ok, result.errors
    assert "CTT*2" in fixed                         # line count repaired
    assert "SE*6*0001" in fixed                     # ST..SE inclusive = 6
    assert "GE*1*7" in fixed                        # GE02 = GS06
    assert "IEA*1*000000007" in fixed               # IEA02 = ISA13


def test_activate_workflow_when_docs_pass():
    c = _client()
    c.post("/specs", data={"doc_type": "855", "trading_partner": "ACMERETAIL"},
           files={"file": ("acme_855.pdf", _FAKE_PDF, "application/pdf")})
    po = c.post("/850/inbound", json={"edi": _sample()}).json()["data"]["po_number"]

    r = c.post(f"/order/{po}/activate-workflow")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["activated"] is True
    assert "997" in d["doc_types"] and "855" in d["doc_types"]
    assert any(p["phase"] == "import" for p in d["workflow"]["phases"])

    # /integrations now reports ACMERETAIL as activated.
    integrations = c.get("/integrations").json()["data"]["integrations"]
    acme = next(i for i in integrations if i["trading_partner"] == "ACMERETAIL")
    assert acme["activated"] is True


def test_activate_workflow_requires_specs():
    c = _client()
    # An 850 from a partner with no companion guides on file.
    # Use a same-length (10-char) id so the fixed-width ISA stays intact.
    raw = _sample().replace("ACMERETAIL", "NOSPECCORP")
    po = c.post("/850/inbound", json={"edi": raw}).json()["data"]["po_number"]
    r = c.post(f"/order/{po}/activate-workflow")
    assert r.status_code == 422
    assert r.json()["success"] is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All workflow tests passed.")
