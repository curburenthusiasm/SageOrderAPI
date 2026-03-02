#!/usr/bin/env python3
"""
Enterprise Systems Knowledge Graph Crawler

Discovers and maps enterprise software used at Jeffco Fibres:
- TLW EDI Translator (EDI processing)
- Cleo Lexicom (EDI connectors/communications)
- Sage 100 (ERP system)
- ShipExec (Warehouse Management)
- ShipStation (Shipping platform)

Builds a comprehensive knowledge graph of:
1. System capabilities and features
2. Data flows between systems
3. Integration points and APIs
4. Business processes and workflows
5. Configuration and setup details
"""

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
import re

try:
    import pyodbc
except ImportError:
    pyodbc = None

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
except ImportError:
    webdriver = None

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('enterprise_crawler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

ENTERPRISE_KB_DIR = "knowledge_base/enterprise"
os.makedirs(ENTERPRISE_KB_DIR, exist_ok=True)


class EnterpriseSystemsCrawler:
    """Crawls enterprise systems to build unified knowledge graph."""

    def __init__(self):
        self.knowledge_graph = {
            "metadata": {
                "last_updated": datetime.now().isoformat(),
                "version": "1.0",
                "organization": "Jeffco Fibres"
            },
            "systems": {
                "tlw_edi": self.init_tlw_structure(),
                "cleo_lexicom": self.init_cleo_structure(),
                "sage_100": self.init_sage_structure(),
                "ship_exec": self.init_shipexec_structure(),
                "shipstation": self.init_shipstation_structure()
            },
            "integrations": [],
            "data_flows": [],
            "business_processes": []
        }

        # Database config
        self.db_host = os.getenv("DB_HOST", "JEF-SQL.jeffcofibres.local")
        self.db_user = os.getenv("DB_USER", "MAS_REPORTS")
        self.db_password = os.getenv("DB_PASSWORD", "")
        self.db_name = os.getenv("DB_NAME", "MAS_JEF")

    def init_tlw_structure(self) -> Dict[str, Any]:
        """Initialize TLW EDI Translator structure."""
        return {
            "name": "TLW EDI Translator",
            "type": "EDI Processing Engine",
            "vendor": "TLW",
            "purpose": "Translates EDI documents (X12, EDIFACT) to/from internal formats",
            "capabilities": [
                "Parse incoming EDI documents",
                "Validate EDI syntax and business rules",
                "Transform EDI to flat files/XML/JSON",
                "Generate outbound EDI from internal data",
                "Map trading partner-specific formats"
            ],
            "edi_transactions": {
                "inbound": ["850 (Purchase Orders)", "860 (PO Changes)", "846 (Inventory)"],
                "outbound": ["810 (Invoices)", "856 (Ship Notices)", "997 (Acknowledgments)"]
            },
            "configuration": {
                "installation_path": "",
                "data_directories": [],
                "mapping_files": [],
                "trading_partners": []
            },
            "interfaces": []
        }

    def init_cleo_structure(self) -> Dict[str, Any]:
        """Initialize Cleo Lexicom structure."""
        return {
            "name": "Cleo Lexicom",
            "type": "Integration Platform / EDI Gateway",
            "vendor": "Cleo",
            "purpose": "Manages EDI communications and file transfers with trading partners",
            "capabilities": [
                "AS2/SFTP/FTP/FTPS protocols",
                "Trading partner onboarding",
                "Secure file transmission",
                "Connection monitoring",
                "Certificate management",
                "Routing and scheduling"
            ],
            "trading_partners": [],
            "connections": [],
            "mailboxes": [],
            "actions": [],
            "interfaces": []
        }

    def init_sage_structure(self) -> Dict[str, Any]:
        """Initialize Sage 100 ERP structure."""
        return {
            "name": "Sage 100",
            "type": "Enterprise Resource Planning (ERP)",
            "vendor": "Sage",
            "purpose": "Core business management system",
            "modules": [
                "Accounts Receivable",
                "Accounts Payable",
                "General Ledger",
                "Sales Order",
                "Purchase Order",
                "Inventory Management",
                "Bill of Materials",
                "Work Order",
                "Job Cost"
            ],
            "database": {
                "type": "SQL Server",
                "server": self.db_host,
                "database": self.db_name,
                "key_tables": []
            },
            "integrations": [],
            "interfaces": []
        }

    def init_shipexec_structure(self) -> Dict[str, Any]:
        """Initialize ShipExec WMS structure."""
        return {
            "name": "ShipExec",
            "type": "Warehouse Management System (WMS)",
            "vendor": "Aldata / HCL",
            "purpose": "Warehouse operations and inventory control",
            "capabilities": [
                "Receiving",
                "Putaway",
                "Picking",
                "Packing",
                "Shipping",
                "Inventory tracking",
                "Wave management",
                "RF scanning"
            ],
            "integrations": [],
            "interfaces": []
        }

    def init_shipstation_structure(self) -> Dict[str, Any]:
        """Initialize ShipStation structure."""
        return {
            "name": "ShipStation",
            "type": "Multi-Carrier Shipping Platform",
            "vendor": "ShipStation",
            "purpose": "Ship orders via multiple carriers with discounted rates",
            "capabilities": [
                "Multi-carrier rating",
                "Label printing",
                "Tracking",
                "Address validation",
                "Batch processing",
                "Automation rules"
            ],
            "carriers": ["UPS", "FedEx", "USPS", "DHL"],
            "api_endpoint": "https://ssapi.shipstation.com",
            "interfaces": []
        }

    def crawl_all(self) -> Dict[str, Any]:
        """Execute all crawlers."""
        logger.info("Starting enterprise systems crawl...")

        # Crawl each system
        self.crawl_tlw_edi()
        self.crawl_cleo_lexicom()
        self.crawl_sage_100()
        self.crawl_shipexec()
        self.crawl_shipstation()

        # Build integration map
        self.map_system_integrations()
        self.map_data_flows()
        self.map_business_processes()

        # Save knowledge graph
        self.save_knowledge_graph()
        return self.knowledge_graph

    def crawl_tlw_edi(self) -> None:
        """Crawl TLW EDI Translator configuration."""
        logger.info("Crawling TLW EDI Translator...")

        tlw = self.knowledge_graph["systems"]["tlw_edi"]

        # Common TLW installation paths
        possible_paths = [
            r"C:\TLW",
            r"C:\Program Files\TLW",
            r"C:\Program Files (x86)\TLW",
            r"D:\TLW"
        ]

        for path in possible_paths:
            if os.path.exists(path):
                tlw["configuration"]["installation_path"] = path
                logger.info(f"Found TLW at: {path}")

                # Look for data directories
                for root, dirs, files in os.walk(path):
                    if "maps" in root.lower() or "mapping" in root.lower():
                        tlw["configuration"]["mapping_files"].extend([
                            os.path.join(root, f) for f in files
                            if f.endswith(('.map', '.xsl', '.xslt'))
                        ])
                    if "data" in root.lower() or "in" in root.lower() or "out" in root.lower():
                        tlw["configuration"]["data_directories"].append(root)

                break

        # Define standard TLW interfaces
        tlw["interfaces"] = [
            {
                "name": "File Input",
                "type": "Input",
                "format": "EDI X12",
                "source": "Cleo Lexicom",
                "description": "Receives EDI files from trading partners via Cleo"
            },
            {
                "name": "Database Output",
                "type": "Output",
                "format": "SQL",
                "destination": "Sage 100",
                "description": "Writes parsed orders to Sage staging tables"
            },
            {
                "name": "CSV Export",
                "type": "Output",
                "format": "CSV",
                "destination": "ShipExec",
                "description": "Exports order data for WMS import"
            }
        ]

        logger.info(f"TLW: Found {len(tlw['configuration']['mapping_files'])} mapping files")

    def crawl_cleo_lexicom(self) -> None:
        """Crawl Cleo Lexicom configuration."""
        logger.info("Crawling Cleo Lexicom...")

        cleo = self.knowledge_graph["systems"]["cleo_lexicom"]

        # Common Cleo paths
        possible_paths = [
            r"C:\Cleo",
            r"C:\Program Files\Cleo",
            r"C:\Program Files (x86)\Cleo",
            r"D:\Cleo"
        ]

        for path in possible_paths:
            if os.path.exists(path):
                logger.info(f"Found Cleo at: {path}")

                # Look for configuration files
                for root, dirs, files in os.walk(path):
                    for file in files:
                        if file.endswith('.xml') and 'host' in file.lower():
                            cleo["connections"].append({
                                "config_file": os.path.join(root, file),
                                "name": file.replace('.xml', '')
                            })
                        if file.endswith('.xml') and 'partner' in file.lower():
                            cleo["trading_partners"].append({
                                "config_file": os.path.join(root, file),
                                "name": file.replace('.xml', '')
                            })
                break

        # Define standard Cleo interfaces
        cleo["interfaces"] = [
            {
                "name": "AS2 Inbound",
                "type": "Input",
                "protocol": "AS2",
                "source": "Trading Partners",
                "destination": "TLW EDI",
                "description": "Receives EDI via AS2 from trading partners"
            },
            {
                "name": "SFTP Inbound",
                "type": "Input",
                "protocol": "SFTP",
                "source": "Trading Partners",
                "destination": "TLW EDI",
                "description": "Receives EDI via SFTP"
            },
            {
                "name": "File Export",
                "type": "Output",
                "protocol": "AS2/SFTP",
                "source": "TLW EDI",
                "destination": "Trading Partners",
                "description": "Sends outbound EDI to trading partners"
            }
        ]

        logger.info(f"Cleo: Found {len(cleo['connections'])} connections")

    def crawl_sage_100(self) -> None:
        """Crawl Sage 100 database schema."""
        logger.info("Crawling Sage 100 ERP...")

        sage = self.knowledge_graph["systems"]["sage_100"]

        if not pyodbc:
            logger.warning("pyodbc not available, skipping Sage database crawl")
            self.add_sage_standard_config()
            return

        try:
            conn_str = (
                f"DRIVER={{SQL Server}};"
                f"SERVER={self.db_host};"
                f"DATABASE={self.db_name};"
                f"UID={self.db_user};"
                f"PWD={self.db_password}"
            )

            conn = pyodbc.connect(conn_str, timeout=10)
            cursor = conn.cursor()

            # Get key Sage tables
            key_patterns = [
                'SO_%',  # Sales Order
                'PO_%',  # Purchase Order
                'AR_%',  # Accounts Receivable
                'AP_%',  # Accounts Payable
                'IM_%',  # Inventory
                'CI_%',  # Customer
                'SY_%'   # System
            ]

            for pattern in key_patterns:
                cursor.execute(f"""
                    SELECT TABLE_NAME, TABLE_TYPE
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_NAME LIKE ?
                    ORDER BY TABLE_NAME
                """, pattern)

                for row in cursor.fetchall():
                    table_name = row[0]
                    sage["database"]["key_tables"].append({
                        "name": table_name,
                        "module": pattern.split('_')[0],
                        "type": row[1]
                    })

            conn.close()
            logger.info(f"Sage: Found {len(sage['database']['key_tables'])} tables")

        except Exception as e:
            logger.error(f"Sage database crawl error: {e}")
            self.add_sage_standard_config()

        # Define standard Sage interfaces
        sage["interfaces"] = [
            {
                "name": "EDI Order Import",
                "type": "Input",
                "source": "TLW EDI",
                "method": "SQL Insert",
                "tables": ["SO_SalesOrderHeader", "SO_SalesOrderDetail"],
                "description": "Imports EDI 850 orders"
            },
            {
                "name": "Invoice Export",
                "type": "Output",
                "destination": "TLW EDI",
                "method": "SQL Query",
                "tables": ["AR_InvoiceHistoryHeader", "AR_InvoiceHistoryDetail"],
                "description": "Exports invoices for EDI 810"
            },
            {
                "name": "ShipExec Integration",
                "type": "Bi-directional",
                "destination": "ShipExec",
                "method": "CSV/API",
                "description": "Syncs orders and inventory"
            }
        ]

    def add_sage_standard_config(self) -> None:
        """Add standard Sage configuration when DB unavailable."""
        sage = self.knowledge_graph["systems"]["sage_100"]

        # Standard Sage tables
        standard_tables = [
            {"name": "SO_SalesOrderHeader", "module": "SO", "type": "TABLE"},
            {"name": "SO_SalesOrderDetail", "module": "SO", "type": "TABLE"},
            {"name": "AR_Customer", "module": "AR", "type": "TABLE"},
            {"name": "AR_InvoiceHistoryHeader", "module": "AR", "type": "TABLE"},
            {"name": "IM_ItemWarehouse", "module": "IM", "type": "TABLE"},
            {"name": "PO_PurchaseOrderHeader", "module": "PO", "type": "TABLE"}
        ]

        sage["database"]["key_tables"] = standard_tables
        logger.info("Added standard Sage table configuration")

    def crawl_shipexec(self) -> None:
        """Crawl ShipExec WMS."""
        logger.info("Crawling ShipExec WMS...")

        shipexec = self.knowledge_graph["systems"]["ship_exec"]

        # ShipExec interfaces with other systems
        shipexec["interfaces"] = [
            {
                "name": "Order Import",
                "type": "Input",
                "source": "Sage 100",
                "method": "CSV/EDI",
                "description": "Imports sales orders for picking"
            },
            {
                "name": "Shipment Confirmation",
                "type": "Output",
                "destination": "Sage 100",
                "method": "CSV/API",
                "description": "Confirms shipments and updates inventory"
            },
            {
                "name": "Shipping Integration",
                "type": "Output",
                "destination": "ShipStation",
                "method": "API",
                "description": "Sends orders to ShipStation for label creation"
            }
        ]

        logger.info("ShipExec configuration added")

    def crawl_shipstation(self) -> None:
        """Crawl ShipStation configuration."""
        logger.info("Crawling ShipStation...")

        shipstation = self.knowledge_graph["systems"]["shipstation"]

        # ShipStation interfaces
        shipstation["interfaces"] = [
            {
                "name": "Order Import",
                "type": "Input",
                "source": "ShipExec/Sage 100",
                "method": "API",
                "endpoint": "/orders/createorder",
                "description": "Receives orders for shipping"
            },
            {
                "name": "Label Creation",
                "type": "Processing",
                "method": "API",
                "endpoint": "/orders/createlabelfororder",
                "description": "Generates shipping labels"
            },
            {
                "name": "Tracking Export",
                "type": "Output",
                "destination": "Sage 100",
                "method": "Webhook/API",
                "description": "Sends tracking numbers back to ERP"
            }
        ]

        logger.info("ShipStation configuration added")

    def map_system_integrations(self) -> None:
        """Map integration points between systems."""
        logger.info("Mapping system integrations...")

        integrations = [
            {
                "name": "EDI Inbound Processing",
                "systems": ["Cleo Lexicom", "TLW EDI", "Sage 100"],
                "flow": "Cleo receives EDI → TLW translates → Sage imports order",
                "data": "850 Purchase Orders",
                "frequency": "Real-time"
            },
            {
                "name": "EDI Outbound Processing",
                "systems": ["Sage 100", "TLW EDI", "Cleo Lexicom"],
                "flow": "Sage exports data → TLW creates EDI → Cleo transmits",
                "data": "810 Invoices, 856 Ship Notices",
                "frequency": "Scheduled (nightly)"
            },
            {
                "name": "Warehouse Order Flow",
                "systems": ["Sage 100", "ShipExec"],
                "flow": "Sage exports orders → ShipExec processes → ShipExec confirms",
                "data": "Sales Orders, Inventory Updates",
                "frequency": "Real-time/Batch"
            },
            {
                "name": "Shipping Label Creation",
                "systems": ["ShipExec", "ShipStation", "Sage 100"],
                "flow": "ShipExec sends order → ShipStation creates label → Tracking to Sage",
                "data": "Shipping orders, Tracking numbers",
                "frequency": "Real-time"
            }
        ]

        self.knowledge_graph["integrations"] = integrations
        logger.info(f"Mapped {len(integrations)} integrations")

    def map_data_flows(self) -> None:
        """Map data flows through the system landscape."""
        logger.info("Mapping data flows...")

        data_flows = [
            {
                "name": "Customer Order to Shipment",
                "trigger": "Customer places order via EDI",
                "steps": [
                    {"seq": 1, "system": "Cleo Lexicom", "action": "Receive 850 EDI from customer"},
                    {"seq": 2, "system": "TLW EDI", "action": "Parse and validate EDI"},
                    {"seq": 3, "system": "TLW EDI", "action": "Transform to SQL format"},
                    {"seq": 4, "system": "Sage 100", "action": "Create sales order"},
                    {"seq": 5, "system": "Sage 100", "action": "Export order to WMS"},
                    {"seq": 6, "system": "ShipExec", "action": "Allocate and pick order"},
                    {"seq": 7, "system": "ShipExec", "action": "Send to ShipStation"},
                    {"seq": 8, "system": "ShipStation", "action": "Create shipping label"},
                    {"seq": 9, "system": "ShipStation", "action": "Return tracking number"},
                    {"seq": 10, "system": "ShipExec", "action": "Confirm shipment"},
                    {"seq": 11, "system": "Sage 100", "action": "Update order status"},
                    {"seq": 12, "system": "TLW EDI", "action": "Generate 856 ASN"},
                    {"seq": 13, "system": "Cleo Lexicom", "action": "Transmit 856 to customer"}
                ]
            },
            {
                "name": "Invoice Generation and Transmission",
                "trigger": "Order ships",
                "steps": [
                    {"seq": 1, "system": "Sage 100", "action": "Create invoice"},
                    {"seq": 2, "system": "Sage 100", "action": "Export invoice data"},
                    {"seq": 3, "system": "TLW EDI", "action": "Generate 810 EDI"},
                    {"seq": 4, "system": "Cleo Lexicom", "action": "Transmit 810 to customer"},
                    {"seq": 5, "system": "Cleo Lexicom", "action": "Receive 997 acknowledgment"}
                ]
            }
        ]

        self.knowledge_graph["data_flows"] = data_flows
        logger.info(f"Mapped {len(data_flows)} data flows")

    def map_business_processes(self) -> None:
        """Map business processes across systems."""
        logger.info("Mapping business processes...")

        processes = [
            {
                "name": "Order-to-Cash",
                "description": "Complete process from receiving order to collecting payment",
                "systems_involved": ["Cleo Lexicom", "TLW EDI", "Sage 100", "ShipExec", "ShipStation"],
                "key_milestones": [
                    "EDI 850 received",
                    "Order entered in Sage",
                    "Order picked in warehouse",
                    "Shipment created",
                    "Invoice sent (EDI 810)",
                    "Payment received"
                ]
            },
            {
                "name": "Inventory Management",
                "description": "Maintain accurate inventory across systems",
                "systems_involved": ["Sage 100", "ShipExec"],
                "key_milestones": [
                    "Receiving",
                    "Putaway",
                    "Cycle counting",
                    "Adjustments",
                    "Sync to ERP"
                ]
            },
            {
                "name": "EDI Trading Partner Onboarding",
                "description": "Set up new EDI trading partner",
                "systems_involved": ["Cleo Lexicom", "TLW EDI"],
                "key_milestones": [
                    "Gather partner specs",
                    "Configure Cleo connection",
                    "Create TLW maps",
                    "Test transactions",
                    "Go live"
                ]
            }
        ]

        self.knowledge_graph["business_processes"] = processes
        logger.info(f"Mapped {len(processes)} business processes")

    def save_knowledge_graph(self) -> None:
        """Save knowledge graph to file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save timestamped version
        filename = os.path.join(ENTERPRISE_KB_DIR, f"enterprise_knowledge_{timestamp}.json")
        with open(filename, 'w') as f:
            json.dump(self.knowledge_graph, f, indent=2)
        logger.info(f"Saved knowledge graph: {filename}")

        # Save latest version
        latest = os.path.join(ENTERPRISE_KB_DIR, "enterprise_knowledge_latest.json")
        with open(latest, 'w') as f:
            json.dump(self.knowledge_graph, f, indent=2)
        logger.info(f"Saved latest: {latest}")

        # Create summary
        self.create_summary()

    def create_summary(self) -> None:
        """Create human-readable summary."""
        summary_file = os.path.join(ENTERPRISE_KB_DIR, "enterprise_summary.txt")

        with open(summary_file, 'w') as f:
            f.write("=" * 60 + "\n")
            f.write("ENTERPRISE SYSTEMS KNOWLEDGE GRAPH\n")
            f.write("=" * 60 + "\n\n")

            f.write("SYSTEMS:\n")
            for sys_key, sys_data in self.knowledge_graph["systems"].items():
                f.write(f"\n  {sys_data['name']} ({sys_data['type']})\n")
                f.write(f"    Purpose: {sys_data['purpose']}\n")
                if 'interfaces' in sys_data:
                    f.write(f"    Interfaces: {len(sys_data['interfaces'])}\n")

            f.write(f"\n\nINTEGRATIONS: {len(self.knowledge_graph['integrations'])}\n")
            for integ in self.knowledge_graph["integrations"]:
                f.write(f"\n  - {integ['name']}\n")
                f.write(f"    Flow: {integ['flow']}\n")

            f.write(f"\n\nDATA FLOWS: {len(self.knowledge_graph['data_flows'])}\n")
            for flow in self.knowledge_graph["data_flows"]:
                f.write(f"\n  - {flow['name']}\n")
                f.write(f"    Steps: {len(flow['steps'])}\n")

            f.write(f"\n\nBUSINESS PROCESSES: {len(self.knowledge_graph['business_processes'])}\n")
            for proc in self.knowledge_graph["business_processes"]:
                f.write(f"\n  - {proc['name']}\n")
                f.write(f"    Systems: {', '.join(proc['systems_involved'])}\n")

        logger.info(f"Summary saved: {summary_file}")


def main():
    print("=" * 60)
    print("ENTERPRISE SYSTEMS KNOWLEDGE GRAPH CRAWLER")
    print("=" * 60)
    print("\nThis will discover:")
    print("  - TLW EDI Translator configuration")
    print("  - Cleo Lexicom trading partners")
    print("  - Sage 100 ERP schema")
    print("  - ShipExec WMS integration")
    print("  - ShipStation API configuration")
    print("  - System integrations and data flows")
    print()

    crawler = EnterpriseSystemsCrawler()
    crawler.crawl_all()

    print("\n" + "=" * 60)
    print("ENTERPRISE SYSTEMS SUMMARY")
    print("=" * 60)

    kg = crawler.knowledge_graph
    print(f"\nSYSTEMS: {len(kg['systems'])}")
    for sys_key, sys_data in kg["systems"].items():
        print(f"  - {sys_data['name']}")

    print(f"\nINTEGRATIONS: {len(kg['integrations'])}")
    for integ in kg["integrations"]:
        print(f"  - {integ['name']}")

    print(f"\nDATA FLOWS: {len(kg['data_flows'])}")
    print(f"BUSINESS PROCESSES: {len(kg['business_processes'])}")

    print("\n" + "=" * 60)
    print(f"\nKnowledge graph saved to: {ENTERPRISE_KB_DIR}/")
    print("\nNext steps:")
    print("1. Review the generated knowledge graph")
    print("2. Update system-specific details as needed")
    print("3. Run CEO.py to use the knowledge in your bots")


if __name__ == "__main__":
    main()
