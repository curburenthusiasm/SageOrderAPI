"""EDI workflow templates for building complete workflows in Tray.io.

Each template defines the steps, connectors, and configuration needed to build
a full EDI workflow via the Tray.io UI. Templates follow the Jeffco Fibres
Order-to-Cash flow across TLW, Cleo, Sage 100, ShipExec, and ShipStation.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from edi_models import WorkflowStep, WorkflowTemplate


# ---------------------------------------------------------------------------
# Helper: build standard Cleo SFTP connection config
# ---------------------------------------------------------------------------

CLEO_SFTP_CONFIG = {
    "host": "cleo-lexicom",
    "port": "22",
    "username": "{trading_partner_sftp_user}",
    "auth_type": "key",
}

SAGE_SQL_CONFIG = {
    "host": "JEF-SQL",
    "database": "MAS_JEF",
    "username": "MAS_REPORTS",
    "auth_type": "sql",
}


# ---------------------------------------------------------------------------
# Template 1: EDI 850 — Purchase Order Inbound
# ---------------------------------------------------------------------------

TEMPLATE_850 = WorkflowTemplate(
    name="EDI 850 - Purchase Order Inbound",
    template_key="850",
    trigger_type="schedule",
    trigger_config={"interval": "5", "unit": "minutes"},
    edi_doc_type="850",
    direction="inbound",
    description=(
        "Polls Cleo SFTP for inbound 850 Purchase Orders, parses via TLW EDI Translator, "
        "creates Sales Orders in Sage 100, and sends 997 Functional Acknowledgment."
    ),
    steps=[
        WorkflowStep(
            connector="SFTP",
            operation="List Files",
            name="Poll Cleo Inbound",
            config={
                "directory": "/inbound/850",
                "pattern": "*.edi",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Boolean Condition",
            operation="Evaluate",
            name="Check for New Files",
            config={
                "condition": "file_count > 0",
                "description": "Only proceed if new 850 files found",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Download File",
            name="Download 850 File",
            config={
                "directory": "/inbound/850",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Parse 850 via TLW",
            config={
                "language": "python",
                "description": "Call TLW EDI Translator to parse X12 850 into JSON order data",
                "script": "# TLW parses 850 EDI document\n# Extracts: PO number, line items, ship-to, dates",
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Create Sage Sales Order",
            config={
                "description": "Insert sales order header and lines into Sage 100 SO tables",
                "query_type": "stored_procedure",
                "procedure": "usp_EDI_CreateSalesOrder",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Generate 997 ACK",
            config={
                "language": "python",
                "description": "Generate 997 Functional Acknowledgment for the received 850",
                "script": "# TLW generates 997 acknowledging receipt of 850",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Upload File",
            name="Send 997 via Cleo",
            config={
                "directory": "/outbound/997",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Move File",
            name="Archive 850 File",
            config={
                "source_directory": "/inbound/850",
                "destination_directory": "/archive/850",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Email",
            operation="Send Email",
            name="Notify EDI Team",
            config={
                "to": "edi@jeffcofibres.com",
                "subject": "EDI 850 Processed - {po_number}",
                "body": "Purchase Order {po_number} from {trading_partner} has been processed and Sales Order created in Sage.",
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template 2: EDI 810 — Invoice Outbound
# ---------------------------------------------------------------------------

TEMPLATE_810 = WorkflowTemplate(
    name="EDI 810 - Invoice Outbound",
    template_key="810",
    trigger_type="schedule",
    trigger_config={"interval": "30", "unit": "minutes"},
    edi_doc_type="810",
    direction="outbound",
    description=(
        "Queries Sage 100 for new invoices, generates 810 EDI documents via TLW, "
        "and transmits to trading partners through Cleo SFTP."
    ),
    steps=[
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Query New Invoices",
            config={
                "description": "Find invoices in Sage 100 not yet sent as EDI 810",
                "query": "SELECT * FROM AR_InvoiceHistoryHeader WHERE EDI_Sent = 0 AND EDI_Partner = 1",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Boolean Condition",
            operation="Evaluate",
            name="Check for Invoices",
            config={
                "condition": "invoice_count > 0",
                "description": "Only proceed if unsent invoices exist",
            },
        ),
        WorkflowStep(
            connector="Loop",
            operation="For Each",
            name="Process Each Invoice",
            config={
                "description": "Loop through each new invoice to generate EDI",
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Get Invoice Lines",
            config={
                "description": "Fetch line-item detail for the current invoice",
                "query": "SELECT * FROM AR_InvoiceHistoryDetail WHERE InvoiceNo = {invoice_no}",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Generate 810 EDI",
            config={
                "language": "python",
                "description": "TLW generates X12 810 Invoice document from Sage invoice data",
                "script": "# TLW generates 810 with header, line items, summary segments",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Upload File",
            name="Send 810 via Cleo",
            config={
                "directory": "/outbound/810",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Mark Invoice Sent",
            config={
                "description": "Update Sage to mark invoice as EDI-sent",
                "query": "UPDATE AR_InvoiceHistoryHeader SET EDI_Sent = 1 WHERE InvoiceNo = {invoice_no}",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Email",
            operation="Send Email",
            name="Notify EDI Team",
            config={
                "to": "edi@jeffcofibres.com",
                "subject": "EDI 810 Sent - {invoice_count} invoices",
                "body": "Successfully generated and sent {invoice_count} EDI 810 invoices.",
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template 3: EDI 856 — ASN Outbound
# ---------------------------------------------------------------------------

TEMPLATE_856 = WorkflowTemplate(
    name="EDI 856 - ASN Outbound",
    template_key="856",
    trigger_type="webhook",
    trigger_config={"description": "Triggered by ShipExec/ShipStation shipment confirmation"},
    edi_doc_type="856",
    direction="outbound",
    description=(
        "Triggered when a shipment is confirmed in ShipExec/ShipStation. Generates "
        "856 Advance Ship Notice via TLW and sends to trading partner through Cleo."
    ),
    steps=[
        WorkflowStep(
            connector="Trigger",
            operation="Webhook Received",
            name="Shipment Confirmed",
            config={
                "description": "Receives shipment data from ShipExec/ShipStation webhook",
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Get Order Details",
            config={
                "description": "Fetch original order and shipping details from Sage 100",
                "query": "SELECT so.*, sh.TrackingNumber, sh.CarrierCode FROM SO_SalesOrderHeader so JOIN ShipmentHeader sh ON so.SalesOrderNo = sh.SalesOrderNo WHERE sh.ShipmentID = {shipment_id}",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Generate 856 ASN",
            config={
                "language": "python",
                "description": "TLW generates X12 856 ASN with shipment, order, and pack-level data",
                "script": "# TLW generates 856 with BSN, HL loops, tracking, carrier info",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Upload File",
            name="Send 856 via Cleo",
            config={
                "directory": "/outbound/856",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Update Shipment Status",
            config={
                "description": "Mark the shipment as ASN-sent in Sage",
                "query": "UPDATE SO_SalesOrderHeader SET ASN_Sent = 1 WHERE SalesOrderNo = {order_no}",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Email",
            operation="Send Email",
            name="Notify EDI Team",
            config={
                "to": "edi@jeffcofibres.com",
                "subject": "EDI 856 ASN Sent - Order {order_no}",
                "body": "ASN sent for order {order_no} to {trading_partner}. Tracking: {tracking_number}",
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template 4: EDI 846 — Inventory Inquiry
# ---------------------------------------------------------------------------

TEMPLATE_846 = WorkflowTemplate(
    name="EDI 846 - Inventory Inquiry",
    template_key="846",
    trigger_type="schedule",
    trigger_config={"interval": "1", "unit": "hours"},
    edi_doc_type="846",
    direction="outbound",
    description=(
        "Periodically queries Sage 100 inventory levels and generates 846 Inventory "
        "Inquiry documents for trading partners that require stock visibility."
    ),
    steps=[
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Query Inventory Levels",
            config={
                "description": "Get current on-hand inventory for EDI partner items",
                "query": "SELECT ItemCode, WarehouseCode, QuantityOnHand, QuantityOnSalesOrder, QuantityOnPurchaseOrder FROM CI_Item WHERE EDI_Reportable = 1",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Boolean Condition",
            operation="Evaluate",
            name="Check Inventory Data",
            config={
                "condition": "item_count > 0",
                "description": "Only proceed if reportable items found",
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Generate 846 EDI",
            config={
                "language": "python",
                "description": "TLW generates X12 846 with inventory levels by item and warehouse",
                "script": "# TLW generates 846 with QTY segments per item",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Upload File",
            name="Send 846 via Cleo",
            config={
                "directory": "/outbound/846",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Log Inventory Report",
            config={
                "description": "Record that inventory report was sent",
                "query": "INSERT INTO EDI_Log (DocType, Direction, Timestamp, ItemCount) VALUES ('846', 'OUT', GETDATE(), {item_count})",
                **SAGE_SQL_CONFIG,
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template 5: EDI 997 — Functional Acknowledgment
# ---------------------------------------------------------------------------

TEMPLATE_997 = WorkflowTemplate(
    name="EDI 997 - Functional Acknowledgment",
    template_key="997",
    trigger_type="schedule",
    trigger_config={"interval": "5", "unit": "minutes"},
    edi_doc_type="997",
    direction="inbound",
    description=(
        "Polls for inbound 997 Functional Acknowledgments from trading partners, "
        "parses them, and logs acceptance/rejection status for previously sent documents."
    ),
    steps=[
        WorkflowStep(
            connector="SFTP",
            operation="List Files",
            name="Poll Cleo for 997s",
            config={
                "directory": "/inbound/997",
                "pattern": "*.edi",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Boolean Condition",
            operation="Evaluate",
            name="Check for 997 Files",
            config={
                "condition": "file_count > 0",
                "description": "Only proceed if new 997 files found",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Download File",
            name="Download 997 File",
            config={
                "directory": "/inbound/997",
                **CLEO_SFTP_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Parse 997 via TLW",
            config={
                "language": "python",
                "description": "TLW parses 997 to extract acceptance/rejection status per transaction set",
                "script": "# TLW parses AK1/AK9 segments for accept/reject codes",
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Update ACK Status",
            config={
                "description": "Log 997 status in EDI tracking table",
                "query": "UPDATE EDI_Log SET AckStatus = {ack_status}, AckTimestamp = GETDATE() WHERE ControlNumber = {control_number}",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Boolean Condition",
            operation="Evaluate",
            name="Check for Rejections",
            config={
                "condition": "has_rejections == true",
                "description": "Check if any transaction sets were rejected",
            },
        ),
        WorkflowStep(
            connector="Email",
            operation="Send Email",
            name="Alert on Rejections",
            config={
                "to": "edi@jeffcofibres.com",
                "subject": "EDI 997 REJECTION - {original_doc_type} {control_number}",
                "body": "Trading partner {trading_partner} rejected {original_doc_type} with control number {control_number}. Error codes: {error_codes}. Immediate investigation required.",
            },
        ),
        WorkflowStep(
            connector="SFTP",
            operation="Move File",
            name="Archive 997 File",
            config={
                "source_directory": "/inbound/997",
                "destination_directory": "/archive/997",
                **CLEO_SFTP_CONFIG,
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template 6: EDI Error Monitor
# ---------------------------------------------------------------------------

TEMPLATE_ERROR_MONITOR = WorkflowTemplate(
    name="EDI Error Monitor",
    template_key="error_monitor",
    trigger_type="schedule",
    trigger_config={"interval": "15", "unit": "minutes"},
    edi_doc_type=None,
    direction=None,
    description=(
        "Monitors all EDI workflows for failures, checks Cleo connectivity, "
        "verifies Sage SQL Server health, and sends consolidated error alerts."
    ),
    steps=[
        WorkflowStep(
            connector="HTTP Client",
            operation="GET Request",
            name="Check Cleo Health",
            config={
                "url": "http://cleo-lexicom:5080/api/health",
                "description": "Verify Cleo Lexicom AS2/SFTP gateway is responding",
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Check Sage Connection",
            config={
                "description": "Verify Sage 100 SQL Server is accessible",
                "query": "SELECT 1 AS HealthCheck",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Check Stale EDI Files",
            config={
                "description": "Find EDI files older than 1 hour that haven't been processed",
                "query": "SELECT * FROM EDI_Log WHERE Status = 'pending' AND Timestamp < DATEADD(hour, -1, GETDATE())",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="SQL Server",
            operation="Execute Query",
            name="Check Failed Transactions",
            config={
                "description": "Find EDI transactions that failed in the last 24 hours",
                "query": "SELECT * FROM EDI_Log WHERE Status = 'error' AND Timestamp > DATEADD(day, -1, GETDATE())",
                **SAGE_SQL_CONFIG,
            },
        ),
        WorkflowStep(
            connector="Script",
            operation="Run Script",
            name="Compile Error Report",
            config={
                "language": "python",
                "description": "Aggregate all health checks and errors into a status report",
                "script": "# Compile: Cleo status, Sage status, stale files, failed transactions",
            },
        ),
        WorkflowStep(
            connector="Boolean Condition",
            operation="Evaluate",
            name="Check for Issues",
            config={
                "condition": "has_errors == true",
                "description": "Only send alert if issues were found",
            },
        ),
        WorkflowStep(
            connector="Email",
            operation="Send Email",
            name="Send Error Alert",
            config={
                "to": "edi@jeffcofibres.com",
                "subject": "EDI Error Alert - {error_count} issues detected",
                "body": "EDI Error Monitor detected {error_count} issues:\n\n{error_report}\n\nPlease investigate immediately.",
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

ALL_TEMPLATES: Dict[str, WorkflowTemplate] = {
    "850": TEMPLATE_850,
    "810": TEMPLATE_810,
    "856": TEMPLATE_856,
    "846": TEMPLATE_846,
    "997": TEMPLATE_997,
    "error_monitor": TEMPLATE_ERROR_MONITOR,
}


def get_template(key: str) -> Optional[WorkflowTemplate]:
    """Look up a template by key (case-insensitive)."""
    return ALL_TEMPLATES.get(key.lower().strip())


def list_templates() -> List[WorkflowTemplate]:
    """Return all available templates."""
    return list(ALL_TEMPLATES.values())
