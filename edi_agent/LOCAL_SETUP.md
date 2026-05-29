# Local setup — running the EDI Agent in PyCharm

Get the stack onto your machine and start testing. Everything runs in **dry
mode** until you add credentials, so you can click around safely first.

## 1. Get the code

The work lives on the branch `claude/agent-webpage-interface-JMjNv` (PR #2).

**Option A — PyCharm's Git UI**
1. **File ▸ Project from Version Control…**
2. URL: `https://github.com/curburenthusiasm/SageOrderAPI.git` → **Clone**.
3. Bottom-right branch widget (or **Git ▸ Branches**) → **Remote ▸
   `origin/claude/agent-webpage-interface-JMjNv` ▸ Checkout**.

**Option B — terminal (PyCharm ▸ View ▸ Tool Windows ▸ Terminal)**
```bash
git clone https://github.com/curburenthusiasm/SageOrderAPI.git
cd SageOrderAPI
git checkout claude/agent-webpage-interface-JMjNv
```

## 2. Create the interpreter (venv)

1. **Settings ▸ Project ▸ Python Interpreter ▸ Add Interpreter ▸ Add Local ▸
   Virtualenv**, base interpreter **Python 3.11+**, location `./.venv`. **OK**.
2. In the PyCharm terminal (venv now active — prompt shows `(.venv)`):
   ```bash
   pip install -r edi_agent/requirements.txt
   ```
   > `pyodbc` is commented out (it needs the Microsoft ODBC driver). Leave it
   > off until you're wiring the live SQL Server connection — the app runs fine
   > without it (SQL just stays in dry mode).

## 3. Configure (optional for first run)

```bash
cp edi_agent/.env.example edi_agent/.env
```
Edit `edi_agent/.env` and fill in only what you want to test live. **Leave a key
blank to keep that integration in dry mode.** Notable ones:

| Var | Turns on |
|-----|----------|
| `ANTHROPIC_API_KEY` | AI chat + spec-guided generation |
| `ORDERFUL_API_KEY` | real EDI submit/poll (else `SIMULATED-…`) |
| `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | ROI InSynch → Sage order create |
| `SHIPSTATION_API_KEY` / `SHIPSTATION_API_SECRET` | tracking → 856/810 |
| `SQL_SERVER_CONN` | live Sage reads (needs `pyodbc` + ODBC driver) |

`edi_agent/.env` is gitignored — your secrets won't be committed.

## 4. Run the web app

**Run config:** **Run ▸ Edit Configurations… ▸ + ▸ Python**
- **Module name** (not script): `uvicorn`
- **Parameters:** `edi_agent.agent:app --reload --port 8000`
- **Working directory:** the repo root (`…/SageOrderAPI`)
- **OK**, then **Run** ▶.

Open **http://localhost:8000**. Click **Load sample 850 → Send** to watch the
whole flow; tabs on the right show the generated 997/855/856/810. Interactive
API docs are at **http://localhost:8000/docs**.

> Terminal equivalent: `uvicorn edi_agent.agent:app --reload --port 8000`

## 5. Run the sync orchestrator

**Run config:** **+ ▸ Python**
- **Module name:** `edi_agent.orderful_sync`
- **Parameters:** `--phase import --sample-850 edi_agent/tests/sample_850.edi`
- **Working directory:** repo root → **Run** ▶.

Drop `--sample-850 …` to poll Orderful for real (needs `ORDERFUL_API_KEY`); add
`--dry-run` to plan without submitting. For go-live, point Windows Task
Scheduler at `python -m edi_agent.orderful_sync` on an interval.

## 6. Run the tests

Terminal:
```bash
python -m pytest edi_agent/tests -q
```
Or right-click the `edi_agent/tests` folder ▸ **Run 'pytest in tests'**. All
tests run fully offline (dry mode) — no credentials required.

## Smoke checklist
- `GET http://localhost:8000/health` → `{"success": true, ...}` with the
  `*_configured` flags showing what's live vs dry.
- Web UI: Load sample 850 → Send → see 997/855, then `generate all`.
- `python -m edi_agent.orderful_sync --phase import --sample-850 edi_agent/tests/sample_850.edi`
  → `imported=1`.
