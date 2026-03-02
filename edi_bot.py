from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None

try:
    import ollama
except ImportError:
    ollama = None

logger = logging.getLogger(__name__)

EDI_MODEL = os.getenv("EDI_MODEL", "qwen2.5:7b-instruct")
EDI_TEMPLATE_DIR = Path("knowledge_base/edi/templates")
EDI_TEMPLATE_INDEX = Path("knowledge_base/edi/templates_index.json")
EDI_MEMORY_FILE = Path("edi_memory.jsonl")


def _ensure_dirs() -> None:
    EDI_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    EDI_TEMPLATE_INDEX.parent.mkdir(parents=True, exist_ok=True)


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "customer"


def _read_spec_text(edi_spec: Optional[str], spec_path: Optional[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "raw_text": "",
        "source": "",
        "structured": None,
    }

    if spec_path:
        path = Path(spec_path)
        if not path.exists():
            return {"error": f"Spec path not found: {spec_path}"}
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_bytes().decode("utf-8", errors="ignore")
        result["raw_text"] = raw
        result["source"] = str(path)
    elif edi_spec:
        result["raw_text"] = edi_spec
        result["source"] = "inline"
    else:
        return {"error": "No EDI spec provided. Provide edi_spec text or spec_path."}

    try:
        structured = json.loads(result["raw_text"])
        result["structured"] = structured
    except Exception:
        result["structured"] = None

    return result


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = [line for line in lines if not line.strip().startswith("```")]
        return "\n".join(lines).strip()
    return stripped


def _build_basic_template(customer_name: str, spec_text: str, source: str) -> Dict[str, Any]:
    return {
        "metadata": {
            "customer": customer_name,
            "generated_at": datetime.now().isoformat(),
            "source": source,
            "template_version": "1.0",
        },
        "trading_partner": {
            "name": customer_name,
            "partner_ids": [],
            "communications": {
                "transport": "",
                "settings": {},
            },
        },
        "documents": [],
        "envelope": {
            "isa": {},
            "gs": {},
        },
        "acknowledgments": {
            "use_997": True,
            "use_999": False,
            "timing_hours": 24,
        },
        "validation_rules": [],
        "mapping_rules": [],
        "trayio_workflow_skeleton": {
            "name": f"{customer_name} EDI One-Shot",
            "steps": [
                "Receive EDI file",
                "Parse EDI",
                "Validate",
                "Transform to internal format",
                "Route to downstream system",
                "Log and notify",
            ],
        },
        "raw_spec": spec_text[:10000],
    }


def _build_template_with_llm(
    customer_name: str,
    spec_text: str,
    structured_spec: Optional[Dict[str, Any]],
    source: str,
) -> Dict[str, Any]:
    if not ollama:
        logger.warning("ollama not installed; using basic template")
        return _build_basic_template(customer_name, spec_text, source)

    system_prompt = (
        "You are an EDI integration architect. Create a one-shot EDI template JSON "
        "for a trading partner based on the provided EDI specification. "
        "Return ONLY valid JSON. No markdown, no extra text."
    )

    user_payload = {
        "customer_name": customer_name,
        "spec_source": source,
        "spec_text": spec_text[:12000],
        "structured_spec": structured_spec,
        "required_sections": [
            "metadata",
            "trading_partner",
            "documents",
            "envelope",
            "acknowledgments",
            "validation_rules",
            "mapping_rules",
            "trayio_workflow_skeleton",
            "testing_plan",
        ],
        "document_guidance": [
            "Include doc types like 850/810/856/997 if present.",
            "For each doc: segments, loops, key fields, required elements.",
            "Include code lists and qualifiers if specified.",
        ],
    }

    try:
        response = ollama.chat(
            model=EDI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, indent=2)},
            ],
        )
        content = response["message"]["content"]
        content = _strip_code_fences(content)
        template = json.loads(content)
        template.setdefault("metadata", {})
        template["metadata"].update(
            {
                "customer": customer_name,
                "generated_at": datetime.now().isoformat(),
                "source": source,
                "template_version": "1.0",
                "generated_by": "ollama",
                "model": EDI_MODEL,
            }
        )
        return template
    except Exception as exc:
        logger.error(f"LLM template generation failed: {exc}")
        fallback = _build_basic_template(customer_name, spec_text, source)
        fallback["metadata"]["generation_error"] = str(exc)
        return fallback


def _load_template_index() -> List[Dict[str, Any]]:
    if not EDI_TEMPLATE_INDEX.exists():
        return []
    try:
        return json.loads(EDI_TEMPLATE_INDEX.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Failed to read template index: {exc}")
        return []


def _save_template_index(index: List[Dict[str, Any]]) -> None:
    EDI_TEMPLATE_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")


def log_memory(action: str, details: Dict[str, Any]) -> None:
    entry = {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "details": details,
    }
    with EDI_MEMORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def read_memory(limit: int = 50, customer: Optional[str] = None) -> List[Dict[str, Any]]:
    if not EDI_MEMORY_FILE.exists():
        return []
    entries: List[Dict[str, Any]] = []
    with EDI_MEMORY_FILE.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if customer:
                details = entry.get("details", {})
                if details.get("customer") != customer:
                    continue
            entries.append(entry)
    return entries[-limit:]


class TrayIoClient:
    def __init__(self) -> None:
        self.api_token = os.getenv("TRAY_IO_API_TOKEN", "")
        self.base_url = self._build_base_url()

    def _build_base_url(self) -> str:
        base = os.getenv("TRAY_IO_API_BASE", "").strip()
        if base:
            return base.rstrip("/")
        base = os.getenv("TRAY_IO_URL", "https://app.tray.io").rstrip("/")
        return f"{base}/api/v1"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def available(self) -> bool:
        return bool(self.api_token and requests)

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not requests:
            return {"ok": False, "error": "requests not installed"}
        if not self.api_token:
            return {"ok": False, "error": "TRAY_IO_API_TOKEN not configured"}
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": response.text}
            if response.text:
                return {"ok": True, "data": response.json()}
            return {"ok": True, "data": {}}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_workflows(self) -> Dict[str, Any]:
        return self._request("GET", "/workflows")

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}")

    def find_workflow_id_by_name(self, name: str) -> Optional[str]:
        result = self.list_workflows()
        if not result.get("ok"):
            return None
        data = result.get("data", {})
        workflows = data.get("data") if isinstance(data, dict) else data
        if not workflows:
            return None
        for workflow in workflows:
            if workflow.get("name", "").lower() == name.lower():
                return workflow.get("id") or workflow.get("workflow_id")
        return None

    def set_workflow_status(self, workflow_id: str, status: str) -> Dict[str, Any]:
        payload = {"status": status}
        return self._request("PATCH", f"/workflows/{workflow_id}", payload)

    def trigger_workflow(self, workflow_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", f"/workflows/{workflow_id}/runs", payload or {})


class EDIBot:
    def __init__(self) -> None:
        self.name = "EDIBot"
        _ensure_dirs()
        self.tray_client = TrayIoClient()

    def check_status(self) -> Dict[str, Any]:
        try:
            template_count = len(list(EDI_TEMPLATE_DIR.glob("*.json")))
            memory_available = EDI_MEMORY_FILE.exists()
            tray_ready = self.tray_client.available()
            return {
                "ok": True,
                "templates": template_count,
                "memory_log": "present" if memory_available else "missing",
                "tray_api": "ready" if tray_ready else "not_configured",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def create_one_shot_template(
        self,
        customer_name: str,
        edi_spec: Optional[str] = None,
        spec_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not customer_name:
            return {"ok": False, "error": "customer_name is required"}

        spec_result = _read_spec_text(edi_spec, spec_path)
        if "error" in spec_result:
            return {"ok": False, "error": spec_result["error"]}

        template = _build_template_with_llm(
            customer_name=customer_name,
            spec_text=spec_result["raw_text"],
            structured_spec=spec_result["structured"],
            source=spec_result["source"],
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{_slugify(customer_name)}_{timestamp}.json"
        template_path = EDI_TEMPLATE_DIR / filename
        template_path.write_text(json.dumps(template, indent=2), encoding="utf-8")

        index = _load_template_index()
        index_entry = {
            "customer": customer_name,
            "created_at": datetime.now().isoformat(),
            "template_path": str(template_path),
        }
        index.append(index_entry)
        _save_template_index(index)

        log_memory(
            "template_created",
            {
                "customer": customer_name,
                "template_path": str(template_path),
                "source": spec_result["source"],
            },
        )

        return {
            "ok": True,
            "template_path": str(template_path),
            "template": template,
        }

    def list_templates(self, customer: Optional[str] = None) -> Dict[str, Any]:
        index = _load_template_index()
        if customer:
            index = [item for item in index if item.get("customer") == customer]
        return {"ok": True, "count": len(index), "templates": index}

    def get_memory(self, limit: int = 50, customer: Optional[str] = None) -> Dict[str, Any]:
        entries = read_memory(limit=limit, customer=customer)
        return {"ok": True, "count": len(entries), "entries": entries}

    def list_trayio_workflows(self) -> Dict[str, Any]:
        return self.tray_client.list_workflows()

    def get_trayio_workflow(self, workflow_id: str) -> Dict[str, Any]:
        if not workflow_id:
            return {"ok": False, "error": "workflow_id required"}
        return self.tray_client.get_workflow(workflow_id)

    def _resolve_workflow_id(self, workflow_id: Optional[str], workflow_name: Optional[str]) -> Optional[str]:
        if workflow_id:
            return workflow_id
        if workflow_name:
            return self.tray_client.find_workflow_id_by_name(workflow_name)
        return None

    def enable_workflow(self, workflow_id: Optional[str], workflow_name: Optional[str]) -> Dict[str, Any]:
        resolved = self._resolve_workflow_id(workflow_id, workflow_name)
        if not resolved:
            return {"ok": False, "error": "workflow_id or workflow_name required"}
        result = self.tray_client.set_workflow_status(resolved, "enabled")
        if result.get("ok"):
            log_memory(
                "workflow_enabled",
                {"workflow_id": resolved, "workflow_name": workflow_name or ""},
            )
        return result

    def disable_workflow(self, workflow_id: Optional[str], workflow_name: Optional[str]) -> Dict[str, Any]:
        resolved = self._resolve_workflow_id(workflow_id, workflow_name)
        if not resolved:
            return {"ok": False, "error": "workflow_id or workflow_name required"}
        result = self.tray_client.set_workflow_status(resolved, "disabled")
        if result.get("ok"):
            log_memory(
                "workflow_disabled",
                {"workflow_id": resolved, "workflow_name": workflow_name or ""},
            )
        return result

    def trigger_workflow(
        self,
        workflow_id: Optional[str],
        workflow_name: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved = self._resolve_workflow_id(workflow_id, workflow_name)
        if not resolved:
            return {"ok": False, "error": "workflow_id or workflow_name required"}
        result = self.tray_client.trigger_workflow(resolved, payload)
        if result.get("ok"):
            log_memory(
                "workflow_triggered",
                {
                    "workflow_id": resolved,
                    "workflow_name": workflow_name or "",
                    "payload": payload or {},
                },
            )
        return result

    def sync_trayio_knowledge_graph(self) -> Dict[str, Any]:
        try:
            from trayio_crawler import TrayIoCrawler
        except Exception as exc:
            return {"ok": False, "error": f"trayio_crawler not available: {exc}"}

        crawler = TrayIoCrawler()
        graph = crawler.crawl_all()
        log_memory("trayio_knowledge_synced", {"workflow_count": len(graph.get("workflows", []))})
        return {"ok": True, "workflows": len(graph.get("workflows", []))}
