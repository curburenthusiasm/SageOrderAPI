"""Workflow builder orchestrator for constructing EDI workflows in Tray.io.

Uses a TraySession (Selenium) to create a workflow from a template definition,
adding and configuring each step via the Tray.io visual builder UI.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from edi_trayio import TraySession
from edi_models import WorkflowBuildResult, WorkflowTemplate
from edi_workflow_templates import get_template, list_templates, ALL_TEMPLATES

logger = logging.getLogger(__name__)


class WorkflowBuilder:
    """Orchestrates building a complete workflow in Tray.io from a template."""

    def __init__(self, session: TraySession):
        self.session = session

    def build_workflow(
        self,
        template_key: str,
        trading_partner: Optional[str] = None,
        workflow_name_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a complete workflow from a template.

        Args:
            template_key: Template key (e.g. '850', '810', 'error_monitor').
            trading_partner: Optional trading partner name to customize the workflow.
            workflow_name_override: Optional custom name (defaults to template name).

        Returns:
            Dict with build result including success status and step details.
        """
        template = get_template(template_key)
        if not template:
            available = ", ".join(ALL_TEMPLATES.keys())
            return {
                "ok": False,
                "error": f"Unknown template '{template_key}'. Available: {available}",
            }

        # Determine workflow name
        wf_name = workflow_name_override or template.name
        if trading_partner:
            wf_name = f"{wf_name} - {trading_partner}"

        logger.info(f"Building workflow: {wf_name} from template [{template_key}]")

        # Track build progress
        result = WorkflowBuildResult(
            template_name=template.name,
            workflow_name=wf_name,
            success=False,
            steps_completed=0,
            steps_total=len(template.steps),
        )

        # Step 1: Create the empty workflow
        create_result = self.session.create_workflow(
            name=wf_name,
            trigger_type=template.trigger_type,
        )
        if not create_result.get("ok"):
            result.errors.append(f"Failed to create workflow: {create_result.get('error', 'unknown')}")
            return {"ok": False, "result": result.model_dump(), **result.model_dump()}

        result.url = create_result.get("url", "")
        logger.info(f"Created workflow: {wf_name} at {result.url}")
        time.sleep(3)

        # Step 2: Add each step from the template
        for i, step in enumerate(template.steps):
            step_label = f"[{i+1}/{len(template.steps)}] {step.name}"
            logger.info(f"Adding step {step_label}")

            # Substitute trading_partner into config values if provided
            step_config = dict(step.config)
            if trading_partner:
                step_config = {
                    k: v.replace("{trading_partner}", trading_partner)
                    for k, v in step_config.items()
                }

            # Add the step
            add_result = self.session.add_workflow_step(
                connector_name=step.connector,
                operation=step.operation,
                step_name=step.name,
            )
            if not add_result.get("ok"):
                error_msg = f"Step {step_label}: {add_result.get('error', 'unknown')}"
                result.errors.append(error_msg)
                logger.warning(f"Failed to add step: {error_msg}")
                # Continue to next step — partial builds are still useful
                continue

            time.sleep(2)

            # Configure the step (if config is provided)
            if step_config:
                config_result = self.session.configure_step(
                    step_name=step.name,
                    config=step_config,
                )
                if not config_result.get("ok"):
                    error_msg = f"Config {step_label}: {config_result.get('error', 'unknown')}"
                    result.errors.append(error_msg)
                    logger.warning(f"Failed to configure step: {error_msg}")
                else:
                    fields_set = config_result.get("fields_set", 0)
                    fields_total = config_result.get("fields_total", 0)
                    if fields_set < fields_total:
                        result.errors.append(
                            f"Config {step_label}: only {fields_set}/{fields_total} fields set"
                        )

            result.steps_completed += 1
            time.sleep(1)

        # Determine overall success
        result.success = result.steps_completed == result.steps_total and not result.errors

        # Get final editor state for verification
        editor_state = self.session.get_workflow_editor_state()
        actual_steps = editor_state.get("step_count", 0) if editor_state.get("ok") else "unknown"

        logger.info(
            f"Build complete: {result.steps_completed}/{result.steps_total} steps, "
            f"{len(result.errors)} errors, {actual_steps} steps on canvas"
        )

        return {
            "ok": result.success or result.steps_completed > 0,
            "result": result.model_dump(),
            "summary": result.summary(),
            "editor_steps": actual_steps,
        }

    def preview_template(self, template_key: str) -> Dict[str, Any]:
        """Preview what a template build would create without actually building.

        Args:
            template_key: Template key to preview.

        Returns:
            Dict with template details and step breakdown.
        """
        template = get_template(template_key)
        if not template:
            available = ", ".join(ALL_TEMPLATES.keys())
            return {
                "ok": False,
                "error": f"Unknown template '{template_key}'. Available: {available}",
            }

        return {
            "ok": True,
            "template": {
                "name": template.name,
                "key": template.template_key,
                "trigger_type": template.trigger_type,
                "trigger_config": template.trigger_config,
                "description": template.description,
                "edi_doc_type": template.edi_doc_type,
                "direction": template.direction,
                "step_count": len(template.steps),
                "steps": [
                    {
                        "index": i + 1,
                        "name": step.name,
                        "connector": step.connector,
                        "operation": step.operation,
                        "config_keys": list(step.config.keys()),
                    }
                    for i, step in enumerate(template.steps)
                ],
            },
            "summary": template.summary(),
        }

    @staticmethod
    def list_available_templates() -> Dict[str, Any]:
        """List all available workflow templates.

        Returns:
            Dict with template list and summary info.
        """
        templates = list_templates()
        return {
            "ok": True,
            "templates": [
                {
                    "key": t.template_key,
                    "name": t.name,
                    "trigger_type": t.trigger_type,
                    "step_count": len(t.steps),
                    "edi_doc_type": t.edi_doc_type,
                    "direction": t.direction,
                    "description": t.description,
                }
                for t in templates
            ],
            "count": len(templates),
        }
