"""Parse SQL Server Agent job scripts to extract lead time rules from CASE statements."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from leadtime_models import LeadTimeRule, SqlFileRules


def read_sql_file(file_path: str) -> str:
    """Read a SQL file, handling UNC paths on Windows."""
    return Path(file_path).read_text(encoding="utf-8-sig")


def extract_query_string(sql_content: str) -> str:
    """Extract the @query parameter value from sp_send_dbmail call."""
    # The query is between @query = ' and the closing ',
    # but it spans many lines. Find the opening and match to the end.
    match = re.search(
        r"@query\s*=\s*'(.*?)'(?:\s*,\s*\n\s*@attach_query_result_as_file)",
        sql_content,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Could not extract @query from SQL file")
    return match.group(1)


def _extract_case_block(query: str, end_label: str) -> str:
    """Extract a CASE...END block that ends with a specific alias."""
    # Find 'end <label>' and walk backward to find matching 'case'
    pattern = rf"(case\s.*?end\s+{re.escape(end_label)})"
    match = re.search(pattern, query, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not find CASE block ending with '{end_label}'")
    return match.group(1)


def _split_when_clauses(case_block: str) -> List[str]:
    """Split a CASE block into individual WHEN clauses."""
    # Split on 'when ' at start of line (allowing whitespace), keeping the delimiter
    parts = re.split(r"(?=(?:^|\n)\s*when\s)", case_block, flags=re.IGNORECASE)
    clauses = []
    for part in parts:
        stripped = part.strip()
        if stripped.upper().startswith("WHEN"):
            clauses.append(stripped)
    return clauses


def _parse_customer_name(clause: str) -> Optional[str]:
    """Extract CUSTOMERNAME from a WHEN clause."""
    # Match: CUSTOMERNAME = "..." or CUSTOMERNAME like "..."
    m = re.search(r'CUSTOMERNAME\s*(?:=|like)\s*"([^"]*)"', clause, re.IGNORECASE)
    return m.group(1) if m else None


def _parse_product_lines(clause: str) -> List[str]:
    """Extract PRODUCTLINE values from IN clause."""
    m = re.search(r'PRODUCTLINE\s+(?:in\s*\(([^)]+)\)|=\s*"([^"]*)")', clause, re.IGNORECASE)
    if not m:
        return []
    if m.group(2):
        return [m.group(2)]
    raw = m.group(1)
    return re.findall(r'"([^"]*)"', raw)


def _parse_item_codes(clause: str) -> Optional[List[str]]:
    """Extract ITEMCODE values from IN clause if present."""
    # Check for itemcode/ITEMCODE in (...)  - can span multiple lines
    m = re.search(r'(?:itemcode|ITEMCODE)\s+in\s*\(([^)]*(?:\n[^)]*)*)\)', clause, re.IGNORECASE)
    if not m:
        # Single item code match
        m = re.search(r'(?:itemcode|ITEMCODE)\s*=\s*"([^"]*)"', clause, re.IGNORECASE)
        if m:
            return [m.group(1)]
        return None
    raw = m.group(1)
    return re.findall(r'"([^"]*)"', raw)


def _parse_po_pattern(clause: str) -> Optional[str]:
    """Extract CUSTOMERPONO LIKE pattern if present."""
    m = re.search(r'CUSTOMERPONO\s+like\s+"([^"]*)"', clause, re.IGNORECASE)
    return m.group(1) if m else None


def _parse_ship_date_method(clause: str) -> Tuple[str, Optional[int]]:
    """Extract the ship date calculation method and days from a WHEN clause."""
    # bus days from DATECREATED
    m = re.search(r'BusDaysDateAdd\s*\(\s*DATECREATED\s*,\s*\(?(\d+)\)?\s*\)', clause, re.IGNORECASE)
    if m:
        return "bus_days_created", int(m.group(1))

    # bus days from ORDERDATE
    m = re.search(r'BusDaysDateAdd\s*\(\s*ORDERDATE\s*,\s*\(?(\d+)\)?\s*\)', clause, re.IGNORECASE)
    if m:
        return "bus_days_ordered", int(m.group(1))

    # SHIPEXPIREDATE
    if re.search(r'SHIPEXPIREDATE', clause, re.IGNORECASE):
        return "ship_expire_date", None

    # DATEADD(DAY, N, UDF_ORD_DATE)
    m = re.search(r'DATEADD\s*\(\s*DAY\s*,\s*(-?\d+)\s*,\s*UDF_ORD_DATE\s*\)', clause, re.IGNORECASE)
    if m:
        return "calendar_days_ord_date", int(m.group(1))

    return "unknown", None


def _parse_late_threshold(clause: str) -> Tuple[Optional[int], str, str]:
    """Extract late threshold days, status, and date basis from a LeadTime WHEN clause.

    Returns (threshold_days, status_text, date_basis).
    """
    # Check for the business-day diff pattern with DATECREATED or ORDERDATE
    m = re.search(
        r'DATEDIFF\s*\(\s*day\s*,\s*(DATECREATED|ORDERDATE)\s*,\s*GETDATE\(\)\s*\)',
        clause,
        re.IGNORECASE,
    )
    date_basis = m.group(1).upper() if m else "DATECREATED"

    # Extract threshold: >= N or > N
    m2 = re.search(r'(?:>=|>)\s*(\d+)', clause)
    threshold = int(m2.group(1)) if m2 else None

    # For "> N" (strict greater-than), the effective threshold is N+1 for >=
    if m2:
        op_match = re.search(r'(>=|>)\s*\d+', clause)
        if op_match and op_match.group(1) == ">":
            # > N is equivalent to >= N+1
            threshold = threshold + 1 if threshold is not None else None

    # Extract status text
    status = "Late"
    status_match = re.search(r'then\s+"([^"]*)"', clause, re.IGNORECASE)
    if status_match:
        status = status_match.group(1)

    return threshold, status, date_basis


def parse_estimated_ship_date_rules(case_block: str) -> List[dict]:
    """Parse Estimated_Ship_Date CASE block into raw rule dicts."""
    clauses = _split_when_clauses(case_block)
    rules = []
    for clause in clauses:
        customer = _parse_customer_name(clause)
        if not customer:
            # Might be a product-line-only rule (no customer)
            if re.search(r'PRODUCTLINE', clause, re.IGNORECASE):
                customer = "__PRODUCT_LINE_ONLY__"
            else:
                continue

        product_lines = _parse_product_lines(clause)
        item_codes = _parse_item_codes(clause)
        po_pattern = _parse_po_pattern(clause)
        method, days = _parse_ship_date_method(clause)

        rules.append({
            "customer_name": customer,
            "product_lines": product_lines,
            "item_codes": item_codes,
            "customer_po_pattern": po_pattern,
            "ship_date_method": method,
            "ship_date_days": days,
            "raw_clause": clause,
        })
    return rules


def parse_lead_time_rules(case_block: str) -> List[dict]:
    """Parse LeadTime CASE block into raw rule dicts."""
    clauses = _split_when_clauses(case_block)
    rules = []
    for clause in clauses:
        customer = _parse_customer_name(clause)
        if not customer:
            if re.search(r'PRODUCTLINE', clause, re.IGNORECASE):
                customer = "__PRODUCT_LINE_ONLY__"
            else:
                continue

        product_lines = _parse_product_lines(clause)
        item_codes = _parse_item_codes(clause)
        threshold, status, date_basis = _parse_late_threshold(clause)

        rules.append({
            "customer_name": customer,
            "product_lines": product_lines,
            "item_codes": item_codes,
            "late_threshold_days": threshold,
            "late_status": status,
            "late_date_basis": date_basis,
            "raw_clause": clause,
        })
    return rules


def _parse_where_exclusions(query: str) -> Tuple[List[str], List[str]]:
    """Parse excluded customers and items from WHERE clause NOT IN lists."""
    excluded_customers = []
    excluded_items = []

    # Customer NOT IN
    m = re.search(r'CUSTOMERNAME\s+not\s+in\s*\(([^)]*(?:\n[^)]*)*)\)', query, re.IGNORECASE)
    if m:
        excluded_customers = re.findall(r'"([^"]*)"', m.group(1))

    # Item NOT IN
    m = re.search(r'ITEMCODE\s+not\s+in\s*\(([^)]*(?:\n[^)]*)*)\)', query, re.IGNORECASE)
    if m:
        excluded_items = re.findall(r'"([^"]*)"', m.group(1))

    return excluded_customers, excluded_items


def merge_ship_and_late_rules(ship_rules: List[dict], late_rules: List[dict]) -> List[LeadTimeRule]:
    """Merge Estimated_Ship_Date rules with LeadTime rules into unified LeadTimeRule objects."""
    merged = []

    for sr in ship_rules:
        # Find matching late rule by customer + product lines + item codes
        matching_late = None
        for lr in late_rules:
            if (lr["customer_name"].upper() == sr["customer_name"].upper()
                    and set(lr.get("product_lines", [])) == set(sr.get("product_lines", []))
                    and lr.get("item_codes") == sr.get("item_codes")):
                matching_late = lr
                break

        # If no exact match, try matching just customer + product lines (ignore items)
        if not matching_late:
            for lr in late_rules:
                if (lr["customer_name"].upper() == sr["customer_name"].upper()
                        and set(lr.get("product_lines", [])) == set(sr.get("product_lines", []))):
                    matching_late = lr
                    break

        rule = LeadTimeRule(
            customer_name=sr["customer_name"],
            product_lines=sr.get("product_lines", []),
            item_codes=sr.get("item_codes"),
            customer_po_pattern=sr.get("customer_po_pattern"),
            ship_date_method=sr["ship_date_method"],
            ship_date_days=sr.get("ship_date_days"),
            late_threshold_days=matching_late["late_threshold_days"] if matching_late else None,
            late_status=matching_late["late_status"] if matching_late else "Late",
            late_date_basis=matching_late.get("late_date_basis", "DATECREATED") if matching_late else "DATECREATED",
        )
        merged.append(rule)

    return merged


def detect_file_type(sql_content: str) -> str:
    """Detect whether this is the shipping or production file."""
    if "OpenOrderWebShipping" in sql_content or "Webster_Open_Orders_Shipping" in sql_content:
        return "shipping"
    elif "ProductionSchedule" in sql_content or "All_Open_OrdersOPS" in sql_content:
        return "production"
    return "unknown"


def parse_sql_content(content: str, source: str, file_type: str | None = None) -> SqlFileRules:
    """Parse SQL content string and return all extracted rules.

    Args:
        content: The raw SQL job step command text.
        source: Label for where the content came from (job name, file path, etc.).
        file_type: 'shipping' or 'production'. Auto-detected if None.
    """
    if file_type is None:
        file_type = detect_file_type(content)
    query = extract_query_string(content)

    # Extract CASE blocks
    ship_case = _extract_case_block(query, "Estimated_Ship_Date")
    late_case = _extract_case_block(query, "LeadTime")

    # Parse individual rules
    ship_rules = parse_estimated_ship_date_rules(ship_case)
    late_rules = parse_lead_time_rules(late_case)

    # Merge into unified rules
    merged = merge_ship_and_late_rules(ship_rules, late_rules)

    # Parse WHERE exclusions
    excluded_customers, excluded_items = _parse_where_exclusions(query)

    return SqlFileRules(
        file_path=source,
        file_type=file_type,
        rules=merged,
        excluded_customers=excluded_customers,
        excluded_items=excluded_items,
    )


def parse_sql_file(file_path: str) -> SqlFileRules:
    """Parse a SQL file and return all extracted rules."""
    content = read_sql_file(file_path)
    return parse_sql_content(content, source=file_path)
