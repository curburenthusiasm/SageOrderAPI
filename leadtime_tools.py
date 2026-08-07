"""Tool implementations for the LeadTime Bot - CRUD operations on SQL lead time rules.

Reads/writes SQL Server Agent job step commands directly via pyodbc (no file share needed).
"""

from __future__ import annotations

import os
import re
import difflib
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

import pyodbc

from leadtime_models import LeadTimeRule, SqlFileRules
from leadtime_parser import parse_sql_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL Server Agent job configuration
# ---------------------------------------------------------------------------

SHIPPING_JOB_NAME = "Open Order Shipping Report"
PRODUCTION_JOB_NAME = "Open Order Production Scheduler"

JOB_MAP = {
    "shipping": SHIPPING_JOB_NAME,
    "production": PRODUCTION_JOB_NAME,
}

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def _get_db_connection() -> pyodbc.Connection:
    """Connect to SQL Server msdb database."""
    host = os.getenv("DB_HOST", "JEF-SQL")
    user = os.getenv("DB_USER", "MAS_REPORTS")
    pwd = os.getenv("DB_PASSWORD", "")
    conn_str = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={host};"
        f"DATABASE=msdb;"
        f"UID={user};"
        f"PWD={pwd};"
        f"Encrypt=yes;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)


def _read_job_step(job_name: str) -> Optional[str]:
    """Read the command text from a SQL Server Agent job step.

    Returns the command string or None if not found.
    """
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT js.command
            FROM msdb.dbo.sysjobs j
            JOIN msdb.dbo.sysjobsteps js ON j.job_id = js.job_id
            WHERE j.name = ?
            AND js.step_id = 1
        """, job_name)
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _write_job_step(job_name: str, new_command: str) -> None:
    """Update the command text of a SQL Server Agent job step."""
    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            EXEC msdb.dbo.sp_update_jobstep
                @job_name = ?,
                @step_id = 1,
                @command = ?
        """, job_name, new_command)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Job discovery
# ---------------------------------------------------------------------------

def find_sql_files(**kwargs) -> Dict[str, Any]:
    """Check that the SQL Server Agent jobs are accessible.

    Returns dict with job names and connectivity status.
    """
    result: Dict[str, Any] = {}
    errors = []

    for file_type, job_name in JOB_MAP.items():
        try:
            content = _read_job_step(job_name)
            if content:
                result[f"{file_type}_file"] = job_name
            else:
                result[f"{file_type}_file"] = None
                errors.append(f"{file_type}: job '{job_name}' not found")
        except Exception as e:
            result[f"{file_type}_file"] = None
            errors.append(f"{file_type}: {e}")

    if errors:
        result["errors"] = errors

    return result


def _get_job_content(file_target: str) -> List[Dict[str, str]]:
    """Resolve file_target to a list of {job_name, file_type, content} dicts."""
    targets = []
    if file_target in ("both", "shipping"):
        targets.append(("shipping", SHIPPING_JOB_NAME))
    if file_target in ("both", "production"):
        targets.append(("production", PRODUCTION_JOB_NAME))

    results = []
    for file_type, job_name in targets:
        content = _read_job_step(job_name)
        if content:
            results.append({
                "job_name": job_name,
                "file_type": file_type,
                "content": content,
            })
    return results


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_customers(file_target: str = "both", **kwargs) -> Dict[str, Any]:
    """List all customers and their lead times from the SQL Server Agent jobs.

    Args:
        file_target: 'shipping', 'production', or 'both'
    """
    sources = _get_job_content(file_target)
    if not sources:
        return {"ok": False, "error": f"No SQL Agent jobs accessible for target={file_target}"}

    results = {}
    for src in sources:
        try:
            parsed = parse_sql_content(src["content"], source=src["job_name"], file_type=src["file_type"])
            customers = {}
            for rule in parsed.rules:
                name = rule.customer_name
                if name not in customers:
                    customers[name] = []
                customers[name].append({
                    "method": rule.ship_date_method,
                    "days": rule.ship_date_days,
                    "product_lines": rule.product_lines,
                    "has_item_codes": rule.item_codes is not None,
                    "late_threshold": rule.late_threshold_days,
                })
            results[src["file_type"]] = {
                "job_name": src["job_name"],
                "customer_count": len(customers),
                "customers": customers,
            }
        except Exception as e:
            results[src["file_type"]] = {"job_name": src["job_name"], "error": str(e)}

    return {"ok": True, "results": results}


def get_customer_rules(
    customer_name: str,
    file_target: str = "both",
    **kwargs,
) -> Dict[str, Any]:
    """Get detailed rules for a specific customer.

    Args:
        customer_name: Customer name to search for (case-insensitive)
        file_target: 'shipping', 'production', or 'both'
    """
    sources = _get_job_content(file_target)
    if not sources:
        return {"ok": False, "error": "No SQL Agent jobs accessible"}

    results = {}
    for src in sources:
        try:
            parsed = parse_sql_content(src["content"], source=src["job_name"], file_type=src["file_type"])
            matching = parsed.find_rules(customer_name)
            if matching:
                results[src["file_type"]] = {
                    "job_name": src["job_name"],
                    "rules": [r.model_dump() for r in matching],
                    "summaries": [r.summary() for r in matching],
                }
            else:
                results[src["file_type"]] = {
                    "job_name": src["job_name"],
                    "rules": [],
                    "message": f"No rules found for '{customer_name}'",
                }
        except Exception as e:
            results[src["file_type"]] = {"error": str(e)}

    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# SQL generation helpers
# ---------------------------------------------------------------------------

def _generate_ship_date_when(rule: LeadTimeRule) -> str:
    """Generate a WHEN clause for the Estimated_Ship_Date CASE block."""
    cond_parts = []

    if rule.customer_name != "__PRODUCT_LINE_ONLY__":
        escaped = rule.customer_name.replace("'", "''")
        cond_parts.append(f'CUSTOMERNAME = "{escaped}"')

    if rule.product_lines:
        pl_list = ", ".join(f'"{pl}"' for pl in rule.product_lines)
        if len(rule.product_lines) == 1:
            cond_parts.append(f'PRODUCTLINE = "{rule.product_lines[0]}"')
        else:
            cond_parts.append(f"PRODUCTLINE in ({pl_list})")

    if rule.item_codes:
        ic_list = ",\n".join(f'"{ic}"' for ic in rule.item_codes)
        cond_parts.append(f"ITEMCODE in ({ic_list})")

    if rule.customer_po_pattern:
        cond_parts.append(f'CUSTOMERPONO like "{rule.customer_po_pattern}"')

    condition = " and ".join(cond_parts)

    if rule.ship_date_method == "bus_days_created":
        expr = f"(select dbo.BusDaysDateAdd(DATECREATED, ({rule.ship_date_days})) as Estimated_Ship_Date)"
    elif rule.ship_date_method == "bus_days_ordered":
        expr = f"(select dbo.BusDaysDateAdd(ORDERDATE, ({rule.ship_date_days})) as Estimated_Ship_Date)"
    elif rule.ship_date_method == "ship_expire_date":
        expr = "(select SHIPEXPIREDATE as Estimated_Ship_Date)"
    elif rule.ship_date_method == "calendar_days_ord_date":
        expr = f"(SELECT DATEADD(DAY, {rule.ship_date_days}, UDF_ORD_DATE))"
    else:
        expr = '""'

    return f"\t\t\twhen {condition}\n\t\t\tthen {expr}"


def _generate_late_when(rule: LeadTimeRule) -> str:
    """Generate a WHEN clause for the LeadTime CASE block."""
    cond_parts = []

    date_col = rule.late_date_basis
    if rule.late_threshold_days is not None:
        cond_parts.append(
            f"DATEDIFF(day,{date_col}, GETDATE())  - \n"
            f"    DATEDIFF(week, {date_col}, GetDate()) * 2 >= {rule.late_threshold_days}"
        )

    if rule.customer_name != "__PRODUCT_LINE_ONLY__":
        escaped = rule.customer_name.replace("'", "''")
        cond_parts.append(f'CUSTOMERNAME = "{escaped}"')

    if rule.product_lines:
        pl_list = ", ".join(f'"{pl}"' for pl in rule.product_lines)
        if len(rule.product_lines) == 1:
            cond_parts.append(f'PRODUCTLINE = "{rule.product_lines[0]}"')
        else:
            cond_parts.append(f"PRODUCTLINE in ({pl_list})")

    if rule.item_codes:
        ic_list = ",\n".join(f'"{ic}"' for ic in rule.item_codes)
        cond_parts.append(f"ITEMCODE in ({ic_list})")

    condition = " and ".join(cond_parts)
    status = rule.late_status

    return f'when {condition}\n\t\t\tthen "{status}"'


# ---------------------------------------------------------------------------
# Content modification helpers
# ---------------------------------------------------------------------------

def _update_days_in_content(
    content: str,
    customer_name: str,
    old_days: int,
    new_days: int,
    product_lines: Optional[List[str]] = None,
    item_codes: Optional[List[str]] = None,
) -> str:
    """Surgically update the days value for a customer in SQL content."""
    escaped = customer_name.replace("'", "''")
    customer_pattern = re.escape(f'CUSTOMERNAME = "{escaped}"')
    pattern = (
        rf'(when\s+.*?{customer_pattern}.*?BusDaysDateAdd\s*\(\s*\w+\s*,\s*\(?)({old_days})(\)?)'
    )
    content = re.sub(pattern, rf'\g<1>{new_days}\g<3>', content, flags=re.DOTALL | re.IGNORECASE)
    return content


def _update_late_threshold_in_content(
    content: str,
    customer_name: str,
    old_threshold: int,
    new_threshold: int,
) -> str:
    """Surgically update the late threshold for a customer in SQL content."""
    escaped = customer_name.replace("'", "''")
    customer_pattern = re.escape(f'CUSTOMERNAME = "{escaped}"')
    pattern = (
        rf'(DATEDIFF\s*\(\s*day\s*,\s*\w+\s*,\s*GETDATE\(\)\s*\)\s*-\s*\n\s*'
        rf'DATEDIFF\s*\(\s*week\s*,\s*\w+\s*,\s*GetDate\(\)\s*\)\s*\*\s*2\s*>=\s*)'
        rf'({old_threshold})'
        rf'(\s+and\s+{customer_pattern})'
    )
    content = re.sub(pattern, rf'\g<1>{new_threshold}\g<3>', content, flags=re.DOTALL | re.IGNORECASE)
    return content


def _add_rule_to_content(content: str, rule: LeadTimeRule) -> str:
    """Add a new customer rule to both CASE blocks in SQL content."""
    ship_when = _generate_ship_date_when(rule)
    late_when = _generate_late_when(rule)

    content = re.sub(
        r'(\s*else\s*""\s*\n\s*end\s+Estimated_Ship_Date)',
        f"\n{ship_when}\n\\1",
        content,
        count=1,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r'(\s*else\s*"OnTime"\s*\n\s*end\s+LeadTime)',
        f"\n{late_when}\n\\1",
        content,
        count=1,
        flags=re.IGNORECASE,
    )
    return content


def _remove_customer_from_content(content: str, customer_name: str) -> str:
    """Remove all WHEN clauses for a customer from both CASE blocks."""
    escaped = re.escape(customer_name.replace("'", "''"))
    pattern = rf'\s*when\s+[^\n]*CUSTOMERNAME\s*(?:=|like)\s*"{escaped}".*?(?=\n\s*when\s|\n\s*else\s|\n\s*end\s)'
    content = re.sub(pattern, "", content, flags=re.DOTALL | re.IGNORECASE)
    return content


# ---------------------------------------------------------------------------
# Write operations (tools called by the agent)
# ---------------------------------------------------------------------------

def _backup_content(job_name: str, content: str) -> str:
    """Save a backup of job step content to a local file before modifying."""
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    safe_name = job_name.replace(" ", "_")
    backup_path = os.path.join(backup_dir, f"{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql")
    Path(backup_path).write_text(content, encoding="utf-8")
    logger.info(f"Backed up {job_name} -> {backup_path}")
    return backup_path


def update_lead_time(
    customer_name: str,
    new_days: int,
    file_target: str = "both",
    also_update_late: bool = True,
    **kwargs,
) -> Dict[str, Any]:
    """Update the lead time (business days) for a customer.

    Args:
        customer_name: Customer name
        new_days: New number of business days
        file_target: 'shipping', 'production', or 'both'
        also_update_late: Also update late threshold to new_days + 1
    """
    sources = _get_job_content(file_target)
    if not sources:
        return {"ok": False, "error": "No SQL Agent jobs accessible"}

    changes = {}
    for src in sources:
        try:
            parsed = parse_sql_content(src["content"], source=src["job_name"], file_type=src["file_type"])
            matching = parsed.find_rules(customer_name)
            if not matching:
                changes[src["file_type"]] = {"error": f"No rules found for '{customer_name}'"}
                continue

            content = src["content"]
            original = content

            for rule in matching:
                if rule.ship_date_days is not None and rule.ship_date_method in (
                    "bus_days_created", "bus_days_ordered"
                ):
                    content = _update_days_in_content(
                        content, customer_name, rule.ship_date_days, new_days,
                        rule.product_lines, rule.item_codes,
                    )
                    if also_update_late and rule.late_threshold_days is not None:
                        new_threshold = new_days + 1
                        content = _update_late_threshold_in_content(
                            content, customer_name, rule.late_threshold_days, new_threshold,
                        )

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{src['job_name']}",
                tofile=f"modified/{src['job_name']}",
                n=3,
            ))

            changes[src["file_type"]] = {
                "job_name": src["job_name"],
                "diff": "".join(diff) if diff else "(no changes)",
                "rules_affected": len(matching),
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[src["file_type"]] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def add_customer_rule(
    customer_name: str,
    ship_date_days: int,
    product_lines: Optional[List[str]] = None,
    late_threshold_days: Optional[int] = None,
    ship_date_method: str = "bus_days_created",
    file_target: str = "both",
    **kwargs,
) -> Dict[str, Any]:
    """Add a new customer lead time rule.

    Args:
        customer_name: New customer name
        ship_date_days: Business days for estimated ship date
        product_lines: Product line codes (default: standard set)
        late_threshold_days: Days before late (default: ship_date_days + 1)
        ship_date_method: Calculation method (default: bus_days_created)
        file_target: 'shipping', 'production', or 'both'
    """
    if product_lines is None:
        product_lines = ["0068", "0069", "0010", "0091", "0048"]
    if late_threshold_days is None:
        late_threshold_days = ship_date_days + 1

    rule = LeadTimeRule(
        customer_name=customer_name,
        product_lines=product_lines,
        item_codes=None,
        customer_po_pattern=None,
        ship_date_method=ship_date_method,
        ship_date_days=ship_date_days,
        late_threshold_days=late_threshold_days,
        late_status="Late",
        late_date_basis="DATECREATED",
    )

    sources = _get_job_content(file_target)
    if not sources:
        return {"ok": False, "error": "No SQL Agent jobs accessible"}

    changes = {}
    for src in sources:
        try:
            content = src["content"]
            original = content
            content = _add_rule_to_content(content, rule)

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{src['job_name']}",
                tofile=f"modified/{src['job_name']}",
                n=3,
            ))

            changes[src["file_type"]] = {
                "job_name": src["job_name"],
                "diff": "".join(diff) if diff else "(no changes)",
                "rule_added": rule.summary(),
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[src["file_type"]] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def remove_customer_rule(
    customer_name: str,
    file_target: str = "both",
    **kwargs,
) -> Dict[str, Any]:
    """Remove all lead time rules for a customer.

    Args:
        customer_name: Customer to remove
        file_target: 'shipping', 'production', or 'both'
    """
    sources = _get_job_content(file_target)
    if not sources:
        return {"ok": False, "error": "No SQL Agent jobs accessible"}

    changes = {}
    for src in sources:
        try:
            content = src["content"]
            original = content
            content = _remove_customer_from_content(content, customer_name)

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{src['job_name']}",
                tofile=f"modified/{src['job_name']}",
                n=3,
            ))

            changes[src["file_type"]] = {
                "job_name": src["job_name"],
                "diff": "".join(diff) if diff else "(no changes)",
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[src["file_type"]] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def update_late_threshold(
    customer_name: str,
    new_threshold: int,
    file_target: str = "both",
    **kwargs,
) -> Dict[str, Any]:
    """Update only the late threshold for a customer (without changing ship date days).

    Args:
        customer_name: Customer name
        new_threshold: New business day threshold for Late status
        file_target: 'shipping', 'production', or 'both'
    """
    sources = _get_job_content(file_target)
    if not sources:
        return {"ok": False, "error": "No SQL Agent jobs accessible"}

    changes = {}
    for src in sources:
        try:
            parsed = parse_sql_content(src["content"], source=src["job_name"], file_type=src["file_type"])
            matching = parsed.find_rules(customer_name)
            if not matching:
                changes[src["file_type"]] = {"error": f"No rules found for '{customer_name}'"}
                continue

            content = src["content"]
            original = content

            for rule in matching:
                if rule.late_threshold_days is not None:
                    content = _update_late_threshold_in_content(
                        content, customer_name, rule.late_threshold_days, new_threshold,
                    )

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{src['job_name']}",
                tofile=f"modified/{src['job_name']}",
                n=3,
            ))

            changes[src["file_type"]] = {
                "job_name": src["job_name"],
                "diff": "".join(diff) if diff else "(no changes)",
                "rules_affected": len(matching),
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[src["file_type"]] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def apply_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Write pending changes to SQL Server Agent job steps after user confirmation.

    Args:
        changes: The changes dict from a previous update/add/remove operation
    """
    applied = {}
    for file_type, change_info in changes.items():
        if isinstance(change_info, dict) and "_new_content" in change_info:
            job_name = change_info["job_name"]
            try:
                backup = _backup_content(job_name, change_info["_original_content"])
                _write_job_step(job_name, change_info["_new_content"])
                applied[file_type] = {
                    "job_name": job_name,
                    "backup": backup,
                    "status": "written",
                }
                logger.info(f"Applied changes to job '{job_name}'")
            except Exception as e:
                applied[file_type] = {"job_name": job_name, "error": str(e)}
        else:
            applied[file_type] = {"status": "skipped", "reason": "no changes or error"}

    return {"ok": True, "applied": applied}
