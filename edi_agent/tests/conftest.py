"""Test environment setup — runs before any test module imports edi_agent.

Forces the deterministic chat path (no API key needed) and points the order
state machine at a throwaway SQLite file so tests never touch a real DB.
"""
import os
import tempfile

os.environ["EDI_AGENT_LLM"] = "0"

# Keep tests offline even when a developer has a real .env in the repo root.
# load_dotenv(..., override=False) will not overwrite these empty values.
for _name in (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "ORDERFUL_API_KEY",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "SHIPSTATION_API_KEY",
    "SHIPSTATION_API_SECRET",
    "SQL_SERVER_CONN",
):
    os.environ[_name] = ""

# Fresh state DB per test run, so persisted doc flags don't leak between runs.
_TEST_DB = os.path.join(tempfile.gettempdir(), "edi_state_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["STATE_DB_PATH"] = _TEST_DB

# Throwaway spec library directory.
import shutil  # noqa: E402
_SPECS = os.path.join(tempfile.gettempdir(), "edi_specs_test")
shutil.rmtree(_SPECS, ignore_errors=True)
os.environ["SPECS_DIR"] = _SPECS
