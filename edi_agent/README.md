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

## Phase 2 — live data & ship automation

- **SQL Server** (`connectors/sql_reader.py`) — pulls product/inventory from the
  MAS_JEF database (pyodbc, same style as `main.py`). Configure `SQL_SERVER_CONN`;
  the product/inventory SQL is env-overridable (`SQL_PRODUCT_QUERY`,
  `SQL_INVENTORY_QUERY`) to match the live schema. `POST /order/{po}/enrich-prices`
  fills missing SKU prices from the product master. Optional — runs in a no-op
  dry mode when `pyodbc`/`SQL_SERVER_CONN` are absent.
- **Shipping API** (`connectors/shipping.py`) — pulls tracking/ship details per PO.
  Configure `SHIPPING_API_KEY` + `SHIPPING_API_BASE_URL`. Dry mode otherwise.
- **Ship-event webhook** — `POST /webhook/ship` with `{po_number, ...}` sets the
  ship data (from the payload, falling back to the shipping API) and
  auto-generates + submits the **856** and **810**.

## REST API (also used by the UI)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/850/inbound` | Receive raw EDI, parse, fire the 997 |
| GET  | `/order/{po}` | Parsed order + mappings + doc status |
| POST | `/order/{po}/mappings` | Update field mappings |
| POST | `/order/{po}/generate/{doc_type}` | Generate (optionally `?submit=true`) |
| POST | `/order/{po}/ship` | Set ship data, generate 856 + 810 |
| GET  | `/order/{po}/output/{doc_type}` | Return generated EDI |
| POST | `/order/{po}/enrich-prices` | Fill SKU prices from SQL Server (Phase 2) |
| POST | `/webhook/ship` | Ship event → auto 856 + 810 (Phase 2) |
| POST | `/chat` | Conversational driver for the web UI |

## Tests

```bash
python -m pytest edi_agent/tests -q
```

Parses the sample 850, generates all four documents, and validates each.

## Notes / scope

- **Phase 1 (done):** models, parser, all four generators (997/855/856/810),
  envelope, FastAPI + web UI, roundtrip tests.
- **Phase 2 (done):** `connectors/sql_reader.py` (live product/inventory from
  SQL Server), `connectors/shipping.py` (tracking data), ship-event webhook that
  auto-triggers 856/810, and `conversation.py` (Claude-powered chat with tool use).
- Trading-partner IDs are never hardcoded — they come from config or are parsed
  off the inbound 850. Control numbers are stateful (`control_numbers.json`).
- The SQL product/inventory queries ship with sensible Sage/MAS defaults but
  **must be pointed at the real table/column names** (via `SQL_PRODUCT_QUERY` /
  `SQL_INVENTORY_QUERY`) before going live.
