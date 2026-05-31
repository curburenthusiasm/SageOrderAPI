"""Tests for the multi-bot dashboard + Judge."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient  # noqa: E402

from edi_agent import agent, judge as judge_mod      # noqa: E402
from edi_agent.agent_events import EventStore, ROSTER  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_850.edi")


def _client():
    return TestClient(agent.app)


def _sample():
    with open(SAMPLE) as fh:
        return fh.read()


# --- Event store ----------------------------------------------------------

def test_event_store_record_and_recent(tmp_path):
    store = EventStore(db_path=str(tmp_path / "e.db"))
    store.record("inboxbot", "email", "Order status inquiry", "resolved", "self_heal")
    store.record("edi_monitor", "edi", "997 rejection", "escalated", "escalate")
    assert store.count() == 2
    recent = store.recent(limit=10)
    assert recent[0]["source"] == "edi_monitor"        # newest first
    assert recent[0]["outcome"] == "escalated"


# --- Judge reward function ------------------------------------------------

def test_judge_reward_table_matches_spec():
    # Spec Section 6.2 mappings.
    assert judge_mod.score_event({"decision": "self_heal", "outcome": "confirmed"}) == 1.0
    assert judge_mod.score_event({"decision": "self_heal", "outcome": "resolved"}) == 0.8
    assert judge_mod.score_event({"decision": "self_heal", "outcome": "corrected"}) == -1.0
    assert judge_mod.score_event({"decision": "escalate", "outcome": "over"}) == -0.3
    assert judge_mod.score_event({"decision": "watch", "outcome": "missed"}) == -0.5
    assert judge_mod.score_event({"outcome": "authority_violation"}) == -2.0
    # explicit reward_signal overrides
    assert judge_mod.score_event({"metadata": {"reward_signal": 0.42}}) == 0.42
    # pending -> no signal
    assert judge_mod.score_event({"outcome": "pending"}) is None


def test_judge_evaluate_aggregates_and_flags_degraded():
    events = [
        {"source": "inboxbot", "outcome": "resolved", "decision": "self_heal",
         "created_at": "2999-01-01T00:00:00+00:00", "subject": "ok"},
        {"source": "leadtime_bot", "outcome": "failed", "decision": "self_heal",
         "created_at": "2999-01-01T00:00:00+00:00", "subject": "bad"},
        {"source": "leadtime_bot", "outcome": "failed", "decision": "self_heal",
         "created_at": "2999-01-01T00:00:00+00:00", "subject": "bad2"},
    ]
    ev = judge_mod.evaluate(events)
    by_src = {a["source"]: a for a in ev["agents"]}
    assert by_src["inboxbot"]["verdict"] == "healthy"    # reward 0.8
    assert by_src["leadtime_bot"]["verdict"] == "failing"  # reward -1.0
    assert "leadtime_bot" in ev["summary"]["degraded"]
    assert ev["summary"]["healthy"] is False
    # Every roster agent shows up even with no events.
    assert {a["source"] for a in ev["agents"]} >= {r["source"] for r in ROSTER}


# --- Endpoints ------------------------------------------------------------

def test_events_ingest_and_dashboard():
    c = _client()
    # An external department bot reports two events.
    r = c.post("/events", json=[
        {"source": "open_claw", "event_type": "resolution", "subject": "briefing posted",
         "outcome": "resolved", "decision": "self_heal"},
        {"source": "sage_bot", "event_type": "erp_query", "subject": "order status",
         "outcome": "resolved", "decision": "self_heal"},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["data"]["recorded"] == 2

    d = c.get("/api/dashboard").json()["data"]
    sources = {a["source"] for a in d["judge"]["agents"]}
    assert "open_claw" in sources and "sage_bot" in sources
    assert any(e["source"] == "open_claw" for e in d["recent_events"])

    j = c.get("/api/judge").json()["data"]
    assert "summary" in j and "agents" in j

    assert c.get("/dashboard").status_code == 200


def test_edi_pipeline_activity_appears_on_dashboard():
    c = _client()
    c.post("/850/inbound", json={"edi": _sample()})
    d = c.get("/api/dashboard").json()["data"]
    # The EDI Agent bot recorded parse/generate/submit events.
    edi = next(a for a in d["judge"]["agents"] if a["source"] == "edi_agent")
    assert edi["events"] >= 1
    assert d["edi_pipeline"]["orders"] >= 1


def test_events_ingest_rejects_bad_payload():
    c = _client()
    r = c.post("/events", json=[{"event_type": "x"}])   # missing required 'source'
    assert r.status_code == 422
    assert r.json()["success"] is False


if __name__ == "__main__":
    import tempfile, pathlib
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames:
                fn(pathlib.Path(tempfile.mkdtemp()))
            else:
                fn()
            print(f"PASS {name}")
