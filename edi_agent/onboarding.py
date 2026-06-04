"""onboarding.py — Autonomous partner onboarding pipeline.

Given a spec file (PDF, Excel, JSON, raw X12 sample, or OpenAPI/Swagger docs)
and partner metadata, this module:

  1. Saves the spec file
  2. Parses it into a structured FieldMap (LLM-powered)
  3. Routes to the right platform:
       - OpenAPI/Swagger detected → rest_api path (direct connector)
       - EDI spec → tray path (Tray.io workflow) or orderful (if partner is on their network)
  4. Builds the workflow / connector config
  5. Registers the partner in Supabase (edi_partners table)
  6. Returns a status summary

Every step is logged to the partner's onboarding_log in Supabase so the
brain can surface progress / errors without polling the file system.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Spec storage — within the SageOrderAPI working directory
SPEC_DIR = Path(os.getenv("SPEC_DIR", Path(__file__).parent.parent / "specs"))
SPEC_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def onboard_partner(
    name: str,
    spec_bytes: bytes,
    spec_filename: str,
    *,
    isa_qualifier: str | None = None,
    orderful_partner_id: str | None = None,  # if already on Orderful network → skip Tray
    force_platform: str | None = None,        # override auto-detect: "orderful"|"tray"|"rest_api"
) -> dict[str, Any]:
    """Run the full onboarding pipeline for a new trading partner.

    :returns: {
        "ok": bool,
        "partner_id": str,
        "platform": str,
        "status": str,           # "active" | "pending_manual_step" | "error"
        "message": str,
        "details": dict,
    }
    """
    from .partner_registry import get_registry

    registry = get_registry()
    partner_id: str | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1 — Save the spec file
        # ------------------------------------------------------------------
        partner_slug = _slugify(name)
        spec_ext = Path(spec_filename).suffix.lower()
        spec_path = SPEC_DIR / f"{partner_slug}{spec_ext}"
        spec_path.write_bytes(spec_bytes)
        log.info(f"[{name}] Spec saved → {spec_path}")

        # ------------------------------------------------------------------
        # Step 2 — Register a stub record so we can log progress
        # ------------------------------------------------------------------
        record = registry.register(
            name=name,
            platform="pending",
            isa_qualifier=isa_qualifier,
            spec_path=str(spec_path),
            status="pending",
        )
        partner_id = record["id"]
        _log(registry, partner_id, "started", f"Spec saved: {spec_path.name}")

        # ------------------------------------------------------------------
        # Step 3 — Parse the spec
        # ------------------------------------------------------------------
        _log(registry, partner_id, "parsing", "Running spec parser…")
        spec = _parse_spec(spec_bytes, spec_filename, name)
        if spec.get("error"):
            _log(registry, partner_id, "parse_error", spec["error"])
            registry.update(partner_id, status="error", spec_raw=spec)
            return _result(False, partner_id, "error", spec["error"])

        registry.update(partner_id, spec_raw=spec)
        _log(registry, partner_id, "parsed", f"Detected doc types: {spec.get('doc_types', [])}")

        # ------------------------------------------------------------------
        # Step 4 — Platform routing
        # ------------------------------------------------------------------
        platform = force_platform or _route_platform(
            spec, spec_ext, orderful_partner_id=orderful_partner_id
        )
        _log(registry, partner_id, "routed", f"Platform: {platform}")
        registry.update(partner_id, platform=platform)

        # ------------------------------------------------------------------
        # Step 5 — Build the workflow / connector config
        # ------------------------------------------------------------------
        _log(registry, partner_id, "building", f"Building {platform} workflow…")

        if platform == "orderful":
            result = _onboard_orderful(name, spec, orderful_partner_id)

        elif platform == "tray":
            result = _onboard_tray(name, spec)

        elif platform == "rest_api":
            result = _onboard_rest_api(name, spec, spec_bytes, spec_filename)

        else:
            raise ValueError(f"Unknown platform: {platform}")

        # ------------------------------------------------------------------
        # Step 6 — Persist final config and mark active (or pending manual)
        # ------------------------------------------------------------------
        final_status = "active" if result.get("ok") and not result.get("manual_import_required") else "pending_manual_step"

        registry.update(
            partner_id,
            platform=platform,
            connector_config=result.get("connector_config", {}),
            workflow_ids=result.get("workflow_ids", {}),
            webhook_urls=result.get("webhook_urls", {}),
            status=final_status,
        )
        _log(registry, partner_id, final_status, result.get("message", "Done"))

        return _result(
            ok=result.get("ok", False),
            partner_id=partner_id,
            platform=platform,
            status=final_status,
            message=result.get("message", ""),
            details=result,
        )

    except Exception as exc:
        log.exception(f"[{name}] Onboarding failed: {exc}")
        if partner_id:
            try:
                registry.update(partner_id, status="error")
                _log(get_registry(), partner_id, "error", str(exc))
            except Exception:
                pass
        return _result(False, partner_id or "", "error", str(exc))


# ---------------------------------------------------------------------------
# Platform-specific onboarding handlers
# ---------------------------------------------------------------------------

def _onboard_orderful(name: str, spec: dict, partner_id_override: str | None) -> dict:
    """Partner is already on the Orderful network — just register their trading partner ID."""
    partner_id = partner_id_override or _slugify(name).upper()
    return {
        "ok": True,
        "message": f"Orderful partner registered: {partner_id}",
        "connector_config": {
            "trading_partner_id": partner_id,
        },
        "workflow_ids": {},
        "webhook_urls": {},
    }


def _onboard_tray(name: str, spec: dict) -> dict:
    """Build a Tray.io workflow from the parsed spec."""
    from .connectors.tray import TrayConnector

    connector = TrayConnector()
    doc_types = spec.get("doc_types") or ["850", "997", "855", "856", "810"]

    result = connector.create_workflow(name, spec, doc_types)

    if result.get("platform") == "tray_api" and result.get("ok"):
        # Fully automated — extract webhook URLs per doc type
        workflows = result.get("workflows", {})
        webhook_urls = {dt: info.get("webhook_url", "") for dt, info in workflows.items()}
        workflow_ids = {dt: info.get("workflow_id", "") for dt, info in workflows.items()}
        return {
            "ok": True,
            "message": f"Tray.io workflows created via API for {name}",
            "connector_config": {"platform": "tray"},
            "workflow_ids": workflow_ids,
            "webhook_urls": webhook_urls,
        }
    elif result.get("platform") == "tray_json_export":
        # No master token — generated JSON for manual import
        exports = result.get("exports", {})
        # Save export files so the user can import them
        _save_tray_exports(name, exports)
        return {
            "ok": True,
            "manual_import_required": True,
            "message": (
                f"Tray.io workflow JSONs generated for {name}. "
                f"Import them in Tray.io UI, then call PATCH /partners/{{id}} "
                f"with the webhook URLs. Add TRAY_MASTER_TOKEN to .env to skip this step."
            ),
            "connector_config": {"platform": "tray"},
            "workflow_ids": {},
            "webhook_urls": {},
            "exports": {dt: f"specs/{_slugify(name)}_tray_{dt}.json" for dt in exports},
        }
    else:
        return {"ok": False, "message": result.get("error", "Tray workflow creation failed")}


def _onboard_rest_api(name: str, spec: dict, api_docs_bytes: bytes, filename: str) -> dict:
    """Parse OpenAPI/Swagger docs and generate a RestApiConnector config."""
    api_schema = _parse_openapi(api_docs_bytes, filename)
    if not api_schema:
        return {"ok": False, "message": "Could not parse OpenAPI/Swagger docs"}

    # LLM-generate the field_map from EDI fields → partner API fields
    field_map = _generate_field_map(spec, api_schema)
    endpoints = _extract_endpoints(api_schema)

    connector_config = {
        "base_url": api_schema.get("base_url", ""),
        "auth": api_schema.get("auth_hint", {"type": "bearer", "token": "REPLACE_ME"}),
        "endpoints": endpoints,
        "field_map": field_map,
    }

    return {
        "ok": True,
        "message": (
            f"REST API connector configured for {name}. "
            f"Update connector_config.auth.token with the partner's API key."
        ),
        "connector_config": connector_config,
        "workflow_ids": {},
        "webhook_urls": {},
    }


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

def _parse_spec(spec_bytes: bytes, filename: str, partner: str) -> dict:
    """Parse any spec format → normalized spec dict.

    Returns a dict with at minimum:
      { partner, doc_types, field_map, transaction_sets, error? }
    """
    ext = Path(filename).suffix.lower()

    # Already-parsed JSON spec
    if ext == ".json":
        try:
            return json.loads(spec_bytes.decode("utf-8"))
        except Exception as exc:
            return {"error": f"JSON parse failed: {exc}"}

    # PDF implementation guide
    if ext == ".pdf":
        return _parse_pdf_spec(spec_bytes, partner)

    # Raw X12 EDI sample — extract structure from actual document
    if ext in (".edi", ".x12", ".txt") or _looks_like_x12(spec_bytes):
        return _parse_x12_sample(spec_bytes, partner)

    # Excel mapping guide
    if ext in (".xlsx", ".xls", ".csv"):
        return _parse_excel_spec(spec_bytes, filename, partner)

    # OpenAPI / Swagger (JSON or YAML) — treated as rest_api spec
    if ext in (".yaml", ".yml"):
        return _parse_openapi(spec_bytes, filename) or {"error": "Could not parse OpenAPI YAML"}

    # Try JSON fallback
    try:
        return json.loads(spec_bytes.decode("utf-8", errors="ignore"))
    except Exception:
        pass

    return {"error": f"Unsupported spec format: {ext}"}


def _parse_pdf_spec(pdf_bytes: bytes, partner: str) -> dict:
    """Extract EDI spec from a PDF implementation guide using GPT-4o."""
    try:
        # Write to temp file for pdfplumber
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        # Try to import the existing ceo-bot PDF parser
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ceo-bot"))
            from edi_spec_from_pdf import extract_pdf_text, llm_extract_spec  # type: ignore
            text, pages = extract_pdf_text(tmp_path)
            return llm_extract_spec(text, partner=partner, doc_type="850")
        except ImportError:
            pass

        # Fallback: extract text with pdfplumber and call OpenAI directly
        return _pdf_llm_fallback(tmp_path, partner)

    except Exception as exc:
        return {"error": f"PDF parse failed: {exc}", "partner": partner}
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _pdf_llm_fallback(pdf_path: str, partner: str) -> dict:
    """Minimal PDF → spec via pdfplumber + OpenAI."""
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:12]:  # first 12 pages covers most spec guides
                text += (page.extract_text() or "") + "\n"
    except ImportError:
        return {"error": "pdfplumber not installed (pip install pdfplumber)"}
    except Exception as exc:
        return {"error": f"PDF text extraction failed: {exc}"}

    return _llm_parse_spec_text(text, partner, "850")


def _parse_x12_sample(x12_bytes: bytes, partner: str) -> dict:
    """Derive spec structure from a raw X12 EDI sample file."""
    x12 = x12_bytes.decode("utf-8", errors="ignore").strip()
    segments = set()
    doc_type = "850"

    for seg in x12.split("~"):
        seg = seg.strip()
        if seg:
            tag = seg.split("*")[0].upper()
            segments.add(tag)
            if tag == "ST":
                elems = seg.split("*")
                doc_type = elems[1] if len(elems) > 1 else "850"

    return {
        "partner": partner,
        "doc_types": [doc_type],
        "source": "x12_sample",
        "segments_detected": sorted(segments),
        "field_map": {},  # LLM mapping skipped for raw samples — use the brain's parser instead
        "notes": "Derived from sample EDI file. Field map will be refined on first live transaction.",
    }


def _parse_excel_spec(excel_bytes: bytes, filename: str, partner: str) -> dict:
    """Parse an Excel field mapping spreadsheet."""
    try:
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(excel_bytes), read_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))

        # Build a text summary for the LLM
        header = rows[0] if rows else []
        sample = rows[1:20]
        text = f"Excel mapping guide for {partner}\n"
        text += "Columns: " + ", ".join(str(h) for h in header) + "\n"
        for row in sample:
            text += " | ".join(str(v) for v in row) + "\n"

        return _llm_parse_spec_text(text, partner, "850")

    except ImportError:
        return {"error": "openpyxl not installed (pip install openpyxl)"}
    except Exception as exc:
        return {"error": f"Excel parse failed: {exc}"}


def _llm_parse_spec_text(text: str, partner: str, doc_type: str) -> dict:
    """Send spec text to OpenAI and extract structured field map."""
    import openai

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "partner": partner,
            "doc_types": [doc_type],
            "source": "text",
            "field_map": {},
            "notes": "OPENAI_API_KEY not set — field map not generated. Add key to enable LLM parsing.",
        }

    client = openai.OpenAI(api_key=api_key)

    prompt = f"""You are an EDI integration specialist. Extract the EDI field mapping from this spec.

Partner: {partner}
Document Type: {doc_type}

Spec text (first 8000 chars):
{text[:8000]}

Return a JSON object with this structure:
{{
  "partner": "{partner}",
  "doc_types": ["850"],
  "transaction_sets": ["850"],
  "isa_qualifier": "<partner ISA qualifier if found, else null>",
  "field_map": {{
    "<EDI segment+element, e.g. BEG03>": {{
      "name": "<field name>",
      "requirement": "M|O|C",
      "data_type": "AN|DT|N2|ID|etc",
      "min_length": 1,
      "max_length": 30,
      "notes": "<any partner-specific notes>"
    }}
  }},
  "validation_rules": [],
  "notes": "<overall notes>"
}}
Return ONLY valid JSON, no markdown."""

    try:
        resp = client.chat.completions.create(
            model=os.getenv("EDI_PARSER_MODEL", "gpt-4o"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=4096,
        )
        content = resp.choices[0].message.content or ""
        return json.loads(content)
    except json.JSONDecodeError as exc:
        return {"error": f"LLM returned invalid JSON: {exc}", "partner": partner}
    except Exception as exc:
        return {"error": f"LLM call failed: {exc}", "partner": partner}


def _parse_openapi(docs_bytes: bytes, filename: str) -> dict | None:
    """Parse OpenAPI/Swagger YAML or JSON → normalized API schema dict."""
    try:
        ext = Path(filename).suffix.lower()
        if ext in (".yaml", ".yml"):
            import yaml
            schema = yaml.safe_load(docs_bytes.decode("utf-8", errors="ignore"))
        else:
            schema = json.loads(docs_bytes.decode("utf-8", errors="ignore"))

        servers = schema.get("servers", [])
        base_url = servers[0].get("url", "") if servers else schema.get("host", "")

        # Extract auth hints
        security = schema.get("components", {}).get("securitySchemes", {})
        auth_hint = {"type": "bearer", "token": "REPLACE_ME"}
        for scheme_name, scheme in security.items():
            if scheme.get("type") == "apiKey":
                auth_hint = {"type": "api_key", "header": scheme.get("name", "X-API-Key"), "token": "REPLACE_ME"}
            elif scheme.get("type") == "oauth2":
                flows = scheme.get("flows", {})
                cc = flows.get("clientCredentials", {})
                auth_hint = {
                    "type": "oauth2_client_credentials",
                    "token_url": cc.get("tokenUrl", ""),
                    "client_id": "REPLACE_ME",
                    "client_secret": "REPLACE_ME",
                }

        return {
            "base_url": base_url,
            "auth_hint": auth_hint,
            "paths": schema.get("paths", {}),
            "info": schema.get("info", {}),
            "components": schema.get("components", {}),
        }
    except Exception as exc:
        log.warning(f"OpenAPI parse failed: {exc}")
        return None


def _generate_field_map(edi_spec: dict, api_schema: dict) -> dict:
    """Use OpenAI to map EDI fields → partner REST API fields."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return {}

    import openai
    client = openai.OpenAI(api_key=api_key)

    edi_fields = list(edi_spec.get("field_map", {}).keys())[:30]
    api_paths = list(api_schema.get("paths", {}).keys())[:20]

    prompt = f"""Map EDI X12 fields to REST API fields.

EDI fields: {edi_fields}
API endpoints: {api_paths}
API schema (first 3000 chars): {json.dumps(api_schema)[:3000]}

Return a JSON object mapping EDI segment+element → API field path:
{{"BEG03": "purchase_order_number", "BEG05": "order_date", ...}}
Return ONLY valid JSON."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2048,
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as exc:
        log.warning(f"Field map generation failed: {exc}")
        return {}


def _extract_endpoints(api_schema: dict) -> dict:
    """Extract submit / status / acknowledge endpoints from OpenAPI schema."""
    paths = api_schema.get("paths", {})
    endpoints = {}

    for path, methods in paths.items():
        path_lower = path.lower()
        for method in methods.keys():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            endpoint = f"{method.upper()} {path}"
            # Heuristic matching
            if "order" in path_lower and method.upper() == "POST" and "submit_order" not in endpoints:
                endpoints["submit_order"] = endpoint
            elif "status" in path_lower and method.upper() == "GET" and "get_status" not in endpoints:
                endpoints["get_status"] = endpoint
            elif "ack" in path_lower and "acknowledge" not in endpoints:
                endpoints["acknowledge"] = endpoint
            elif "inbound" in path_lower and method.upper() == "GET" and "fetch_inbound" not in endpoints:
                endpoints["fetch_inbound"] = endpoint

    return endpoints


# ---------------------------------------------------------------------------
# Platform router
# ---------------------------------------------------------------------------

def _route_platform(spec: dict, spec_ext: str, orderful_partner_id: str | None = None) -> str:
    """Decide which platform to use based on spec type and partner capabilities."""
    # Explicit Orderful partner → use Orderful
    if orderful_partner_id:
        return "orderful"

    # OpenAPI/Swagger → direct REST API
    if spec_ext in (".yaml", ".yml") or spec.get("paths"):
        return "rest_api"

    # EDI spec or X12 sample → Tray.io workflow
    return "tray"


def _looks_like_x12(data: bytes) -> bool:
    """Quick check if bytes look like a raw X12 file."""
    try:
        text = data[:100].decode("utf-8", errors="ignore")
        return text.startswith("ISA")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tray export file saver
# ---------------------------------------------------------------------------

def _save_tray_exports(partner_name: str, exports: dict) -> None:
    """Save Tray.io workflow JSON export files to the specs directory."""
    slug = _slugify(partner_name)
    for doc_type, content in exports.items():
        path = SPEC_DIR / f"{slug}_tray_{doc_type}.json"
        try:
            path.write_text(json.dumps(content, indent=2))
            log.info(f"Tray export saved → {path}")
        except Exception as exc:
            log.warning(f"Could not save Tray export for {doc_type}: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9_]", "_", text.lower().strip())


def _log(registry, partner_id: str, event: str, message: str) -> None:
    try:
        registry.append_log(partner_id, {"event": event, "message": message})
    except Exception as exc:
        log.warning(f"Could not append log for {partner_id}: {exc}")


def _result(
    ok: bool,
    partner_id: str,
    status: str,
    message: str,
    platform: str = "",
    details: dict | None = None,
) -> dict:
    return {
        "ok": ok,
        "partner_id": partner_id,
        "platform": platform,
        "status": status,
        "message": message,
        "details": details or {},
    }
