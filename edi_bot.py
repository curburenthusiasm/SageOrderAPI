"""EDI Bot - AI agent for managing EDI workflows and integrations via Tray.io.

Uses Anthropic Claude API with tool-use to interpret natural language requests
and perform operations on Tray.io workflows via Selenium browser automation.
Replaces the legacy ChromeBot with full EDI development platform capabilities.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict

from dotenv import load_dotenv
import anthropic

from edi_tools import (
    list_workflows,
    get_workflow_detail,
    enable_workflow,
    disable_workflow,
    trigger_workflow,
    get_workflow_logs,
    create_workflow,
    check_edi_errors,
    retrigger_edi,
    get_system_status,
    get_edi_knowledge_context,
    close_session,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"
MAX_TURNS = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("edi_bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS = {
    "list_workflows": list_workflows,
    "get_workflow_detail": get_workflow_detail,
    "enable_workflow": enable_workflow,
    "disable_workflow": disable_workflow,
    "trigger_workflow": trigger_workflow,
    "get_workflow_logs": get_workflow_logs,
    "create_workflow": create_workflow,
    "check_edi_errors": check_edi_errors,
    "retrigger_edi": retrigger_edi,
    "get_system_status": get_system_status,
}

# ---------------------------------------------------------------------------
# Tool definitions for Claude API
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_workflows",
        "description": (
            "List all Tray.io workflows. Shows workflow name, status, trigger type, "
            "and whether each workflow is EDI-related. "
            "Uses live Selenium browser session if available, otherwise falls back to cached knowledge graph."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_edi": {
                    "type": "boolean",
                    "description": "If true, only return EDI-related workflows (default: false)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_workflow_detail",
        "description": (
            "Get detailed information for a specific workflow by name. "
            "Returns steps, connectors used, trigger type, and last execution info."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Name of the workflow to inspect (case-insensitive match)",
                },
            },
            "required": ["workflow_name"],
        },
    },
    {
        "name": "enable_workflow",
        "description": "Enable a disabled Tray.io workflow. Requires browser session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Name of the workflow to enable",
                },
            },
            "required": ["workflow_name"],
        },
    },
    {
        "name": "disable_workflow",
        "description": "Disable an active Tray.io workflow. Requires browser session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Name of the workflow to disable",
                },
            },
            "required": ["workflow_name"],
        },
    },
    {
        "name": "trigger_workflow",
        "description": (
            "Manually trigger a Tray.io workflow to run immediately. "
            "Requires browser session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Name of the workflow to trigger",
                },
            },
            "required": ["workflow_name"],
        },
    },
    {
        "name": "get_workflow_logs",
        "description": (
            "Get recent execution logs for a workflow. "
            "Shows execution status (success/failed/running), timestamps, and error messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Name of the workflow to get logs for",
                },
            },
            "required": ["workflow_name"],
        },
    },
    {
        "name": "create_workflow",
        "description": (
            "Create a new Tray.io workflow with a given name and trigger type. "
            "Requires browser session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the new workflow",
                },
                "trigger_type": {
                    "type": "string",
                    "enum": ["manual", "webhook", "schedule"],
                    "description": "Trigger type for the workflow (default: manual)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "check_edi_errors",
        "description": (
            "Check for failed EDI transactions across all EDI-related workflows. "
            "Scans workflow execution logs for failures and returns error details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "retrigger_edi",
        "description": (
            "Retrigger a failed EDI document by re-running its associated workflow. "
            "Use this after investigating and fixing the root cause of an EDI failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "Name of the workflow to retrigger",
                },
            },
            "required": ["workflow_name"],
        },
    },
    {
        "name": "get_system_status",
        "description": (
            "Get status overview of all enterprise systems: "
            "TLW EDI Translator, Cleo Lexicom, Sage 100, ShipExec, ShipStation. "
            "Shows system types, purposes, and integration counts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """Build the system prompt with dynamic knowledge context."""
    knowledge = get_edi_knowledge_context()

    return f"""You are the EDI Bot, a specialized agent for managing EDI workflows and integrations \
at Jeffco Fibres (Webster Industries).

You manage EDI (Electronic Data Interchange) operations through Tray.io workflow automation \
and coordinate across the enterprise system landscape.

ENTERPRISE SYSTEMS:
- **TLW EDI Translator**: Parses/generates EDI documents (X12). Handles 850, 810, 856, 846, 997.
- **Cleo Lexicom**: EDI gateway for AS2/SFTP communications with trading partners.
- **Sage 100**: ERP system (SQL Server on JEF-SQL). Sales orders, invoicing, inventory.
- **ShipExec**: Warehouse Management System. Picking, packing, shipping.
- **ShipStation**: Multi-carrier shipping platform. Label creation, tracking.

EDI DOCUMENT TYPES:
- **850**: Purchase Order (inbound from customers)
- **810**: Invoice (outbound to customers)
- **856**: Advance Ship Notice / ASN (outbound to customers)
- **846**: Inventory Inquiry (inbound/outbound)
- **997**: Functional Acknowledgment (both directions)

DATA FLOW - Order to Cash:
1. Customer sends 850 PO via EDI (AS2/SFTP through Cleo)
2. TLW translates EDI to Sage format
3. Sage 100 creates sales order
4. ShipExec picks and packs order
5. ShipStation creates shipping label
6. Sage 100 invoices the order
7. TLW generates 810 Invoice and 856 ASN
8. Cleo transmits EDI back to customer
9. Customer sends 997 acknowledgment

TRAY.IO WORKFLOW MANAGEMENT:
- You can list, inspect, enable, disable, trigger, and create workflows
- You interact with Tray.io through browser automation (Selenium)
- Always verify workflow status before making changes
- When creating new EDI workflows, follow the established naming conventions

CURRENT KNOWLEDGE:
{knowledge}

IMPORTANT RULES:
- Always list workflows first to understand current state before making changes
- When asked about EDI errors, check workflow logs for recent failures
- When retriggering failed EDI, explain what went wrong first
- For new trading partner setup, describe the full onboarding process
- Be specific about which enterprise system handles each part of the flow
"""


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(user_request: str) -> str:
    """Run the EDI Bot agent for a single user request. Returns final text response."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = _build_system_prompt()

    messages = [{"role": "user", "content": user_request}]

    try:
        for _ in range(MAX_TURNS):
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            # Process response content blocks
            assistant_text = ""
            tool_uses = []

            for block in response.content:
                if block.type == "text":
                    assistant_text += block.text
                elif block.type == "tool_use":
                    tool_uses.append(block)

            # Add assistant message to conversation
            messages.append({"role": "assistant", "content": response.content})

            # If no tool calls, return the text response
            if not tool_uses:
                if assistant_text:
                    print(f"\nEDI Bot: {assistant_text}")
                return assistant_text

            # Execute tool calls
            tool_results = []
            for tool_use in tool_uses:
                fn_name = tool_use.name
                args = tool_use.input or {}

                print(f"\n  [Tool] {fn_name}({json.dumps(args, default=str)})")

                fn = TOOL_FUNCTIONS.get(fn_name)
                if not fn:
                    result = {"ok": False, "error": f"Unknown tool: {fn_name}"}
                else:
                    try:
                        result = fn(**args)
                    except TypeError as e:
                        result = {"ok": False, "error": f"Invalid args for {fn_name}: {e}"}
                    except Exception as e:
                        logger.exception(f"Error in {fn_name}")
                        result = {"ok": False, "error": str(e)}

                # Truncate large results for display
                result_str = json.dumps(result, indent=2, default=str)
                if len(result_str) > 500:
                    print(f"  [Result] ({len(result_str)} chars) ...")
                else:
                    print(f"  [Result] {result_str}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result, default=str),
                })

            messages.append({"role": "user", "content": tool_results})

            # Check stop reason
            if response.stop_reason == "end_turn":
                break

        return assistant_text or "(max turns reached)"

    finally:
        # Close the browser session when the agent run ends
        close_session()


# ---------------------------------------------------------------------------
# Standalone function for CEO.py delegation
# ---------------------------------------------------------------------------

def delegate_edi_task(task: str) -> Dict[str, Any]:
    """Entry point for CEO.py to delegate EDI tasks.

    Args:
        task: Natural language description of the EDI task

    Returns:
        Dict with ok, message, and details
    """
    try:
        response = run_agent(task)
        return {"ok": True, "message": response}
    except Exception as e:
        logger.exception("EDI Bot error")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

def main():
    """Run the EDI Bot interactively."""
    print("=" * 70)
    print("EDI Bot - EDI Workflow & Integration Manager for Jeffco Fibres")
    print(f"Model: {MODEL}")
    print("=" * 70)
    print("\nManages Tray.io workflows and EDI operations via browser automation.")
    print(f"Tray.io URL: {os.getenv('TRAY_IO_URL', 'not configured')}")
    print(f"Selenium: {'available' if __import__('edi_trayio').SELENIUM_AVAILABLE else 'NOT available'}")
    print("\nExamples:")
    print('  "List all workflows"')
    print('  "Show me the EDI workflows"')
    print('  "Check for EDI errors"')
    print('  "What are the details of the 850 order processing workflow?"')
    print('  "Trigger the invoice generation workflow"')
    print('  "Disable the test workflow"')
    print('  "Create a new webhook workflow called 856 ASN Processing"')
    print('  "What systems handle the order-to-cash flow?"')
    print("\nType 'exit' or 'quit' to stop.")
    print("=" * 70)

    if not ANTHROPIC_API_KEY:
        print("\nERROR: ANTHROPIC_API_KEY not set in .env file.")
        print("Add: ANTHROPIC_API_KEY=sk-ant-...")
        return

    while True:
        try:
            user_input = input("\nEDI Bot > ").strip()
            if user_input.lower() in {"exit", "quit"}:
                break
            if not user_input:
                continue
            run_agent(user_input)
        except KeyboardInterrupt:
            print("\n\nShutting down EDI Bot...")
            break

    close_session()
    print("\nEDI Bot terminated. Goodbye!")


if __name__ == "__main__":
    main()
