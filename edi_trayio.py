"""Selenium session manager for Tray.io UI automation.

Provides a persistent browser session for interacting with Tray.io workflows,
connectors, and execution logs via the web UI.
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import (
        TimeoutException,
        NoSuchElementException,
        WebDriverException,
    )
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

load_dotenv()

logger = logging.getLogger(__name__)

# EDI-related keywords for classifying workflows
EDI_KEYWORDS = [
    "edi", "x12", "edifact", "850", "810", "856", "846", "997",
    "purchase order", "invoice", "asn", "ship notice", "acknowledgment",
    "trading partner", "cleo", "tlw", "as2", "sftp",
]


class TraySession:
    """Manages a persistent Selenium browser session with Tray.io."""

    def __init__(self):
        self.driver: Optional[Any] = None
        self.logged_in = False
        self.username = os.getenv("TRAY_IO_USERNAME", "")
        self.password = os.getenv("TRAY_IO_PASSWORD", "")
        self.base_url = os.getenv("TRAY_IO_URL", "https://app.tray.io")
        self.headless = os.getenv("TRAY_HEADLESS", "false").lower() == "true"

    def _ensure_selenium(self) -> None:
        """Check that Selenium is available."""
        if not SELENIUM_AVAILABLE:
            raise RuntimeError(
                "Selenium is not installed. Install with: pip install selenium"
            )

    def _init_driver(self) -> None:
        """Initialize the Chrome WebDriver if not already open."""
        if self.driver is not None:
            return

        self._ensure_selenium()

        chrome_options = Options()
        if self.headless:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_argument("--disable-dev-shm-usage")

        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.implicitly_wait(10)
        logger.info("Chrome WebDriver initialized")

    def login(self) -> Dict[str, Any]:
        """Log into Tray.io via the auth page."""
        self._init_driver()

        if self.logged_in:
            return {"ok": True, "message": "Already logged in"}

        if not self.username or not self.password:
            return {"ok": False, "error": "TRAY_IO_USERNAME and TRAY_IO_PASSWORD must be set in .env"}

        try:
            logger.info("Logging into Tray.io...")
            self.driver.get("https://auth.tray.io/login")
            time.sleep(3)

            # Wait for and fill login form
            username_field = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.NAME, "email"))
            )
            username_field.clear()
            username_field.send_keys(self.username)

            password_field = self.driver.find_element(By.NAME, "password")
            password_field.clear()
            password_field.send_keys(self.password)

            # Submit
            login_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            login_button.click()
            logger.info("Login form submitted")

            # Wait for redirect to app
            time.sleep(8)
            current_url = self.driver.current_url
            logger.info(f"Post-login URL: {current_url}")

            if "auth.tray.io/login" in current_url:
                self._save_screenshot("trayio_login_failed.png")
                return {"ok": False, "error": "Login failed - still on login page"}

            self.logged_in = True
            return {"ok": True, "message": f"Logged in successfully. Current URL: {current_url}"}

        except TimeoutException:
            self._save_screenshot("trayio_login_timeout.png")
            return {"ok": False, "error": "Login form did not appear within timeout"}
        except Exception as e:
            self._save_screenshot("trayio_login_error.png")
            logger.exception("Login error")
            return {"ok": False, "error": f"Login error: {e}"}

    def _ensure_logged_in(self) -> Dict[str, Any]:
        """Ensure we are logged in, logging in if necessary."""
        if not self.logged_in:
            result = self.login()
            if not result.get("ok"):
                return result
        return {"ok": True}

    def navigate_to_workflows(self) -> Dict[str, Any]:
        """Navigate to the workflows page."""
        auth = self._ensure_logged_in()
        if not auth.get("ok"):
            return auth

        try:
            logger.info("Navigating to workflows...")
            self.driver.get(self.base_url)
            time.sleep(5)
            return {"ok": True, "url": self.driver.current_url}
        except Exception as e:
            self._save_screenshot("trayio_nav_error.png")
            return {"ok": False, "error": f"Navigation error: {e}"}

    def list_workflows(self) -> Dict[str, Any]:
        """Scrape workflow list from the current page."""
        nav = self.navigate_to_workflows()
        if not nav.get("ok"):
            return nav

        try:
            workflows = []

            # Try multiple CSS selectors to find workflow elements
            selectors = [
                "a[href*='workflows/']",
                "[class*='workflow']",
                "[data-test*='workflow']",
                "div[role='button']",
                ".workflow-card",
                "[class*='Workflow']",
                "[class*='workflow-name']",
                "tr[class*='workflow']",
            ]

            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        logger.info(f"Found {len(elements)} elements with '{selector}'")
                        for elem in elements[:50]:
                            try:
                                text = elem.text.strip()
                                href = elem.get_attribute("href") or ""
                                if text and 3 < len(text) < 200:
                                    # Extract workflow ID from URL if available
                                    wf_id = ""
                                    if "/workflows/" in href:
                                        parts = href.split("/workflows/")
                                        if len(parts) > 1:
                                            wf_id = parts[1].split("/")[0].split("?")[0]

                                    # Check if EDI-related
                                    text_lower = text.lower()
                                    edi_related = any(kw in text_lower for kw in EDI_KEYWORDS)

                                    workflows.append({
                                        "id": wf_id,
                                        "name": text,
                                        "url": href,
                                        "status": "active",
                                        "edi_related": edi_related,
                                        "discovered_via": selector,
                                    })
                            except Exception:
                                pass
                except Exception:
                    pass

            # Deduplicate by name
            seen = set()
            unique = []
            for wf in workflows:
                key = wf["name"].lower()
                if key not in seen:
                    seen.add(key)
                    unique.append(wf)

            if unique:
                logger.info(f"Found {len(unique)} unique workflows")
                return {"ok": True, "workflows": unique, "count": len(unique)}
            else:
                self._save_screenshot("trayio_no_workflows.png")
                return {
                    "ok": True,
                    "workflows": [],
                    "count": 0,
                    "message": "No workflows found on page. Screenshot saved.",
                }

        except Exception as e:
            self._save_screenshot("trayio_list_error.png")
            logger.exception("Error listing workflows")
            return {"ok": False, "error": f"Error listing workflows: {e}"}

    def get_workflow_detail(self, workflow_name: str) -> Dict[str, Any]:
        """Click into a workflow and scrape its details."""
        auth = self._ensure_logged_in()
        if not auth.get("ok"):
            return auth

        try:
            # First find the workflow link
            nav = self.navigate_to_workflows()
            if not nav.get("ok"):
                return nav

            # Find and click the workflow
            clicked = False
            for selector in ["a", "div[role='button']", "[class*='workflow']"]:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        if workflow_name.lower() in elem.text.strip().lower():
                            elem.click()
                            clicked = True
                            break
                except Exception:
                    pass
                if clicked:
                    break

            if not clicked:
                return {"ok": False, "error": f"Workflow '{workflow_name}' not found on page"}

            time.sleep(5)

            # Scrape detail page
            detail = {
                "name": workflow_name,
                "url": self.driver.current_url,
                "steps": [],
                "connectors": [],
                "trigger_type": "unknown",
                "description": "",
            }

            # Try to find steps/nodes
            step_selectors = [
                "[class*='step']",
                "[class*='node']",
                "[data-test*='step']",
                "[class*='Step']",
            ]
            for sel in step_selectors:
                try:
                    step_elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for step_elem in step_elements[:30]:
                        step_text = step_elem.text.strip()
                        if step_text and len(step_text) < 200:
                            detail["steps"].append(step_text)
                except Exception:
                    pass

            # Try to find connector badges
            connector_selectors = [
                "[class*='connector']",
                "[class*='Connector']",
                "[data-test*='connector']",
            ]
            for sel in connector_selectors:
                try:
                    conn_elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for conn_elem in conn_elements[:20]:
                        conn_text = conn_elem.text.strip()
                        if conn_text and len(conn_text) < 100:
                            detail["connectors"].append(conn_text)
                except Exception:
                    pass

            # Deduplicate
            detail["steps"] = list(dict.fromkeys(detail["steps"]))
            detail["connectors"] = list(dict.fromkeys(detail["connectors"]))

            return {"ok": True, "detail": detail}

        except Exception as e:
            self._save_screenshot("trayio_detail_error.png")
            logger.exception("Error getting workflow detail")
            return {"ok": False, "error": f"Error getting workflow detail: {e}"}

    def enable_workflow(self, workflow_name: str) -> Dict[str, Any]:
        """Enable a disabled workflow via the UI."""
        return self._toggle_workflow(workflow_name, enable=True)

    def disable_workflow(self, workflow_name: str) -> Dict[str, Any]:
        """Disable an active workflow via the UI."""
        return self._toggle_workflow(workflow_name, enable=False)

    def _toggle_workflow(self, workflow_name: str, enable: bool) -> Dict[str, Any]:
        """Toggle workflow enabled/disabled status."""
        # Navigate to workflow detail first
        detail_result = self.get_workflow_detail(workflow_name)
        if not detail_result.get("ok"):
            return detail_result

        try:
            action = "enable" if enable else "disable"

            # Look for toggle switch, enable/disable button
            toggle_selectors = [
                "[class*='toggle']",
                "[class*='Toggle']",
                "[data-test*='toggle']",
                "button[class*='enable']",
                "button[class*='disable']",
                "[role='switch']",
            ]

            for sel in toggle_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        elem.click()
                        time.sleep(2)
                        logger.info(f"Clicked toggle for {workflow_name}")
                        return {
                            "ok": True,
                            "message": f"Workflow '{workflow_name}' {action}d",
                            "action": action,
                        }
                except Exception:
                    pass

            return {"ok": False, "error": f"Could not find toggle button for '{workflow_name}'"}

        except Exception as e:
            self._save_screenshot(f"trayio_{action}_error.png")
            return {"ok": False, "error": f"Error toggling workflow: {e}"}

    def trigger_workflow(self, workflow_name: str) -> Dict[str, Any]:
        """Manually trigger a workflow run."""
        detail_result = self.get_workflow_detail(workflow_name)
        if not detail_result.get("ok"):
            return detail_result

        try:
            # Look for run/trigger button
            run_selectors = [
                "button[class*='run']",
                "button[class*='Run']",
                "[data-test*='run']",
                "button[class*='trigger']",
                "[data-test*='trigger']",
                "button[title*='Run']",
                "button[aria-label*='Run']",
            ]

            for sel in run_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        if elem.is_displayed() and elem.is_enabled():
                            elem.click()
                            time.sleep(3)
                            logger.info(f"Triggered workflow: {workflow_name}")
                            return {
                                "ok": True,
                                "message": f"Triggered workflow '{workflow_name}'",
                            }
                except Exception:
                    pass

            return {"ok": False, "error": f"Could not find run button for '{workflow_name}'"}

        except Exception as e:
            self._save_screenshot("trayio_trigger_error.png")
            return {"ok": False, "error": f"Error triggering workflow: {e}"}

    def get_workflow_logs(self, workflow_name: str) -> Dict[str, Any]:
        """Get execution logs for a workflow."""
        detail_result = self.get_workflow_detail(workflow_name)
        if not detail_result.get("ok"):
            return detail_result

        try:
            # Look for logs/history tab
            log_selectors = [
                "[class*='log']",
                "[class*='Log']",
                "[class*='history']",
                "[class*='History']",
                "[data-test*='log']",
                "[data-test*='history']",
                "a[href*='log']",
                "a[href*='history']",
                "button[class*='execution']",
            ]

            # Click into logs tab
            for sel in log_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        if elem.is_displayed():
                            elem.click()
                            time.sleep(3)
                            break
                except Exception:
                    pass

            # Scrape log entries
            logs = []
            log_entry_selectors = [
                "[class*='execution']",
                "[class*='Execution']",
                "tr[class*='log']",
                "[class*='log-entry']",
                "[data-test*='execution']",
            ]

            for sel in log_entry_selectors:
                try:
                    entries = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for entry in entries[:20]:
                        text = entry.text.strip()
                        if text and len(text) < 500:
                            # Try to determine status from text/class
                            entry_class = entry.get_attribute("class") or ""
                            status = "unknown"
                            if "success" in entry_class.lower() or "success" in text.lower():
                                status = "success"
                            elif "fail" in entry_class.lower() or "error" in text.lower():
                                status = "failed"
                            elif "running" in entry_class.lower() or "running" in text.lower():
                                status = "running"

                            logs.append({
                                "workflow_name": workflow_name,
                                "text": text,
                                "status": status,
                            })
                except Exception:
                    pass

            return {
                "ok": True,
                "workflow_name": workflow_name,
                "logs": logs,
                "count": len(logs),
            }

        except Exception as e:
            self._save_screenshot("trayio_logs_error.png")
            return {"ok": False, "error": f"Error getting workflow logs: {e}"}

    def create_workflow(self, name: str, trigger_type: str = "manual") -> Dict[str, Any]:
        """Create a new workflow via the UI."""
        auth = self._ensure_logged_in()
        if not auth.get("ok"):
            return auth

        try:
            nav = self.navigate_to_workflows()
            if not nav.get("ok"):
                return nav

            # Look for "Create" or "New workflow" button
            create_selectors = [
                "button[class*='create']",
                "button[class*='Create']",
                "[data-test*='create']",
                "button[class*='new']",
                "button[class*='New']",
                "[data-test*='new-workflow']",
                "button[aria-label*='Create']",
                "button[aria-label*='New']",
            ]

            clicked = False
            for sel in create_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        if elem.is_displayed() and elem.is_enabled():
                            elem.click()
                            clicked = True
                            time.sleep(3)
                            break
                except Exception:
                    pass
                if clicked:
                    break

            if not clicked:
                return {"ok": False, "error": "Could not find 'Create workflow' button"}

            # Try to fill in workflow name
            name_selectors = [
                "input[placeholder*='name']",
                "input[placeholder*='Name']",
                "input[name*='name']",
                "input[data-test*='name']",
                "input[type='text']",
            ]

            named = False
            for sel in name_selectors:
                try:
                    input_elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if input_elem.is_displayed():
                        input_elem.clear()
                        input_elem.send_keys(name)
                        named = True
                        break
                except Exception:
                    pass

            if not named:
                self._save_screenshot("trayio_create_no_name_field.png")
                return {"ok": False, "error": "Could not find workflow name input field"}

            # Try to select trigger type if available
            trigger_selectors = [
                f"[data-test*='{trigger_type}']",
                f"button[class*='{trigger_type}']",
                f"[class*='{trigger_type}']",
            ]
            for sel in trigger_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        if elem.is_displayed():
                            elem.click()
                            time.sleep(1)
                            break
                except Exception:
                    pass

            # Click create/save/confirm button
            confirm_selectors = [
                "button[class*='confirm']",
                "button[class*='save']",
                "button[class*='create']",
                "button[type='submit']",
                "[data-test*='confirm']",
                "[data-test*='save']",
            ]

            for sel in confirm_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        if elem.is_displayed() and elem.is_enabled():
                            elem.click()
                            time.sleep(3)
                            logger.info(f"Created workflow: {name}")
                            return {
                                "ok": True,
                                "message": f"Created workflow '{name}' with trigger type '{trigger_type}'",
                                "url": self.driver.current_url,
                            }
                except Exception:
                    pass

            self._save_screenshot("trayio_create_no_confirm.png")
            return {"ok": False, "error": "Could not find confirm/save button after naming workflow"}

        except Exception as e:
            self._save_screenshot("trayio_create_error.png")
            logger.exception("Error creating workflow")
            return {"ok": False, "error": f"Error creating workflow: {e}"}

    def take_screenshot(self, filename: str = "trayio_screenshot.png") -> Dict[str, Any]:
        """Take a screenshot of the current browser state."""
        if not self.driver:
            return {"ok": False, "error": "No browser session active"}

        try:
            self.driver.save_screenshot(filename)
            return {"ok": True, "message": f"Screenshot saved to {filename}"}
        except Exception as e:
            return {"ok": False, "error": f"Screenshot error: {e}"}

    def _save_screenshot(self, filename: str) -> None:
        """Save a debug screenshot silently."""
        try:
            if self.driver:
                self.driver.save_screenshot(filename)
                logger.info(f"Debug screenshot saved: {filename}")
        except Exception:
            pass

    def close(self) -> None:
        """Close the browser session."""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("Browser session closed")
            except Exception:
                pass
            finally:
                self.driver = None
                self.logged_in = False
