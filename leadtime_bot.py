"""LeadTime Bot - AI agent for managing customer lead time rules in SQL Server Agent jobs.

Uses Anthropic Claude API with tool-use to interpret natural language requests
and perform CRUD operations on lead time CASE statements in two SQL files.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict

from dotenv import load_dotenv
import anthropic

from leadtime_tools import (
    find_sql_files,
    list_customers,
    get_customer_rules,
    update_lead_time,
    add_customer_rule,
    remove_customer_rule,
    update_late_threshold,
    apply_changes,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"
MAX_TURNS = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("leadtime_bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pending changes buffer (for preview -> apply workflow)
# ---------------------------------------------------------------------------

_pending_changes: Dict[str, Any] = {}


def _wrap_apply(confirmed: bool) -> Dict[str, Any]:
    """Apply or discard pending changes."""
    global _pending_changes
    if not _pending_changes:
        return {"ok": False, "error": "No pending changes to apply"}
    if not confirmed:
        _pending_changes = {}
        return {"ok": True, "message": "Changes discarded"}
    result = apply_changes(_pending_changes)
    _pending_changes = {}
    return result


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS = {
    "find_sql_files": find_sql_files,
    "list_customers": list_customers,
    "get_customer_rules": get_customer_rules,
    "update_lead_time": update_lead_time,
    "add_customer_rule": add_customer_rule,
    "remove_customer_rule": remove_customer_rule,
    "update_late_threshold": update_late_threshold,
    "apply_changes": _wrap_apply,
}


# ---------------------------------------------------------------------------
# Tool definitions for Claude API
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "find_sql_files",
        "description": (
            "Check connectivity to the SQL Server Agent jobs that contain lead time rules. "
            "Returns the job names for shipping and production. "
            "Call this first to verify access before other operations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_customers",
        "description": (
            "List all customers and their current lead time settings from the SQL Agent jobs. "
            "Shows customer names, ship date method, business days, and late thresholds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_target": {
                    "type": "string",
                    "enum": ["both", "shipping", "production"],
                    "description": "Which job(s) to read from (default: both)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_customer_rules",
        "description": (
            "Get detailed lead time rules for a specific customer. "
            "Shows ship date calculation method, business days, product lines, "
            "item-code-specific overrides, and late threshold."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer name to look up (case-insensitive)",
                },
                "file_target": {
                    "type": "string",
                    "enum": ["both", "shipping", "production"],
                    "description": "Which job(s) to read from (default: both)",
                },
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "update_lead_time",
        "description": (
            "Update the lead time (business days) for a customer's estimated ship date. "
            "By default also updates the late threshold to new_days + 1. "
            "Returns a preview diff - call apply_changes to write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer name to update",
                },
                "new_days": {
                    "type": "integer",
                    "description": "New number of business days for estimated ship date",
                },
                "file_target": {
                    "type": "string",
                    "enum": ["both", "shipping", "production"],
                    "description": "Which job(s) to update (default: both)",
                },
                "also_update_late": {
                    "type": "boolean",
                    "description": "Also update late threshold to new_days + 1 (default: true)",
                },
            },
            "required": ["customer_name", "new_days"],
        },
    },
    {
        "name": "add_customer_rule",
        "description": (
            "Add a new customer lead time rule to the SQL Agent jobs. "
            "Inserts WHEN clauses into both the Estimated_Ship_Date and LeadTime CASE blocks. "
            "Returns a preview diff - call apply_changes to write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "New customer name exactly as it should appear in SQL",
                },
                "ship_date_days": {
                    "type": "integer",
                    "description": "Business days for estimated ship date",
                },
                "product_lines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Product line codes (default: ['0068','0069','0010','0091','0048'])",
                },
                "late_threshold_days": {
                    "type": "integer",
                    "description": "Business days before Late status (default: ship_date_days + 1)",
                },
                "ship_date_method": {
                    "type": "string",
                    "enum": ["bus_days_created", "bus_days_ordered", "ship_expire_date", "calendar_days_ord_date"],
                    "description": "Ship date calculation method (default: bus_days_created)",
                },
                "file_target": {
                    "type": "string",
                    "enum": ["both", "shipping", "production"],
                    "description": "Which job(s) to add to (default: both)",
                },
            },
            "required": ["customer_name", "ship_date_days"],
        },
    },
    {
        "name": "remove_customer_rule",
        "description": (
            "Remove all lead time rules for a customer from the SQL Agent jobs. "
            "Removes WHEN clauses from both the Estimated_Ship_Date and LeadTime CASE blocks. "
            "Returns a preview diff - call apply_changes to write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer name to remove",
                },
                "file_target": {
                    "type": "string",
                    "enum": ["both", "shipping", "production"],
                    "description": "Which job(s) to remove from (default: both)",
                },
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "update_late_threshold",
        "description": (
            "Update only the late threshold for a customer without changing the ship date days. "
            "Returns a preview diff - call apply_changes to write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer name to update",
                },
                "new_threshold": {
                    "type": "integer",
                    "description": "New business day threshold for Late status",
                },
                "file_target": {
                    "type": "string",
                    "enum": ["both", "shipping", "production"],
                    "description": "Which job(s) to update (default: both)",
                },
            },
            "required": ["customer_name", "new_threshold"],
        },
    },
    {
        "name": "apply_changes",
        "description": (
            "Apply or discard pending changes from a previous update/add/remove operation. "
            "Changes are written directly to the SQL Server Agent job steps with automatic backups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmed": {
                    "type": "boolean",
                    "description": "True to write changes, False to discard",
                },
            },
            "required": ["confirmed"],
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the LeadTime Bot, a specialized agent for managing customer lead time rules \
at Jeffco Fibres (Webster Industries).

You manage two SQL Server Agent jobs on JEF-SQL that send daily open order reports:
1. **Shipping job** (Open Order Shipping Report) - sent to the shipping team
2. **Production job** (Open Order Production Scheduler) - sent to the production scheduling team

You read and write job step commands directly via the SQL Server msdb database (no file share needed).

Each job step contains two massive CASE statements:
- **Estimated_Ship_Date**: Calculates when an order should ship based on customer-specific rules
- **LeadTime**: Determines if an order is "Late", "OnTime", or "Ship date assigned by customer"

IMPORTANT RULES:
- The two jobs may have DIFFERENT rules for the same customer. Always check both.
- When updating lead times, the late threshold should typically be set to lead_time_days + 1 \
(e.g., 4 day lead time -> 5 day late threshold).
- Always show the user a preview diff before applying changes.
- Changes are backed up locally before writing to SQL Server.
- When asked to update a customer, update BOTH the Estimated_Ship_Date and LeadTime CASE blocks.

WORKFLOW:
1. First call find_sql_files to verify SQL Server connectivity
2. Use list_customers or get_customer_rules to show current state
3. For changes, the tool returns a preview diff with status="preview"
4. Show the diff to the user and ask for confirmation
5. Call apply_changes with confirmed=true to write, or confirmed=false to discard

Ship date methods:
- bus_days_created: dbo.BusDaysDateAdd(DATECREATED, N) - most common
- bus_days_ordered: dbo.BusDaysDateAdd(ORDERDATE, N) - rare (NATURES SLEEP)
- ship_expire_date: SHIPEXPIREDATE - customer-assigned date
- calendar_days_ord_date: DATEADD(DAY, N, UDF_ORD_DATE) - calendar days (BOB'S DISCOUNT)

Standard product lines: "0068", "0069", "0010", "0091", "0048"
Some customers also use: "0065", "0102"
"""


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(user_request: str) -> str:
    """Run the LeadTime Bot agent for a single user request. Returns final text response."""
    global _pending_changes

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages = [{"role": "user", "content": user_request}]

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
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
                print(f"\nLeadTime Bot: {assistant_text}")
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
                    # Store pending changes for the apply workflow
                    if isinstance(result, dict) and result.get("status") == "preview":
                        _pending_changes = result.get("changes", {})
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


# ---------------------------------------------------------------------------
# Standalone function for CEO.py delegation
# ---------------------------------------------------------------------------

def delegate_leadtime_task(task: str) -> Dict[str, Any]:
    """Entry point for CEO.py to delegate lead time tasks.

    Args:
        task: Natural language description of the lead time task

    Returns:
        Dict with ok, message, and details
    """
    try:
        response = run_agent(task)
        return {"ok": True, "message": response}
    except Exception as e:
        logger.exception("LeadTime Bot error")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

def main():
    """Run the LeadTime Bot interactively."""
    print("=" * 70)
    print("LeadTime Bot - Customer Lead Time Manager for Jeffco Fibres")
    print(f"Model: {MODEL}")
    print("=" * 70)
    print("\nManages lead time rules directly in SQL Server Agent jobs on JEF-SQL.")
    print("\nExamples:")
    print('  "List all customers and their lead times"')
    print('  "What are the rules for Mattress Firm?"')
    print('  "Change Mattress Firm lead time to 4 business days"')
    print('  "Add new customer ACME Corp with 5 day lead time"')
    print('  "Remove TEMP customer from both files"')
    print("\nType 'exit' or 'quit' to stop.")
    print("=" * 70)

    if not ANTHROPIC_API_KEY:
        print("\nERROR: ANTHROPIC_API_KEY not set in .env file.")
        print("Add: ANTHROPIC_API_KEY=sk-ant-...")
        return

    while True:
        try:
            user_input = input("\nLeadTime Bot > ").strip()
            if user_input.lower() in {"exit", "quit"}:
                break
            if not user_input:
                continue
            run_agent(user_input)
        except KeyboardInterrupt:
            print("\n\nShutting down LeadTime Bot...")
            break

    print("\nLeadTime Bot terminated. Goodbye!")


if __name__ == "__main__":
    main()
