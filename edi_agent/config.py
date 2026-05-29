"""Runtime configuration sourced from environment variables.

Nothing here is a secret default -- production values come from the
environment (see ``.env.example``). Trading-partner IDs are never hardcoded;
they come from config or are parsed off the inbound 850.
"""
import os


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
