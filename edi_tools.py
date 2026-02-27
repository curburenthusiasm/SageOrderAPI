"""Tool implementations for EDI Bot.

Wraps edi_trayio.py Selenium session and knowledge graph queries
into tool functions callable by the Claude API agent.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict, List, Optional

from edi_trayio import TraySession, SELENIUM_AVAILABLE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level session (lazy-initialized)
# ---------------------------------------------------------------------------

_session: Optional[TraySession] = None


def _get_session() -> TraySession:
    """Get or create the shared Tray.io session."""
    global _session
    if _session is None:
        _session = TraySession()
    return _session


def close_session() -> None:
    """Close the shared session. Called when the agent run ends."""
    global _session
    if _session is not None:
        _session.close()
        _session = None


# ---------------------------------------------------------------------------
# Knowledge graph helpers
# ---------------------------------------------------------------------------

def _load_knowledge(filename: str) -> Dict[str, Any]:
    """Load a knowledge graph JSON file."""
    paths = [
        os.path.join("knowledge_base", "trayio", filename),
        os.path.join("knowledge_base", "enterprise", filename),
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load {path}: {e}")
    return {}


def _load_trayio_knowledge() -> Dict[str, Any]:
    """Load Tray.io knowledge graph."""
    return _load_knowledge("trayio_knowledge_latest.json")


def _load_enterprise_knowledge() -> Dict[str, Any]:
    """Load enterprise systems knowledge graph."""
    return _load_knowledge("enterprise_knowledge_latest.json")


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def list_workflows(filter_edi: bool = False) -> Dict[str, Any]:
    """List all Tray.io workflows.

    Tries Selenium first; falls back to knowledge graph if browser unavailable.
    """
    if SELENIUM_AVAILABLE:
        try:
            session = _get_session()
            result = session.list_workflows()
            if result.get("ok"):
                workflows = result.get("workflows", [])
                if filter_edi:
                    workflows = [w for w in workflows if w.get("edi_related")]
                    result["workflows"] = workflows
                    result["count"] = len(workflows)
                return result
        except Exception as e:
            logger.warning(f"Selenium list_workflows failed: {e}, falling back to knowledge graph")

    # Fallback: use knowledge graph
    kg = _load_trayio_knowledge()
    workflows = kg.get("workflows", [])
    if filter_edi:
        workflows = [w for w in workflows if w.get("edi_related")]

    return {
        "ok": True,
        "workflows": workflows,
        "count": len(workflows),
        "source": "knowledge_graph",
        "note": "Data from cached knowledge graph, not live Tray.io" if not SELENIUM_AVAILABLE
                else "Fell back to knowledge graph due to browser error",
    }


def get_workflow_detail(workflow_name: str) -> Dict[str, Any]:
    """Get detailed info for a specific workflow."""
    if SELENIUM_AVAILABLE:
        try:
            session = _get_session()
            return session.get_workflow_detail(workflow_name)
        except Exception as e:
            logger.warning(f"Selenium get_workflow_detail failed: {e}")

    # Fallback: search knowledge graph
    kg = _load_trayio_knowledge()
    for wf in kg.get("workflows", []):
        if workflow_name.lower() in wf.get("name", "").lower():
            return {"ok": True, "detail": wf, "source": "knowledge_graph"}

    return {"ok": False, "error": f"Workflow '{workflow_name}' not found"}


def enable_workflow(workflow_name: str) -> Dict[str, Any]:
    """Enable a disabled workflow."""
    if not SELENIUM_AVAILABLE:
        return {"ok": False, "error": "Selenium not available - cannot enable workflows without browser"}

    session = _get_session()
    return session.enable_workflow(workflow_name)


def disable_workflow(workflow_name: str) -> Dict[str, Any]:
    """Disable an active workflow."""
    if not SELENIUM_AVAILABLE:
        return {"ok": False, "error": "Selenium not available - cannot disable workflows without browser"}

    session = _get_session()
    return session.disable_workflow(workflow_name)


def trigger_workflow(workflow_name: str) -> Dict[str, Any]:
    """Manually trigger a workflow run."""
    if not SELENIUM_AVAILABLE:
        return {"ok": False, "error": "Selenium not available - cannot trigger workflows without browser"}

    session = _get_session()
    return session.trigger_workflow(workflow_name)


def get_workflow_logs(workflow_name: str) -> Dict[str, Any]:
    """Get recent execution logs for a workflow."""
    if not SELENIUM_AVAILABLE:
        return {"ok": False, "error": "Selenium not available - cannot get logs without browser"}

    session = _get_session()
    return session.get_workflow_logs(workflow_name)


def create_workflow(name: str, trigger_type: str = "manual") -> Dict[str, Any]:
    """Create a new workflow in Tray.io."""
    if not SELENIUM_AVAILABLE:
        return {"ok": False, "error": "Selenium not available - cannot create workflows without browser"}

    session = _get_session()
    return session.create_workflow(name, trigger_type)


def check_edi_errors() -> Dict[str, Any]:
    """Check for failed EDI transactions across all workflows.

    Scans EDI-related workflows for recent failures.
    """
    # First get EDI workflows
    wf_result = list_workflows(filter_edi=True)
    if not wf_result.get("ok"):
        return wf_result

    workflows = wf_result.get("workflows", [])
    if not workflows:
        return {"ok": True, "errors": [], "message": "No EDI workflows found"}

    errors = []

    if SELENIUM_AVAILABLE:
        session = _get_session()
        for wf in workflows[:10]:  # Check up to 10 EDI workflows
            wf_name = wf.get("name", "")
            if not wf_name:
                continue
            try:
                logs_result = session.get_workflow_logs(wf_name)
                if logs_result.get("ok"):
                    for log in logs_result.get("logs", []):
                        if log.get("status") == "failed":
                            errors.append({
                                "workflow": wf_name,
                                "log": log.get("text", ""),
                                "status": "failed",
                            })
            except Exception as e:
                logger.warning(f"Could not check logs for {wf_name}: {e}")

    return {
        "ok": True,
        "errors": errors,
        "error_count": len(errors),
        "workflows_checked": len(workflows),
    }


def retrigger_edi(workflow_name: str) -> Dict[str, Any]:
    """Retrigger a failed EDI document by re-running its workflow."""
    return trigger_workflow(workflow_name)


def get_system_status() -> Dict[str, Any]:
    """Get status overview of all enterprise systems from knowledge graph."""
    enterprise_kg = _load_enterprise_knowledge()
    trayio_kg = _load_trayio_knowledge()

    systems = []
    for sys_key, sys_data in enterprise_kg.get("systems", {}).items():
        systems.append({
            "name": sys_data.get("name", sys_key),
            "type": sys_data.get("type", "unknown"),
            "purpose": sys_data.get("purpose", ""),
            "interfaces": len(sys_data.get("interfaces", [])),
        })

    tray_workflows = trayio_kg.get("workflows", [])
    edi_count = sum(1 for w in tray_workflows if w.get("edi_related"))

    return {
        "ok": True,
        "enterprise_systems": systems,
        "trayio_workflows": len(tray_workflows),
        "edi_workflows": edi_count,
        "integrations": len(enterprise_kg.get("integrations", [])),
        "data_flows": len(enterprise_kg.get("data_flows", [])),
    }


def get_edi_knowledge_context() -> str:
    """Build a knowledge context string for the system prompt."""
    enterprise_kg = _load_enterprise_knowledge()
    trayio_kg = _load_trayio_knowledge()

    parts = []

    # Enterprise systems
    for sys_key, sys_data in enterprise_kg.get("systems", {}).items():
        parts.append(f"- {sys_data.get('name', sys_key)}: {sys_data.get('purpose', '')}")

    # Integrations
    for integ in enterprise_kg.get("integrations", []):
        parts.append(f"- Integration: {integ.get('name', '')} - {integ.get('flow', '')}")

    # Data flows
    for flow in enterprise_kg.get("data_flows", []):
        steps = flow.get("steps", [])
        parts.append(f"- Data flow: {flow.get('name', '')} ({len(steps)} steps)")

    # Tray.io workflows
    workflows = trayio_kg.get("workflows", [])
    if workflows:
        parts.append(f"\nTray.io: {len(workflows)} workflows")
        for wf in workflows[:15]:
            edi_tag = " [EDI]" if wf.get("edi_related") else ""
            parts.append(f"  - {wf.get('name', 'Unknown')}{edi_tag}")

    # EDI patterns
    edi_patterns = trayio_kg.get("edi_patterns", {})
    if edi_patterns:
        parts.append("\nEDI Patterns:")
        for pattern_name, pattern_wfs in edi_patterns.items():
            if pattern_wfs:
                parts.append(f"  {pattern_name}: {', '.join(pattern_wfs[:3])}")

    return "\n".join(parts)
