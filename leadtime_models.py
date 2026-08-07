"""Data models for lead time rules parsed from SQL Server Agent job scripts."""

from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class LeadTimeRule(BaseModel):
    """A single customer lead time rule extracted from a SQL CASE statement."""

    customer_name: str = Field(description="Customer name exactly as it appears in SQL (double-quoted)")
    product_lines: List[str] = Field(description="Product line codes, e.g. ['0068','0069','0010']")
    item_codes: Optional[List[str]] = Field(default=None, description="Item codes if rule is item-specific")
    customer_po_pattern: Optional[str] = Field(default=None, description="CUSTOMERPONO LIKE pattern, e.g. '1%'")

    # Estimated ship date calculation
    ship_date_method: str = Field(
        description="One of: bus_days_created, bus_days_ordered, ship_expire_date, calendar_days_ord_date"
    )
    ship_date_days: Optional[int] = Field(
        default=None,
        description="Number of days for the ship date calc (None for ship_expire_date)"
    )

    # Late threshold
    late_threshold_days: Optional[int] = Field(
        default=None,
        description="Business days threshold for Late status"
    )
    late_status: str = Field(
        default="Late",
        description="Status when threshold exceeded: 'Late' or 'Ship date assigned by customer'"
    )
    late_date_basis: str = Field(
        default="DATECREATED",
        description="Date column used for late calc: 'DATECREATED' or 'ORDERDATE'"
    )

    def summary(self) -> str:
        """Human-readable summary of this rule."""
        method_desc = {
            "bus_days_created": f"{self.ship_date_days} business days from DATECREATED",
            "bus_days_ordered": f"{self.ship_date_days} business days from ORDERDATE",
            "ship_expire_date": "SHIPEXPIREDATE (customer-assigned)",
            "calendar_days_ord_date": f"{self.ship_date_days} calendar days from UDF_ORD_DATE",
        }
        parts = [
            f"Customer: {self.customer_name}",
            f"  Ship date: {method_desc.get(self.ship_date_method, self.ship_date_method)}",
            f"  Product lines: {', '.join(self.product_lines)}",
        ]
        if self.item_codes:
            parts.append(f"  Item codes: {len(self.item_codes)} specific items")
        if self.customer_po_pattern:
            parts.append(f"  PO pattern: LIKE '{self.customer_po_pattern}'")
        if self.late_threshold_days is not None:
            parts.append(f"  Late after: {self.late_threshold_days} business days ({self.late_status})")
        else:
            parts.append(f"  Late status: {self.late_status}")
        return "\n".join(parts)


class SqlFileRules(BaseModel):
    """All parsed rules from a single SQL file."""

    file_path: str = Field(description="Full path to the SQL file")
    file_type: str = Field(description="'shipping' or 'production'")
    rules: List[LeadTimeRule] = Field(default_factory=list)
    excluded_customers: List[str] = Field(
        default_factory=list,
        description="Customers excluded in WHERE NOT IN (shipping file only)"
    )
    excluded_items: List[str] = Field(
        default_factory=list,
        description="Items excluded in WHERE NOT IN (shipping file only)"
    )

    def find_rules(self, customer_name: str) -> List[LeadTimeRule]:
        """Find all rules for a customer (case-insensitive)."""
        return [r for r in self.rules if r.customer_name.upper() == customer_name.upper()]

    def customer_names(self) -> List[str]:
        """Get unique customer names."""
        seen = set()
        names = []
        for r in self.rules:
            key = r.customer_name.upper()
            if key not in seen:
                seen.add(key)
                names.append(r.customer_name)
        return sorted(names, key=str.upper)
