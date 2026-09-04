"""The Judge — evaluates agent decisions into success/failure and a reward.

Implements the reward function from the Autonomous Department spec (Section 6):
every agent decision (SELF-HEAL / ESCALATE / WATCH) and its outcome maps to a
scalar reward. The Judge aggregates rewards per agent over a rolling window,
computes a success rate, assigns a verdict, and flags any agent whose 7-day
average drops below -0.3 (the spec's degradation threshold).

Pure functions over event dicts (see agent_events.EventStore) so it can judge
this project's EDI bots and the department's agents identically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from .agent_events import ROSTER

# (decision, outcome) -> reward, straight from spec Section 6.2.
REWARD_TABLE = {
    ("self_heal", "confirmed"): 1.0,    # Robert confirmed resolution correct
    ("self_heal", "resolved"): 0.8,     # correct, unverified
    ("self_heal", "corrected"): -1.0,   # Robert had to redo it
    ("escalate", "warranted"): 0.5,     # correct caution
    ("escalate", "over"): -0.3,         # over-escalation
    ("escalate", "constraint"): 1.0,    # hard constraint caught
    ("watch", "resolved"): 0.3,         # appropriate inaction
    ("watch", "missed"): -0.5,          # under-escalation
    ("any", "authority_violation"): -2.0,
    ("any", "constraint"): 0.0,         # safety layer, not an agent error
}

# Fallback when we only know the outcome (no explicit decision/outcome pairing).
_OUTCOME_FALLBACK = {
    "resolved": 0.8,
    "failed": -1.0,
    "escalated": 0.5,
    "ignored": 0.0,
    "pending": None,     # no signal yet
}

DEGRADED_THRESHOLD = -0.3
STALE_MIN = 15          # minutes -> "stale"
OFFLINE_MIN = 60        # minutes -> "offline"


def score_event(event: dict) -> Optional[float]:
    """Reward for one event, or None when there's no signal yet (pending)."""
    # Heartbeats are liveness signals, not decisions — they carry no reward.
    if (event.get("event_type") or "").lower() == "heartbeat":
        return None
    meta = event.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("reward_signal") is not None:
        try:
            return float(meta["reward_signal"])
        except (TypeError, ValueError):
            pass
    decision = (event.get("decision") or "").lower()
    outcome = (event.get("outcome") or "").lower()
    if (decision, outcome) in REWARD_TABLE:
        return REWARD_TABLE[(decision, outcome)]
    if ("any", outcome) in REWARD_TABLE:
        return REWARD_TABLE[("any", outcome)]
    return _OUTCOME_FALLBACK.get(outcome)


def _minutes_since(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except ValueError:
        return None


def _status(last_iso: Optional[str]) -> str:
    mins = _minutes_since(last_iso)
    if mins is None:
        return "offline"
    if mins <= STALE_MIN:
        return "active"
    if mins <= OFFLINE_MIN:
        return "stale"
    return "offline"


def evaluate(events: List[dict]) -> dict:
    """Roll up events into per-agent verdicts + an overall department view."""
    by_source: dict = {}
    for ev in events:
        src = ev.get("source") or "unknown"
        agg = by_source.setdefault(src, {
            "source": src, "events": 0, "rewards": [], "by_outcome": {},
            "last_at": None, "last_subject": "",
        })
        agg["events"] += 1
        outcome = (ev.get("outcome") or "pending").lower()
        agg["by_outcome"][outcome] = agg["by_outcome"].get(outcome, 0) + 1
        r = score_event(ev)
        if r is not None:
            agg["rewards"].append(r)
        # events arrive newest-first; capture the first (latest) seen
        if agg["last_at"] is None:
            agg["last_at"] = ev.get("created_at")
            agg["last_subject"] = ev.get("subject", "")

    labels = {a["source"]: a for a in ROSTER}
    agents = []
    # Union of the known roster and any sources that actually reported.
    for src in {*labels.keys(), *by_source.keys()}:
        agg = by_source.get(src)
        meta = labels.get(src, {"label": src, "group": "Other"})
        rewards = agg["rewards"] if agg else []
        reward_avg = round(sum(rewards) / len(rewards), 3) if rewards else None
        resolved = (agg["by_outcome"].get("resolved", 0) if agg else 0)
        failed = (agg["by_outcome"].get("failed", 0) if agg else 0)
        graded = resolved + failed
        success_rate = round(resolved / graded, 3) if graded else None
        agents.append({
            "source": src,
            "label": meta["label"],
            "group": meta["group"],
            "events": agg["events"] if agg else 0,
            "by_outcome": agg["by_outcome"] if agg else {},
            "reward_avg": reward_avg,
            "success_rate": success_rate,
            "last_at": agg["last_at"] if agg else None,
            "last_subject": agg["last_subject"] if agg else "",
            "status": _status(agg["last_at"] if agg else None),
            "verdict": _verdict(reward_avg, agg["events"] if agg else 0),
            "degraded": reward_avg is not None and reward_avg < DEGRADED_THRESHOLD,
        })
    agents.sort(key=lambda a: (a["group"], a["label"]))

    degraded = [a["source"] for a in agents if a["degraded"]]
    active = sum(1 for a in agents if a["status"] == "active")
    return {
        "agents": agents,
        "summary": {
            "total_agents": len(agents),
            "active": active,
            "degraded": degraded,
            "events_judged": len(events),
            "healthy": not degraded,
        },
    }


def _verdict(reward_avg: Optional[float], events: int) -> str:
    if events == 0:
        return "no data"
    if reward_avg is None:
        return "pending"
    if reward_avg < DEGRADED_THRESHOLD:
        return "failing"
    if reward_avg < 0.5:
        return "watch"
    return "healthy"
