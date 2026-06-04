"""connectors/tray.py — Tray.io connector.

Two modes:
  1. Workflow creation  — uses Tray.io Platform GraphQL API (requires TRAY_MASTER_TOKEN).
                          Given a parsed spec dict, creates + deploys a Tray workflow,
                          returns the workflow_id and its webhook trigger URL.

  2. Document submission — POSTs an X12 payload to the partner's registered webhook URL.
                          This requires only the webhook URL (stored in partner registry)
                          and works without the master token at runtime.

Dry-run: when TRAY_MASTER_TOKEN is absent, create_workflow() generates the Tray.io
import JSON and returns it for manual import. submit() still works as long as
a webhook_url is registered for the partner.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import requests

from .base import ConnectorBase, InboundDocument, SubmitResult

log = logging.getLogger(__name__)

TRAY_GRAPHQL_URL = "https://api.tray.io/graphql"


class TrayConnector(ConnectorBase):
    """Tray.io transport connector."""

    PLATFORM = "tray"

    def __init__(
        self,
        master_token: str | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ):
        self.master_token = master_token or os.getenv("TRAY_MASTER_TOKEN", "")
        self.webhook_url = webhook_url  # per-partner, stored in registry
        self.webhook_secret = webhook_secret

    # ------------------------------------------------------------------
    # ConnectorBase interface
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> SubmitResult:
        """POST X12 payload to the partner's Tray webhook trigger URL."""
        if not self.webhook_url:
            return SubmitResult(
                transaction_id="",
                ok=False,
                error="No webhook_url configured for this partner — did onboarding complete?",
            )

        tx_id = f"TRAY-{doc_type}-{uuid.uuid4().hex[:10]}"
        headers = {"Content-Type": "application/json"}
        if self.webhook_secret:
            headers["X-Tray-Secret"] = self.webhook_secret

        payload = {
            "transaction_id": tx_id,
            "trading_partner": trading_partner,
            "doc_type": doc_type,
            "x12": x12_string,
        }

        try:
            resp = requests.post(self.webhook_url, headers=headers, json=payload, timeout=30)
        except requests.RequestException as exc:
            return SubmitResult(transaction_id=tx_id, ok=False, error=str(exc))

        if resp.status_code >= 400:
            return SubmitResult(
                transaction_id=tx_id,
                ok=False,
                error=f"Tray webhook returned {resp.status_code}: {resp.text[:200]}",
            )

        try:
            raw = resp.json()
        except ValueError:
            raw = {"text": resp.text}

        return SubmitResult(transaction_id=tx_id, ok=True, raw=raw)

    def supports_workflow_creation(self) -> bool:
        return True  # always try — falls back to JSON export if no token

    # ------------------------------------------------------------------
    # Workflow creation via Tray.io Platform API
    # ------------------------------------------------------------------

    def create_workflow(self, partner_name: str, spec: dict, doc_types: list[str]) -> dict[str, Any]:
        """Create a Tray.io workflow for a new trading partner.

        With TRAY_MASTER_TOKEN: creates via GraphQL API, returns live webhook URL.
        Without token: generates import JSON, returns it for manual import.
        """
        if self.master_token:
            return self._create_via_api(partner_name, spec, doc_types)
        else:
            return self._create_json_export(partner_name, spec, doc_types)

    def _create_via_api(self, partner_name: str, spec: dict, doc_types: list[str]) -> dict[str, Any]:
        """Create workflow via Tray.io GraphQL Platform API."""
        # Step 1: Get workspace ID
        workspace_id = self._get_workspace_id()
        if not workspace_id:
            return {"ok": False, "error": "Could not retrieve Tray workspace ID"}

        results = {}
        for doc_type in doc_types:
            wf_name = f"EDI {doc_type} — {partner_name}"
            workflow_id = self._graphql_create_workflow(workspace_id, wf_name)
            if not workflow_id:
                results[doc_type] = {"ok": False, "error": f"Failed to create workflow {wf_name}"}
                continue

            webhook_url = self._graphql_get_webhook_url(workflow_id)
            results[doc_type] = {
                "ok": True,
                "workflow_id": workflow_id,
                "webhook_url": webhook_url,
                "name": wf_name,
            }
            log.info(f"Created Tray workflow [{wf_name}] → {workflow_id}")

        all_ok = all(v.get("ok") for v in results.values())
        return {"ok": all_ok, "workflows": results, "platform": "tray_api"}

    def _create_json_export(self, partner_name: str, spec: dict, doc_types: list[str]) -> dict[str, Any]:
        """No master token — generate Tray.io import JSON from spec.

        The JSON can be imported via Tray.io UI → Import Workflow.
        Once imported, copy the webhook URL from the workflow trigger and
        call PATCH /partners/{id} to register it.
        """
        try:
            # Reuse the existing edi_spec_to_trayio logic from ceo-bot
            # Import path may vary depending on deployment — try gracefully
            from edi_spec_to_trayio import EDISpecToTrayIO  # type: ignore
            exporter = EDISpecToTrayIO()
            exports = {}
            for doc_type in doc_types:
                tray_json = exporter.export(spec, doc_type=doc_type, partner=partner_name)
                exports[doc_type] = tray_json
        except ImportError:
            exports = self._minimal_tray_export(partner_name, spec, doc_types)

        import_instructions = (
            f"1. Go to Tray.io → your project → Import Workflow\n"
            f"2. Upload each JSON file for {partner_name}\n"
            f"3. Open each workflow, copy the Webhook trigger URL\n"
            f"4. Call PATCH /partners/{{id}} with {{\"webhook_urls\": {{\"850\": \"...\"}}}}"
        )

        return {
            "ok": True,
            "platform": "tray_json_export",
            "manual_import_required": True,
            "import_instructions": import_instructions,
            "exports": exports,
        }

    def _minimal_tray_export(self, partner_name: str, spec: dict, doc_types: list[str]) -> dict:
        """Fallback: generate minimal Tray.io workflow JSON without the ceo-bot library."""
        exports = {}
        for doc_type in doc_types:
            exports[doc_type] = {
                "tray_export_version": "1.0.0",
                "name": f"EDI {doc_type} — {partner_name}",
                "description": f"Auto-generated EDI {doc_type} workflow for {partner_name}",
                "steps": [
                    {
                        "id": str(uuid.uuid4()),
                        "title": "Receive EDI Payload",
                        "connector": {"name": "webhook", "version": "1.0"},
                        "operation": "trigger",
                    },
                    {
                        "id": str(uuid.uuid4()),
                        "title": f"Process {doc_type}",
                        "connector": {"name": "script", "version": "3.1"},
                        "operation": "run_script",
                        "note": f"Add transform logic from spec: {json.dumps(spec.get('field_map', {}))[:500]}",
                    },
                ],
                "spec_source": spec.get("partner", partner_name),
                "doc_type": doc_type,
            }
        return exports

    # ------------------------------------------------------------------
    # Tray.io GraphQL helpers
    # ------------------------------------------------------------------

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.master_token}",
            "Content-Type": "application/json",
        }
        body = {"query": query}
        if variables:
            body["variables"] = variables
        try:
            resp = requests.post(TRAY_GRAPHQL_URL, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.error(f"Tray GraphQL error: {exc}")
            return {"errors": [str(exc)]}

    def _get_workspace_id(self) -> str | None:
        result = self._gql("{ viewer { workspaces { edges { node { id name } } } } }")
        try:
            edges = result["data"]["viewer"]["workspaces"]["edges"]
            return edges[0]["node"]["id"] if edges else None
        except (KeyError, IndexError, TypeError):
            log.error(f"Could not extract workspace ID from: {result}")
            return None

    def _graphql_create_workflow(self, workspace_id: str, name: str) -> str | None:
        mutation = """
        mutation CreateWorkflow($workspaceId: ID!, $name: String!) {
            createWorkflow(input: { workspaceId: $workspaceId, name: $name }) {
                workflow { id name }
            }
        }
        """
        result = self._gql(mutation, {"workspaceId": workspace_id, "name": name})
        try:
            return result["data"]["createWorkflow"]["workflow"]["id"]
        except (KeyError, TypeError):
            log.error(f"createWorkflow failed: {result}")
            return None

    def _graphql_get_webhook_url(self, workflow_id: str) -> str:
        query = """
        query GetWorkflow($id: ID!) {
            workflow(id: $id) { triggerUrl }
        }
        """
        result = self._gql(query, {"id": workflow_id})
        try:
            return result["data"]["workflow"]["triggerUrl"] or ""
        except (KeyError, TypeError):
            return ""
