#!/usr/bin/env python3
"""
Tray.io Knowledge Graph Crawler

Discovers and maps EDI workflows, integrations, and automation patterns.

This crawler:
1. Connects to Tray.io API
2. Discovers all workflows/integrations
3. Maps workflow dependencies and triggers
4. Documents API endpoints and connectors
5. Builds a knowledge graph for ChromeBot

Can also use Selenium to crawl the UI if API access is limited.
"""

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

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
        logging.FileHandler('trayio_crawler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TRAYIO_KB_DIR = "knowledge_base/trayio"
os.makedirs(TRAYIO_KB_DIR, exist_ok=True)


class TrayIoCrawler:
    """Crawls Tray.io to build knowledge graph."""

    def __init__(self):
        self.knowledge_graph = {
            "metadata": {
                "last_updated": datetime.now().isoformat(),
                "version": "1.0",
                "source": "tray.io"
            },
            "workflows": [],
            "connectors": [],
            "api_endpoints": [],
            "edi_patterns": [],
            "triggers": [],
            "actions": [],
            "integrations": {}
        }
        self.api_token = os.getenv("TRAY_IO_API_TOKEN", "")
        self.username = os.getenv("TRAY_IO_USERNAME", "")
        self.password = os.getenv("TRAY_IO_PASSWORD", "")
        self.base_url = os.getenv("TRAY_IO_URL", "https://app.tray.io")

    def crawl_all(self) -> Dict[str, Any]:
        """Execute all crawlers."""
        logger.info("Starting Tray.io knowledge graph crawl...")

        # Try API first, fall back to UI scraping
        if self.api_token and requests:
            logger.info("Using Tray.io API...")
            self.crawl_via_api()
        elif self.username and self.password and webdriver:
            logger.info("Using Selenium UI crawler...")
            self.crawl_via_selenium()
        else:
            logger.warning("No credentials or dependencies for Tray.io crawling")
            logger.warning("Set TRAY_IO_API_TOKEN or TRAY_IO_USERNAME/PASSWORD in .env")
            logger.warning("Or install: pip install requests selenium")
            self.create_manual_config_template()

        self.extract_edi_patterns()
        self.save_knowledge_graph()
        return self.knowledge_graph

    def crawl_via_api(self) -> None:
        """Crawl Tray.io via API."""
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

        try:
            # Get all workflows
            logger.info("Fetching workflows...")
            response = requests.get(
                f"{self.base_url}/api/v1/workflows",
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                workflows = response.json().get("data", [])
                logger.info(f"Found {len(workflows)} workflows")

                for workflow in workflows:
                    self.process_workflow(workflow)

            # Get connectors
            logger.info("Fetching connectors...")
            response = requests.get(
                f"{self.base_url}/api/v1/connectors",
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                connectors = response.json().get("data", [])
                logger.info(f"Found {len(connectors)} connectors")
                self.knowledge_graph["connectors"] = connectors

        except Exception as e:
            logger.error(f"API crawl error: {e}")
            logger.info("Falling back to manual configuration...")
            self.create_manual_config_template()

    def crawl_via_selenium(self) -> None:
        """Crawl Tray.io using Selenium (UI automation)."""
        if not webdriver:
            logger.error("Selenium not installed. Install with: pip install selenium")
            return

        logger.info("Starting Selenium crawler...")

        chrome_options = Options()
        # Don't use headless to see what's happening
        # chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--start-maximized")

        driver = None
        try:
            driver = webdriver.Chrome(options=chrome_options)
            driver.implicitly_wait(10)

            # Login - use actual Tray.io auth URL
            logger.info("Logging into Tray.io...")
            login_url = "https://auth.tray.io/login"
            driver.get(login_url)
            time.sleep(3)

            # Wait for login form
            try:
                username_field = WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.NAME, "email"))
                )
                username_field.send_keys(self.username)
                logger.info("Entered username")

                password_field = driver.find_element(By.NAME, "password")
                password_field.send_keys(self.password)
                logger.info("Entered password")

                # Submit
                login_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
                login_button.click()
                logger.info("Clicked login")

                # Wait for redirect
                time.sleep(8)
                logger.info(f"Current URL after login: {driver.current_url}")

            except Exception as login_error:
                logger.error(f"Login error: {login_error}")
                driver.save_screenshot("trayio_login_error.png")
                logger.info("Screenshot saved to trayio_login_error.png")
                raise

            # Navigate to workflows URL from .env
            logger.info("Navigating to workflows...")
            driver.get(self.base_url)
            time.sleep(5)

            # Get all workflow links
            logger.info("Scanning for workflows...")
            workflows_found = []

            # Try multiple selectors
            selectors = [
                "a[href*='workflows/']",
                "[class*='workflow']",
                "[data-test*='workflow']",
                "div[role='button']",
                ".workflow-card",
                "[class*='Workflow']"
            ]

            for selector in selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        logger.info(f"Found {len(elements)} with '{selector}'")
                        for elem in elements[:30]:
                            try:
                                text = elem.text.strip()
                                href = elem.get_attribute("href") or ""
                                if text and len(text) > 3 and len(text) < 200:
                                    workflows_found.append({
                                        "name": text,
                                        "url": href,
                                        "status": "active",
                                        "discovered_via": "selenium"
                                    })
                            except:
                                pass
                except:
                    pass

            if workflows_found:
                logger.info(f"Successfully scraped {len(workflows_found)} workflows")
                self.knowledge_graph["workflows"].extend(workflows_found)
            else:
                logger.warning("No workflows found on page")
                with open("trayio_page_source.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                logger.info("Page source saved for debugging")
                self.create_manual_config_template()

            logger.info("Selenium crawl complete")

        except Exception as e:
            logger.error(f"Selenium crawl error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self.create_manual_config_template()

        finally:
            if driver:
                driver.quit()

    def process_workflow(self, workflow: Dict[str, Any]) -> None:
        """Process a discovered workflow."""
        workflow_info = {
            "id": workflow.get("id"),
            "name": workflow.get("name"),
            "description": workflow.get("description", ""),
            "status": workflow.get("status", "unknown"),
            "tags": workflow.get("tags", []),
            "trigger_type": workflow.get("trigger", {}).get("type", "unknown"),
            "steps": [],
            "connectors_used": [],
            "edi_related": self.is_edi_workflow(workflow)
        }

        # Extract steps
        steps = workflow.get("steps", [])
        for step in steps:
            workflow_info["steps"].append({
                "name": step.get("name"),
                "type": step.get("type"),
                "connector": step.get("connector", {}).get("name")
            })

            connector_name = step.get("connector", {}).get("name")
            if connector_name and connector_name not in workflow_info["connectors_used"]:
                workflow_info["connectors_used"].append(connector_name)

        self.knowledge_graph["workflows"].append(workflow_info)

        # Extract trigger info
        if workflow.get("trigger"):
            self.knowledge_graph["triggers"].append({
                "workflow_id": workflow.get("id"),
                "workflow_name": workflow.get("name"),
                "type": workflow.get("trigger", {}).get("type"),
                "schedule": workflow.get("trigger", {}).get("schedule")
            })

    def is_edi_workflow(self, workflow: Dict[str, Any]) -> bool:
        """Determine if workflow is EDI-related."""
        name = workflow.get("name", "").lower()
        desc = workflow.get("description", "").lower()
        tags = [t.lower() for t in workflow.get("tags", [])]

        edi_keywords = ["edi", "x12", "edifact", "850", "810", "856", "order", "invoice", "shipment", "asn"]

        return any(keyword in name or keyword in desc or keyword in tags for keyword in edi_keywords)

    def extract_edi_patterns(self) -> None:
        """Extract common EDI patterns from workflows."""
        logger.info("Extracting EDI patterns...")

        edi_workflows = [w for w in self.knowledge_graph["workflows"] if w.get("edi_related")]

        patterns = {
            "inbound_orders": [],
            "outbound_invoices": [],
            "shipment_notifications": [],
            "inventory_updates": [],
            "acknowledgments": []
        }

        for workflow in edi_workflows:
            name = workflow["name"].lower()

            if any(k in name for k in ["850", "purchase order", "po", "order"]):
                patterns["inbound_orders"].append(workflow["name"])
            elif any(k in name for k in ["810", "invoice", "billing"]):
                patterns["outbound_invoices"].append(workflow["name"])
            elif any(k in name for k in ["856", "asn", "shipment", "ship notice"]):
                patterns["shipment_notifications"].append(workflow["name"])
            elif any(k in name for k in ["846", "inventory", "stock"]):
                patterns["inventory_updates"].append(workflow["name"])
            elif any(k in name for k in ["997", "ack", "acknowledgment"]):
                patterns["acknowledgments"].append(workflow["name"])

        self.knowledge_graph["edi_patterns"] = patterns
        logger.info(f"Identified EDI patterns: {sum(len(v) for v in patterns.values())} workflows")

    def create_manual_config_template(self) -> None:
        """Create a manual configuration template for users to fill in."""
        logger.info("Creating manual configuration template...")

        template = {
            "metadata": {
                "instructions": "Fill in your Tray.io workflows manually",
                "last_updated": datetime.now().isoformat()
            },
            "workflows": [
                {
                    "name": "Example: 850 Purchase Order Processing",
                    "description": "Receives 850 EDI files and creates orders",
                    "trigger_type": "webhook",
                    "schedule": "on demand",
                    "status": "enabled",
                    "edi_type": "850 - Purchase Order",
                    "steps": [
                        "Receive EDI file",
                        "Parse EDI",
                        "Validate data",
                        "Create order in ERP",
                        "Send 997 acknowledgment"
                    ]
                },
                {
                    "name": "Example: 810 Invoice Generation",
                    "description": "Generates 810 EDI invoices from shipped orders",
                    "trigger_type": "schedule",
                    "schedule": "daily at 6pm",
                    "status": "enabled",
                    "edi_type": "810 - Invoice",
                    "steps": [
                        "Query shipped orders",
                        "Format as 810 EDI",
                        "Send to trading partner",
                        "Log transaction"
                    ]
                }
            ],
            "connectors": [
                "HTTP/Webhook",
                "SFTP",
                "Database",
                "Email",
                "Custom API"
            ],
            "edi_patterns": {
                "inbound_orders": ["850 Purchase Order Processing"],
                "outbound_invoices": ["810 Invoice Generation"],
                "shipment_notifications": [],
                "inventory_updates": [],
                "acknowledgments": []
            }
        }

        template_file = os.path.join(TRAYIO_KB_DIR, "workflows_template.json")
        with open(template_file, 'w') as f:
            json.dump(template, f, indent=2)

        logger.info(f"Template created: {template_file}")
        logger.info("Edit this file with your actual workflows, then re-run the trainer")

        # Also use this as our knowledge if no real data
        if not self.knowledge_graph["workflows"]:
            self.knowledge_graph.update(template)

    def save_knowledge_graph(self) -> None:
        """Save knowledge graph to disk."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save timestamped version
        kb_file = os.path.join(TRAYIO_KB_DIR, f"trayio_knowledge_{timestamp}.json")
        with open(kb_file, 'w') as f:
            json.dump(self.knowledge_graph, f, indent=2)

        # Save latest version
        latest_file = os.path.join(TRAYIO_KB_DIR, "trayio_knowledge_latest.json")
        with open(latest_file, 'w') as f:
            json.dump(self.knowledge_graph, f, indent=2)

        logger.info(f"Tray.io knowledge graph saved to {kb_file}")

        # Generate summary
        self.generate_summary()

    def generate_summary(self) -> None:
        """Generate human-readable summary."""
        summary = []
        summary.append("=" * 60)
        summary.append("TRAY.IO KNOWLEDGE GRAPH SUMMARY")
        summary.append("=" * 60)
        summary.append("")

        # Workflows
        summary.append("WORKFLOWS:")
        summary.append(f"  Total: {len(self.knowledge_graph['workflows'])}")
        edi_count = sum(1 for w in self.knowledge_graph['workflows'] if w.get('edi_related'))
        summary.append(f"  EDI-related: {edi_count}")
        summary.append("")

        # List workflows
        if self.knowledge_graph['workflows']:
            summary.append("  Top workflows:")
            for workflow in self.knowledge_graph['workflows'][:10]:
                status = workflow.get('status', 'unknown')
                summary.append(f"    - {workflow['name']} ({status})")
        summary.append("")

        # EDI Patterns
        summary.append("EDI PATTERNS:")
        for pattern_name, workflows in self.knowledge_graph.get('edi_patterns', {}).items():
            if workflows:
                summary.append(f"  {pattern_name.replace('_', ' ').title()}: {len(workflows)}")
        summary.append("")

        # Connectors
        summary.append("CONNECTORS:")
        connectors = self.knowledge_graph.get('connectors', [])
        if isinstance(connectors, list):
            for connector in connectors[:10]:
                if isinstance(connector, dict):
                    summary.append(f"  - {connector.get('name', 'Unknown')}")
                else:
                    summary.append(f"  - {connector}")
        summary.append("")

        summary.append("=" * 60)

        summary_text = "\n".join(summary)
        print(summary_text)

        # Save summary
        summary_file = os.path.join(TRAYIO_KB_DIR, "trayio_summary.txt")
        with open(summary_file, 'w') as f:
            f.write(summary_text)


def main():
    """Run the Tray.io crawler."""
    print("Starting Tray.io Knowledge Graph Crawler...")
    print("")
    print("This will discover:")
    print("  - EDI workflows and integrations")
    print("  - Workflow triggers and schedules")
    print("  - Connectors and API endpoints")
    print("  - EDI patterns (850, 810, 856, etc.)")
    print("")

    crawler = TrayIoCrawler()
    knowledge = crawler.crawl_all()

    print("\nCrawl complete!")
    print(f"Knowledge graph saved to: {TRAYIO_KB_DIR}/")
    print("")
    print("Next steps:")
    print("1. Review the generated knowledge graph")
    print("2. If template was created, fill in your actual workflows")
    print("3. Run 'python knowledge_trainer.py' to train ChromeBot")


if __name__ == "__main__":
    main()
