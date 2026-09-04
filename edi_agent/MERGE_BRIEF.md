# Merge Brief — Order-API (edi_agent) ↔ Morning Taskbot (CEO Bot / Open Claw)

**For:** Open Claw, working on the morning-taskbot repo (branch `elegant-tharpe`)
**Prepared by:** the Order-API project (this repo, `edi_agent/`)
**Goal:** the morning-taskbot agents consume Order-API data, and both systems'
bots appear — judged — on one dashboard.

> DRAFT — finalize once the taskbot directory cleanup is complete and its module
> layout is known. Items marked **[CONFIRM]** need a value from Robert/OpenClaw.

---

## 0. Operating rules for this merge

- Work only on branch `elegant-tharpe` in the taskbot repo; **never push to `main`**.
- Treat the Order-API as an **independent service with a stable REST contract** —
  do not copy its internals into the taskbot. Integrate over HTTP.
- Every endpoint returns the envelope `{ "success": bool, "data": ..., "error": {...} }`.
  Always check `success` before using `data`.
- Do not modify Order-API append-only data paths or its hard behaviors. If the
  Order-API needs a change, request it as a PR against *its* branch
  (`claude/agent-webpage-interface-JMjNv`) — don't fork its logic into the taskbot.
- Log every integration action to `work_events` (taskbot side) as usual.

---

## 1. Architecture decision

The Order-API stays a standalone FastAPI service. The taskbot agents call it.
Two data-flow directions exist; **do both, they're complementary**:

1. **Taskbot → Order-API (commands & queries).** `sage_bot`, `edi_monitor`, and
   Open Claw call Order-API endpoints to read order state and drive EDI.
2. **Bidirectional activity feed (one dashboard).** Pick ONE of:
   - **(Recommended) Order-API → Supabase.** Order-API mirrors its
     `agent_events` rows into the department's Supabase `work_events` table, so
     the department's existing dashboard/judge is the single pane of glass.
   - **Department → Order-API `/events`.** The department agents also POST their
     `work_events` to Order-API `POST /events`, so the Order-API `/dashboard` is
     the single pane. (Already supported today.)

   **[CONFIRM]** which dashboard is canonical: Supabase-backed (department) or
   the Order-API `/dashboard`. The reward/Judge logic is identical on both sides
   (spec Section 6.2); converge on one.

Base URL of the Order-API service: **[CONFIRM]** (e.g. `http://127.0.0.1:8000`
locally, or a tunnel like `https://orderapi.jfcops.com`).

---

## 2. Order-API REST contract the taskbot consumes

All under the base URL; all return the `{success,data,error}` envelope.

| Need (taskbot agent) | Method & path | Notes |
|---|---|---|
| Receive/parse an 850, fire 997 | `POST /850/inbound` `{edi}` | returns parsed order + 997 |
| Create Sage order from an 850 | `POST /order/{po}/import-to-sage` | ROI InSynch |
| Order + doc status (for `sage_bot`) | `GET /order/{po}/status` | persisted state |
| List all orders + phase | `GET /orders` | RECEIVED→…→INVOICED |
| Generate/submit a doc | `POST /order/{po}/generate/{doc_type}?submit=true` | 997/855/856/810 |
| Ship → 856 + 810 | `POST /order/{po}/ship` | packages/tracking |
| Correct a rejected file (for `edi_monitor`) | `POST /correct` `{edi, failure_message, trading_partner?, doc_type?}` | spec-guided fix |
| Run the sync loop | `POST /sync/{phase}` | import\|asn\|invoice\|all |
| Partner integrations + activation | `GET /integrations` | which partners are live |
| Failure lessons | `GET /learning` | what the agent has learned |
| Unified dashboard data | `GET /api/dashboard` / `GET /api/judge` | judged agents + pipeline |
| Health / connector status | `GET /health` | dry vs live per connector |
| Report a bot's activity | `POST /events` (single or list) | `work_events` shape |

`/events` payload shape (matches `work_events`):
```json
{ "source": "sage_bot", "event_type": "erp_query", "subject": "order status 4500012345",
  "outcome": "resolved", "decision": "self_heal", "metadata": { "reward_signal": 0.8 } }
```

---

## 3. Agent-by-agent integration

- **`sage_bot`** — overlaps Order-API's Sage reads. Route order/inventory/invoice
  lookups for EDI POs through Order-API (`GET /order/{po}/status`, `/orders`,
  and its `SQLReader`) instead of a second Sage connector. Keep `sage_bot` as the
  department's general Sage interface for non-EDI queries.
- **`edi_monitor`** — overlaps Order-API's Orderful polling. **Decide one owner
  of the Orderful cursor** to avoid double-processing (the spec notes the P0
  cursor-ack bug). Recommended: Order-API's `POST /sync/import` owns inbound 850
  ingestion + cursor; `edi_monitor` watches transmission *health/acks* and, on a
  rejection, calls Order-API `POST /correct` with the partner error, then logs to
  `edi_incidents`. This makes the rejection→fix→relearn loop automatic.
- **`leadtime_bot` / `InboxBot`** — no direct overlap; they may enrich context
  by calling `GET /order/{po}/status` when an email/lead-time issue references a PO.
- **`CEO.py` scheduler** — add Order-API health to its checks (`GET /health`),
  and schedule `POST /sync/all` on the cadence that replaces the old
  logicbroker_sync cron.
- **Open Claw** — reads `GET /api/dashboard` / `GET /api/judge` for the morning
  briefing's EDI section; can drive Order-API via `POST /agent/message`.

---

## 4. Auth & secrets

- The Order-API currently has **no auth**. For cross-service calls add an
  `X-Api-Key` check mirroring the department's `CEO_API_KEY` pattern.
  **[Order-API task]** add an `ORDERAPI_KEY` env + header check; **[taskbot task]**
  send it from every call. Until then, bind Order-API to localhost only.
- Shared/needed env on the taskbot side: `ORDERAPI_BASE_URL`, `ORDERAPI_KEY`,
  plus the Order-API's own creds live in *its* `.env` (Orderful/ShipStation/
  SQL/ROI/Anthropic) — do not duplicate secrets across repos.

---

## 5. Reward / Judge convergence

Both systems implement the same reward table (spec Section 6.2). Keep ONE
implementation of record:
- If Supabase is canonical: the department's reward logic stays; Order-API
  mirrors events in and the department judges them.
- If Order-API `/dashboard` is canonical: department agents post to `/events`
  and `edi_agent/judge.py` is the judge.

Do not maintain two diverging reward tables — pick the canonical one and have the
other system feed it.

---

## 6. Concrete task list for Open Claw (on `elegant-tharpe`)

```
MERGE-001 [P0] Add ORDERAPI_BASE_URL (+ ORDERAPI_KEY) to env + docs/ENVIRONMENT.md.
MERGE-002 [P0] Build a thin OrderApiClient in the taskbot (requests/httpx) that
              wraps the endpoints in §2 and unwraps the {success,data,error}
              envelope, raising on success=false.
MERGE-003 [P1] Route sage_bot's EDI-PO lookups through OrderApiClient.get_order_status().
MERGE-004 [P1] Wire edi_monitor: on a partner rejection, call OrderApiClient.correct()
              with the error text; log result to edi_incidents.
MERGE-005 [P1] Decide cursor ownership (Order-API sync owns inbound) and remove
              the duplicate poll from edi_monitor.
MERGE-006 [P1] Choose the canonical dashboard (§1.2). Implement the one-directional
              event mirror accordingly.
MERGE-007 [P2] Add GET /health(Order-API) to CEO.py system-health checks + briefing.
MERGE-008 [P2] Open Claw morning briefing: pull GET /api/dashboard EDI section.
```

Corresponding **Order-API-side** asks (PR against its branch, not the taskbot):
```
OA-1  Add optional X-Api-Key auth (ORDERAPI_KEY).
OA-2  (If Supabase canonical) add a Supabase mirror of agent_events -> work_events.
OA-3  Confirm Orderful inbound shape + ROI create response once live creds exist
      (the two adapter points already flagged).
```

---

## 7. Inputs still needed [CONFIRM]

1. Taskbot repo location + cleaned-up module layout (so client calls map to real files).
2. Canonical dashboard: Supabase or Order-API `/dashboard`.
3. Order-API base URL / tunnel for the taskbot to reach.
4. Cursor ownership decision for Orderful inbound (recommended: Order-API).
5. Walmart confirmations still pending (companion-guide specifics) — unrelated to
   the merge but tracked in the Order-API README.

---

*Hand this to Open Claw after the directory cleanup. It will be refined once the
taskbot's final structure is shared.*
