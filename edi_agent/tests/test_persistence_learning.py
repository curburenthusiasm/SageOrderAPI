"""Tests for session persistence (survives restart) and failure learning."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent import agent, spec_generator        # noqa: E402
from edi_agent.learning import LessonStore          # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")
_FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _client():
    return TestClient(agent.app)


def _raw(po):
    with open(SAMPLE) as fh:
        return fh.read().replace("4500012345", po)


# --- Session persistence --------------------------------------------------

def test_session_round_trips_through_storage():
    po = "PERSIST01"
    c = _client()
    c.post("/850/inbound", json={"edi": _raw(po)})
    c.post(f"/order/{po}/mappings", json={"mappings": {"carrier_code": "FDXG"}})

    # Simulate a restart: drop the in-memory session, then access it again.
    agent._SESSIONS.pop(po, None)
    r = c.get(f"/order/{po}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["order"]["po_number"] == po
    assert data["mappings"]["carrier_code"] == "FDXG"     # mapping survived
    assert "997" in data["status"]["documents_generated"]  # generated doc survived


def test_restore_sessions_rehydrates_memory():
    po = "PERSIST02"
    c = _client()
    c.post("/850/inbound", json={"edi": _raw(po)})
    agent._SESSIONS.clear()           # wipe memory entirely (cold start)
    agent._restore_sessions()         # what runs at module import / boot
    assert po in agent._SESSIONS
    assert agent._SESSIONS[po].order.po_number == po


# --- Failure learning -----------------------------------------------------

def test_lesson_store_records_and_dedups(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "l.db"))
    store.record("WALMART", "856", "Missing TD5 routing", source="rejection")
    store.record("WALMART", "856", "Missing TD5 routing", source="rejection")  # dup
    store.record("WALMART", "856", "BSN03 date format wrong")
    store.record("", "856", "Generic: CTT must equal HL count")  # generic
    assert store.count() == 3
    lessons = store.lessons_for("WALMART", "856")
    assert "Missing TD5 routing" in lessons
    assert "Generic: CTT must equal HL count" in lessons   # generic included
    assert store.lessons_for("TARGET", "856") == ["Generic: CTT must equal HL count"]


def test_correction_records_lesson_and_feeds_it_back(monkeypatch):
    c = _client()
    po = "LEARN01"
    # Own partner (10-char id keeps the ISA fixed width) so we don't pollute
    # other tests' spec library.
    partner = "LEARNCORP1"
    raw = _raw(po).replace("ACMERETAIL", partner)
    c.post("/specs", data={"doc_type": "855", "trading_partner": partner},
           files={"file": ("p_855.pdf", _FAKE_PDF, "application/pdf")})
    c.post("/850/inbound", json={"edi": raw})
    c.post(f"/order/{po}/generate/855")

    captured = {}

    def fake_repair(doc_type, order, failed_x12, failure_message, spec_path, lessons=None):
        captured["lessons"] = lessons
        return failed_x12, True, "corrected from failure message using partner spec"

    monkeypatch.setattr(spec_generator, "repair_with_failure", fake_repair)

    before = agent._lessons.count()
    msg = "Partner rejected: N1*ST segment is required on the 855"
    r = c.post(f"/order/{po}/correct/855", json={"failure_message": msg})
    assert r.status_code == 200, r.text

    # The failure became a durable lesson...
    assert agent._lessons.count() == before + 1
    assert any(msg in l for l in agent._lessons.lessons_for(partner, "855"))
    # ...and it was fed into the correction prompt.
    assert captured["lessons"] and any(msg in l for l in captured["lessons"])


def test_learning_endpoint():
    agent._lessons.record("ACMERETAIL", "810", "IT1 needs UP qualifier")
    r = _client().get("/learning")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["count"] >= 1
    assert any(x["lesson"] == "IT1 needs UP qualifier" for x in body["lessons"])


if __name__ == "__main__":
    import tempfile, pathlib
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames:
                fn(pathlib.Path(tempfile.mkdtemp()))
            else:
                fn()
            print(f"PASS {name}")
