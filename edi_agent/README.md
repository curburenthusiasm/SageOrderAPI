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
│   └── orderful.py     # submit to Orderful (dry-run when no API key)
├── static/index.html   # chat web UI + document viewer
├── agent.py            # FastAPI app + conversational brain
├── mappings.py         # human-editable field overrides
├── config.py           # env-driven config
└── tests/              # sample_850.edi + test_roundtrip.py
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

## Submission mode

Without `ORDERFUL_API_KEY` the connector runs in **dry-run** mode and returns a
`SIMULATED-…` transaction id, so you can exercise the whole pipeline locally.
Set the key in `.env` to submit for real. The header pill shows `live` vs
`dry-run`.

## REST API (also used by the UI)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/850/inbound` | Receive raw EDI, parse, fire the 997 |
| GET  | `/order/{po}` | Parsed order + mappings + doc status |
| POST | `/order/{po}/mappings` | Update field mappings |
| POST | `/order/{po}/generate/{doc_type}` | Generate (optionally `?submit=true`) |
| POST | `/order/{po}/ship` | Set ship data, generate 856 + 810 |
| GET  | `/order/{po}/output/{doc_type}` | Return generated EDI |
| POST | `/chat` | Conversational driver for the web UI |

## Tests

```bash
python -m pytest edi_agent/tests -q
```

Parses the sample 850, generates all four documents, and validates each.

## Notes / scope

- **Phase 1 (done):** models, parser, 997, 855, envelope, FastAPI + web UI,
  roundtrip tests. The 856 and 810 generators are included too.
- **Phase 2 (next):** `connectors/sql_reader.py` (live product/inventory from
  SQL Server), `connectors/shipping.py` (tracking data), and an auto-trigger of
  856/810 on a ship-event webhook.
- Trading-partner IDs are never hardcoded — they come from config or are parsed
  off the inbound 850. Control numbers are stateful (`control_numbers.json`).
- Conversation is handled by a deterministic intent parser (no external LLM
  dependency), so document production is reproducible.
