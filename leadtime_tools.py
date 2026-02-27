"""Tool implementations for the LeadTime Bot - CRUD operations on SQL lead time rules."""

from __future__ import annotations

import os
import re
import shutil
import difflib
import logging
from glob import glob
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

from leadtime_models import LeadTimeRule, SqlFileRules
from leadtime_parser import parse_sql_file, read_sql_file

logger = logging.getLogger(__name__)

SQL_FILES_PATH = os.getenv("SQL_FILES_PATH", r"\\jef-sql\Apps-Reports")

# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_sql_files(date_str: Optional[str] = None) -> Dict[str, Any]:
    """Find the SQL files for a given date (default: today).

    Returns dict with shipping_file, production_file paths or error.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    base = SQL_FILES_PATH
    shipping_pattern = os.path.join(base, f"OpenOrderShippingWeb_{date_str}.sql")
    production_pattern = os.path.join(base, f"prod_scheduler_{date_str}.sql")

    shipping_files = glob(shipping_pattern)
    production_files = glob(production_pattern)

    result = {
        "date": date_str,
        "shipping_file": shipping_files[0] if shipping_files else None,
        "production_file": production_files[0] if production_files else None,
    }

    if not result["shipping_file"] and not result["production_file"]:
        # Try to find the most recent files
        all_shipping = sorted(glob(os.path.join(base, "OpenOrderShippingWeb_*.sql")))
        all_production = sorted(glob(os.path.join(base, "prod_scheduler_*.sql")))
        if all_shipping:
            result["shipping_file"] = all_shipping[-1]
            result["note"] = f"No files for {date_str}, using most recent"
        if all_production:
            result["production_file"] = all_production[-1]

    return result


def _get_file_paths(file_target: str, date_str: Optional[str] = None) -> List[str]:
    """Resolve file target ('both', 'shipping', 'production') to actual paths."""
    files_info = find_sql_files(date_str)
    paths = []
    if file_target in ("both", "shipping") and files_info.get("shipping_file"):
        paths.append(files_info["shipping_file"])
    if file_target in ("both", "production") and files_info.get("production_file"):
        paths.append(files_info["production_file"])
    return paths


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_customers(file_target: str = "both", date_str: Optional[str] = None) -> Dict[str, Any]:
    """List all customers and their lead times from the specified SQL file(s).

    Args:
        file_target: 'shipping', 'production', or 'both'
        date_str: Date string YYYYMMDD (default: today)
    """
    paths = _get_file_paths(file_target, date_str)
    if not paths:
        return {"ok": False, "error": f"No SQL files found for target={file_target}"}

    results = {}
    for path in paths:
        try:
            parsed = parse_sql_file(path)
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
            results[parsed.file_type] = {
                "file": path,
                "customer_count": len(customers),
                "customers": customers,
            }
        except Exception as e:
            results[Path(path).stem] = {"file": path, "error": str(e)}

    return {"ok": True, "results": results}


def get_customer_rules(
    customer_name: str,
    file_target: str = "both",
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Get detailed rules for a specific customer.

    Args:
        customer_name: Customer name to search for (case-insensitive)
        file_target: 'shipping', 'production', or 'both'
        date_str: Date string YYYYMMDD (default: today)
    """
    paths = _get_file_paths(file_target, date_str)
    if not paths:
        return {"ok": False, "error": f"No SQL files found"}

    results = {}
    for path in paths:
        try:
            parsed = parse_sql_file(path)
            matching = parsed.find_rules(customer_name)
            if matching:
                results[parsed.file_type] = {
                    "file": path,
                    "rules": [r.model_dump() for r in matching],
                    "summaries": [r.summary() for r in matching],
                }
            else:
                results[parsed.file_type] = {
                    "file": path,
                    "rules": [],
                    "message": f"No rules found for '{customer_name}'",
                }
        except Exception as e:
            results[Path(path).stem] = {"error": str(e)}

    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# SQL generation helpers
# ---------------------------------------------------------------------------

def _generate_ship_date_when(rule: LeadTimeRule) -> str:
    """Generate a WHEN clause for the Estimated_Ship_Date CASE block."""
    # Build condition
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

    # Build result expression
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
# Write operations
# ---------------------------------------------------------------------------

def _backup_file(file_path: str) -> str:
    """Create a backup of a SQL file before modifying. Returns backup path."""
    backup_path = file_path + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(file_path, backup_path)
    logger.info(f"Backed up {file_path} -> {backup_path}")
    return backup_path


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

    # Build a regex that finds the customer's WHEN clause and captures the days value
    # in BusDaysDateAdd calls
    customer_pattern = re.escape(f'CUSTOMERNAME = "{escaped}"')

    # Find all occurrences of this customer's BusDaysDateAdd clauses
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

    # Find the DATEDIFF >= N pattern near this customer name
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
    # Generate the WHEN clauses
    ship_when = _generate_ship_date_when(rule)
    late_when = _generate_late_when(rule)

    # Insert ship date WHEN before the 'else ""' in Estimated_Ship_Date CASE
    content = re.sub(
        r'(\s*else\s*""\s*\n\s*end\s+Estimated_Ship_Date)',
        f"\n{ship_when}\n\\1",
        content,
        count=1,
        flags=re.IGNORECASE,
    )

    # Insert late WHEN before the 'else "OnTime"' in LeadTime CASE
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

    # Remove WHEN clauses that reference this customer
    # Match from 'when' to 'then (...)' or 'then "..."', including multiline item codes
    pattern = rf'\s*when\s+[^\n]*CUSTOMERNAME\s*(?:=|like)\s*"{escaped}".*?(?=\n\s*when\s|\n\s*else\s|\n\s*end\s)'
    content = re.sub(pattern, "", content, flags=re.DOTALL | re.IGNORECASE)

    return content


# ---------------------------------------------------------------------------
# Tool functions (called by the agent)
# ---------------------------------------------------------------------------

def update_lead_time(
    customer_name: str,
    new_days: int,
    file_target: str = "both",
    date_str: Optional[str] = None,
    also_update_late: bool = True,
) -> Dict[str, Any]:
    """Update the lead time (business days) for a customer.

    Args:
        customer_name: Customer name
        new_days: New number of business days
        file_target: 'shipping', 'production', or 'both'
        date_str: Date string YYYYMMDD (default: today)
        also_update_late: Also update late threshold to new_days + 1
    """
    paths = _get_file_paths(file_target, date_str)
    if not paths:
        return {"ok": False, "error": "No SQL files found"}

    changes = {}
    for path in paths:
        try:
            parsed = parse_sql_file(path)
            matching = parsed.find_rules(customer_name)
            if not matching:
                changes[parsed.file_type] = {"error": f"No rules found for '{customer_name}'"}
                continue

            content = read_sql_file(path)
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

            # Generate diff
            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{Path(path).name}",
                tofile=f"modified/{Path(path).name}",
                n=3,
            ))

            changes[parsed.file_type] = {
                "file": path,
                "diff": "".join(diff) if diff else "(no changes)",
                "rules_affected": len(matching),
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[Path(path).stem] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def add_customer_rule(
    customer_name: str,
    ship_date_days: int,
    product_lines: Optional[List[str]] = None,
    late_threshold_days: Optional[int] = None,
    ship_date_method: str = "bus_days_created",
    file_target: str = "both",
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a new customer lead time rule.

    Args:
        customer_name: New customer name
        ship_date_days: Business days for estimated ship date
        product_lines: Product line codes (default: standard set)
        late_threshold_days: Days before late (default: ship_date_days + 1)
        ship_date_method: Calculation method (default: bus_days_created)
        file_target: 'shipping', 'production', or 'both'
        date_str: Date string YYYYMMDD (default: today)
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

    paths = _get_file_paths(file_target, date_str)
    if not paths:
        return {"ok": False, "error": "No SQL files found"}

    changes = {}
    for path in paths:
        try:
            content = read_sql_file(path)
            original = content
            content = _add_rule_to_content(content, rule)

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{Path(path).name}",
                tofile=f"modified/{Path(path).name}",
                n=3,
            ))

            file_type = "shipping" if "OpenOrderShipping" in path else "production"
            changes[file_type] = {
                "file": path,
                "diff": "".join(diff) if diff else "(no changes)",
                "rule_added": rule.summary(),
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[Path(path).stem] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def remove_customer_rule(
    customer_name: str,
    file_target: str = "both",
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove all lead time rules for a customer.

    Args:
        customer_name: Customer to remove
        file_target: 'shipping', 'production', or 'both'
        date_str: Date string YYYYMMDD (default: today)
    """
    paths = _get_file_paths(file_target, date_str)
    if not paths:
        return {"ok": False, "error": "No SQL files found"}

    changes = {}
    for path in paths:
        try:
            content = read_sql_file(path)
            original = content
            content = _remove_customer_from_content(content, customer_name)

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{Path(path).name}",
                tofile=f"modified/{Path(path).name}",
                n=3,
            ))

            file_type = "shipping" if "OpenOrderShipping" in path else "production"
            changes[file_type] = {
                "file": path,
                "diff": "".join(diff) if diff else "(no changes)",
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[Path(path).stem] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def update_late_threshold(
    customer_name: str,
    new_threshold: int,
    file_target: str = "both",
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Update only the late threshold for a customer (without changing ship date days).

    Args:
        customer_name: Customer name
        new_threshold: New business day threshold for Late status
        file_target: 'shipping', 'production', or 'both'
        date_str: Date string YYYYMMDD (default: today)
    """
    paths = _get_file_paths(file_target, date_str)
    if not paths:
        return {"ok": False, "error": "No SQL files found"}

    changes = {}
    for path in paths:
        try:
            parsed = parse_sql_file(path)
            matching = parsed.find_rules(customer_name)
            if not matching:
                changes[parsed.file_type] = {"error": f"No rules found for '{customer_name}'"}
                continue

            content = read_sql_file(path)
            original = content

            for rule in matching:
                if rule.late_threshold_days is not None:
                    content = _update_late_threshold_in_content(
                        content, customer_name, rule.late_threshold_days, new_threshold,
                    )

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"original/{Path(path).name}",
                tofile=f"modified/{Path(path).name}",
                n=3,
            ))

            changes[parsed.file_type] = {
                "file": path,
                "diff": "".join(diff) if diff else "(no changes)",
                "rules_affected": len(matching),
                "_new_content": content,
                "_original_content": original,
            }
        except Exception as e:
            changes[Path(path).stem] = {"error": str(e)}

    return {"ok": True, "changes": changes, "status": "preview"}


def apply_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Write pending changes to disk after user confirmation.

    Args:
        changes: The changes dict from a previous update/add/remove operation
    """
    applied = {}
    for file_type, change_info in changes.items():
        if isinstance(change_info, dict) and "_new_content" in change_info:
            path = change_info["file"]
            try:
                backup = _backup_file(path)
                Path(path).write_text(change_info["_new_content"], encoding="utf-8")
                applied[file_type] = {
                    "file": path,
                    "backup": backup,
                    "status": "written",
                }
                logger.info(f"Applied changes to {path}")
            except Exception as e:
                applied[file_type] = {"file": path, "error": str(e)}
        else:
            applied[file_type] = {"status": "skipped", "reason": "no changes or error"}

    return {"ok": True, "applied": applied}
