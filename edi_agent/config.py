"""Runtime configuration sourced from environment variables.

Nothing here is a secret default -- production values come from the
environment (see ``.env.example``). Trading-partner IDs are never hardcoded;
they come from config or are parsed off the inbound 850.
"""
import os

# Load a local .env (project root or edi_agent/.env) for development. No-op if
# python-dotenv isn't installed or no file exists. Real deployments use the
# process environment.
try:
    from dotenv import load_dotenv
    _here = os.path.dirname(__file__)
    for _candidate in (
        os.path.join(_here, ".env"),
        os.path.join(os.path.dirname(_here), ".env"),
    ):
        if os.path.exists(_candidate):
            load_dotenv(_candidate, override=False)
            break
except Exception:  # noqa: BLE001 - dotenv optional
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class Config:
    # --- Orderful API ---
    ORDERFUL_API_KEY = _env("ORDERFUL_API_KEY")
    ORDERFUL_BASE_URL = _env("ORDERFUL_BASE_URL", "https://api.orderful.com/v2")

    # --- Our (vendor) interchange identity ---
    VENDOR_ISA_ID = _env("VENDOR_ISA_ID", "JEFFCOFIBRES")
    VENDOR_ISA_QUALIFIER = _env("VENDOR_ISA_QUALIFIER", "ZZ")

    # --- Downstream data sources (Phase 2) ---
    SQL_SERVER_CONN = _env("SQL_SERVER_CONN")

    # --- ROI InSynch API (write orders into Sage 100) ---
    ROI_BASE_URL = _env("ROI_BASE_URL", "https://roiconsultingapi.azurewebsites.net")
    ROI_COMPANY_CODE = _env("ROI_COMPANY_CODE", "JEF")
    ROI_API_SCOPE = _env("ROI_API_SCOPE", "api://17d2b604-8930-40bc-81ab-583fdca33c6f/.default")
    # Azure AD client-credentials (same identity as main.py; values come from env).
    ROI_TOKEN_URL = _env(
        "ROI_TOKEN_URL",
        "https://login.microsoftonline.com/974b2ee7-8fca-4ab4-948a-02becfbf058f/oauth2/v2.0/token",
    )
    AZURE_CLIENT_ID = _env("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET = _env("AZURE_CLIENT_SECRET")
    # Sage defaults applied when an order doesn't carry them.
    ROI_CUSTOMER_NO = _env("ROI_CUSTOMER_NO", "")
    ROI_AR_DIVISION_NO = _env("ROI_AR_DIVISION_NO", "00")
    ROI_WAREHOUSE_CODE = _env("ROI_WAREHOUSE_CODE", "000")

    # ShipStation REST API (Basic auth: base64 of "key:secret").
    SHIPSTATION_API_KEY = _env("SHIPSTATION_API_KEY")
    SHIPSTATION_API_SECRET = _env("SHIPSTATION_API_SECRET")
    SHIPSTATION_BASE_URL = _env("SHIPSTATION_BASE_URL", "https://ssapi.shipstation.com")

    # --- Order state machine (SQLite) ---
    STATE_DB_PATH = _env(
        "STATE_DB_PATH",
        os.path.join(os.path.dirname(__file__), "edi_state.db"),
    )

    # --- Partner EDI spec / companion-guide library ---
    SPECS_DIR = _env("SPECS_DIR", os.path.join(os.path.dirname(__file__), "specs"))
    # Set EDI_SPEC_GUIDED=0 to disable the Claude spec-tailoring pass.
    SPEC_GUIDED = _env("EDI_SPEC_GUIDED", "1") != "0"

    # --- Envelope defaults ---
    X12_VERSION = _env("X12_VERSION", "004010")
    USAGE_INDICATOR = _env("EDI_USAGE_INDICATOR", "P")  # P=production, T=test

    # --- Outbound document defaults (partner requirements, e.g. Walmart) ---
    GS1_COMPANY_PREFIX = _env("GS1_COMPANY_PREFIX", "")   # for SSCC-18 carton labels (856 MAN*GM)
    SHIP_FROM_NAME = _env("SHIP_FROM_NAME", "Jeffco Fibres")
    SHIP_FROM_ADDRESS = _env("SHIP_FROM_ADDRESS", "12 Park Street")
    SHIP_FROM_CITY = _env("SHIP_FROM_CITY", "Webster")
    SHIP_FROM_STATE = _env("SHIP_FROM_STATE", "MA")
    SHIP_FROM_ZIP = _env("SHIP_FROM_ZIP", "01570")

    @classmethod
    def as_dict(cls) -> dict:
        """Non-secret view of config for the UI/status endpoint."""
        return {
            "orderful_base_url": cls.ORDERFUL_BASE_URL,
            "orderful_configured": bool(cls.ORDERFUL_API_KEY),
            "vendor_isa_id": cls.VENDOR_ISA_ID,
            "vendor_isa_qualifier": cls.VENDOR_ISA_QUALIFIER,
            "x12_version": cls.X12_VERSION,
            "usage_indicator": cls.USAGE_INDICATOR,
        }


config = Config()
