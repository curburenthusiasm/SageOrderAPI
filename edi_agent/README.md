# EDI Agent

A conversational EDI processing agent. Accept an inbound X12 **850** (Purchase
Order), parse it into a structured model, and produce the four outbound
documents — **997**, **855**, **856**, **810** — through a chat-style web page.
Outbound docs are validated against the X12 envelope spec, then submitted via
the Orderful API.

```
edi_agent/
├── core/
│   ├── models.py       # Order / LineItem / Party dataclasses
│   ├── parser.py       # X12 850 -> internal model (auto-detects delimiters)
│   ├── validator.py    # envelope + segment-count validation
│   └── envelope.py     # ISA/GS/ST wrapper, stateful control numbers
├── generators/
│   ├── gen_997.py      # Functional Acknowledgment
│   ├── gen_855.py      # PO Acknowledgment
│   ├── gen_856.py      # Ship Notice / ASN
│   └── gen_810.py      # Invoice
├── connectors/
│   ├── orderful.py     # submit to Orderful (dry-run when no API key)
│   ├── sql_reader.py   # product/inventory from SQL Server (Phase 2, optional)
│   └── shipping.py     # tracking/ASN data from a shipping API (Phase 2, optional)
├── conversation.py     # Claude-powered conversational layer (tool use)
├── static/index.html   # chat web UI + document viewer
├── agent.py            # FastAPI app + conversational brain
├── mappings.py         # human-editable field overrides
├── config.py           # env-driven config
└── tests/              # sample_850.edi + test_roundtrip.py + test_phase2.py
```

## Run it

```bash
cd edi_agent
pip install -r requirements.txt
cp .env.example .env          # optional; fill in Orderful creds for live submit
cd ..
uvicorn edi_agent.agent:app --reload --port 8000
```

Open **http://localhost:8000** and talk to the agent:

1. Click **Load sample 850** (or paste your own 850) and **Send**.
   The agent parses it and immediately generates + submits the **997**.
2. Give it your mappings in plain language:
   `set price ABC123 29.99`, `carrier UPSN`, `ship method GROUND`,
   `ack code AC`.
3. `generate 855` — the PO acknowledgment, validated and shown in the viewer.
4. When you're ready to ship: `ship date 20250605`,
   `tracking 1Z999AA10123456784`, then `generate all` (or `generate 856` /
   `generate 810`). Add `submit` / `send` to push to Orderful.

Type `help` in the chat for the full command list. Generated EDI appears in the
right-hand panel where you can copy or download it.

## Conversational layer (Claude API)

`/chat` has two backends, chosen automatically:

- **AI mode** — when `ANTHROPIC_API_KEY` is set, the chat is driven by Claude
  (`claude-opus-4-8`, adaptive thinking) with a tool surface that runs the
  pipeline: `parse_inbound_850`, `update_mappings`, `generate_document`,
  `set_ship_data`, `get_order`, `get_status`. You can talk to it freely
  ("acknowledge this PO at $31.50 a unit and invoice it net 30").
- **Command mode** — with no key, the built-in deterministic parser handles the
  same workflow via explicit commands. No external dependency; document
  generation stays fully reproducible.

The header pill shows `mode: AI` vs `mode: commands`. Force command mode with
`EDI_AGENT_LLM=0`.

## Submission mode

Without `ORDERFUL_API_KEY` the connector runs in **dry-run** mode and returns a
`SIMULATED-…` transaction id, so you can exercise the whole pipeline locally.
Set the key in `.env` to submit for real. The header pill shows `live` vs
`dry-run`.

## Phase 2 — live data, ship automation & state machine

- **SQL Server / Sage 100** (`connectors/sql_reader.py`) — `SQLReader` with
  `get_order` / `get_order_lines` / `get_invoice` / `get_invoice_lines` /
  `get_freight` over the Sage 100 tables (`so_salesorderheader`, …,
  `ar_invoicehistoryheader`). pyodbc, 5 s query timeout, connection pooling.
  `POST /order/{po}/enrich-prices` fills missing SKU prices. Optional — no-op dry
  mode when `pyodbc`/`SQL_SERVER_CONN` are absent.
- **ShipStation** (`connectors/shipping.py`) — `ShippingConnector` pulls shipped
  records per PO (Basic auth, rate-limit backoff), maps `carrierCode` → X12 via
  `CARRIER_MAP`, and normalizes to ship date/time + `packages`. Dry mode otherwise.
- **Ship auto-trigger** — `POST /order/{po}/ship` (or `/webhook/ship`) sets ship
  data (from the payload, falling back to ShipStation) and auto-generates +
  submits the **856** then the **810**. Supports split shipments via `packages`
  (one `HL*S` loop per box).
- **Order state machine** (`order_state.py`) — per-PO document status persisted in
  SQLite (`edi_state.db`): `RECEIVED → 997_SENT → 855_SENT → SHIPPED → INVOICED`,
  with transaction ids. Surfaced via `/order/{po}/status` and `/orders`.

## Onboarding a new integration (partner spec library)

The goal: when a new trading partner shows up, you hand the agent their EDI
**companion guides** and it's ready to build that partner's documents.

1. **Upload the specs** — `POST /specs` (multipart: `doc_type`, `trading_partner`,
   `file`) for each PDF guide (855, 856, 810, …), or use **Upload spec** in the
   web UI. Specs are keyed by `(trading_partner, doc_type)`; the set of specs for
   one partner *is* the integration (`GET /integrations`).
2. **Generation conforms to the spec** — whenever a doc is generated for that
   partner, the deterministic generator builds a valid baseline, then (if
   `ANTHROPIC_API_KEY` is set) Claude tailors it to the companion guide and our
   validator re-checks it. No key, bad PDF, or invalid output → it safely falls
   back to the baseline. `status.spec_notes` records what happened per doc.
3. **Build + test the workflow** — `POST /integrations/{partner}/build` with a
   `sample_850` parses it and runs every doc type the partner has a spec for
   (997 always) through generate → validate (→ submit if asked), returning a
   per-doc report you can snapshot into tests.

## OpenClaw / external-agent integration

Every JSON endpoint returns a consistent envelope so an external agent
(OpenClaw, or anything else) can drive the pipeline over plain REST:

```json
{ "success": true, "data": { ... }, "error": null }
```

Errors use the same shape (`success:false`, `error:{message,status}`) with the
right HTTP code. Two ways to drive it:

- **Call the REST endpoints directly** — e.g. `POST /order/4521/ship` with the
  ship body; interpret `data` / `error`.
- **`POST /agent/message`** — a single natural-language entry point
  (`{message, po_number?}`) that routes through the same conversational brain as
  the web UI and returns the envelope. OpenClaw owns the reasoning; this app is
  the tool backend.

## REST API (also used by the UI)

All responses use the `{success, data, error}` envelope above.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/850/inbound` | Receive raw EDI, parse, fire the 997 |
| GET  | `/order/{po}` | Parsed order + mappings + doc status |
| POST | `/order/{po}/mappings` | Update field mappings |
| POST | `/order/{po}/generate/{doc_type}` | Generate (optionally `?submit=true`) |
| POST | `/order/{po}/ship` | Set ship data → auto 856 + 810 |
| POST | `/order/{po}/invoice` | Generate/submit the 810 only |
| GET  | `/order/{po}/status` | Persisted document state for a PO |
| GET  | `/orders` | List all POs and their state |
| GET  | `/order/{po}/output/{doc_type}` | Return generated EDI |
| POST | `/order/{po}/import-to-sage` | Create the Sage 100 sales order via ROI InSynch |
| POST | `/order/{po}/enrich-prices` | Fill SKU prices from SQL Server |
| POST | `/webhook/ship` | Ship event → auto 856 + 810 (PO in body) |
| POST | `/specs` | Upload a partner companion-guide PDF (multipart) |
| GET  | `/specs` | List uploaded specs (optional `?trading_partner=`) |
| DELETE | `/specs/{id}` | Remove a spec |
| GET  | `/integrations` | Partners onboarded + doc types their specs cover |
| POST | `/integrations/{partner}/build` | Run a sample 850 through every covered doc |
| POST | `/chat` | Conversational driver for the web UI |
| POST | `/agent/message` | NL entry point for OpenClaw / external agents |

## Tests

```bash
python -m pytest edi_agent/tests -q
```

Roundtrip (parse → all four docs → validate, incl. split-shipment 856) plus the
Phase 2 suite (connectors, state machine, endpoints + envelope, `/agent/message`).

## Go-live integrations

At go-live the agent is wired to four external systems:

| System | Role | Status |
|--------|------|--------|
| **Orderful API** | EDI transport — submit outbound 997/855/856/810, receive 850 | wired (`connectors/orderful.py`), dry-run without key |
| **ShipStation API** | tracking / ASN data → 856 & 810 | wired (`connectors/shipping.py`), dry-run without keys |
| **SQL Server (Sage 100)** | read product/inventory/order/invoice data | wired (`connectors/sql_reader.py`), dry-run without conn string |
| **ROI InSynch API** | write orders into Sage 100 (`POST /api/v2/sales_order_headers`) | wired (`connectors/roi_insynch.py`), dry-run without `AZURE_CLIENT_*` |

## Notes / scope

- **Phase 1 (done):** models, parser, all four generators (997/855/856/810),
  envelope, FastAPI + web UI, roundtrip tests.
- **Phase 2 (done):** Sage 100 `SQLReader`, ShipStation `ShippingConnector`,
  ship auto-trigger (856 + 810) with split-shipment `packages`, SQLite order
  state machine, the `{success,data,error}` envelope + `/agent/message` for
  OpenClaw, and `conversation.py` (Claude-powered chat with tool use).
- **Integrations (done):** partner spec library (`spec_store.py`) + spec-guided
  generation (`spec_generator.py`) — upload companion-guide PDFs, build/validate
  any partner's docs from a sample 850.
- Trading-partner IDs are never hardcoded — they come from config or are parsed
  off the inbound 850. Control numbers are stateful (`control_numbers.json`).
- The Sage 100 query stubs use standard column names but **should be confirmed
  against the real schema** (run `SELECT TOP 1 *` on the two tables) and adjusted
  via the `SQL_*_QUERY` env vars before going live.
