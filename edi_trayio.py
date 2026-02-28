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

    # ------------------------------------------------------------------
    # UI Helper methods for resilient interactions
    # ------------------------------------------------------------------

    def _click_with_retry(
        self, selectors: List[str], description: str = "", max_retries: int = 3
    ) -> Optional[Any]:
        """Try multiple CSS selectors with retries until one clicks successfully.

        Returns the clicked element or None.
        """
        for attempt in range(max_retries):
            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        if elem.is_displayed() and elem.is_enabled():
                            elem.click()
                            logger.info(f"Clicked {description or selector} (attempt {attempt+1})")
                            return elem
                except Exception:
                    pass
            time.sleep(1)

        logger.warning(f"Could not click {description}: tried {selectors}")
        return None

    def _wait_and_click(self, selector: str, timeout: int = 10, description: str = "") -> bool:
        """Explicit wait for element, then click."""
        try:
            elem = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            elem.click()
            logger.info(f"Clicked {description or selector}")
            return True
        except (TimeoutException, WebDriverException) as e:
            logger.warning(f"wait_and_click failed for {description or selector}: {e}")
            return False

    def _fill_field(self, selector: str, value: str, timeout: int = 10) -> bool:
        """Clear and fill a form field."""
        try:
            elem = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            elem.clear()
            elem.send_keys(value)
            return True
        except (TimeoutException, WebDriverException) as e:
            logger.warning(f"fill_field failed for {selector}: {e}")
            return False

    def _search_and_select(
        self, search_selector: str, query: str, result_selector: str, timeout: int = 10
    ) -> bool:
        """Type into a search field and click the first matching result."""
        if not self._fill_field(search_selector, query, timeout):
            return False
        time.sleep(2)  # Wait for search results to populate
        try:
            results = self.driver.find_elements(By.CSS_SELECTOR, result_selector)
            for result in results:
                if result.is_displayed():
                    result_text = result.text.strip().lower()
                    if query.lower() in result_text or result_text in query.lower():
                        result.click()
                        logger.info(f"Selected search result: {result.text.strip()}")
                        return True
            # If no text match, just click the first visible result
            for result in results:
                if result.is_displayed():
                    result.click()
                    logger.info(f"Selected first visible result: {result.text.strip()}")
                    return True
        except Exception as e:
            logger.warning(f"search_and_select failed: {e}")
        return False

    def _find_element_by_text(self, tag: str, text: str, partial: bool = True) -> Optional[Any]:
        """Find an element by its visible text content."""
        try:
            elements = self.driver.find_elements(By.TAG_NAME, tag)
            for elem in elements:
                elem_text = elem.text.strip()
                if partial:
                    if text.lower() in elem_text.lower():
                        return elem
                else:
                    if text.lower() == elem_text.lower():
                        return elem
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Workflow builder methods
    # ------------------------------------------------------------------

    def add_workflow_step(
        self, connector_name: str, operation: str, step_name: str
    ) -> Dict[str, Any]:
        """In the workflow editor, add a new step by searching for a connector.

        1. Click the "+" / "Add Step" button on the canvas
        2. Search for the connector in the step picker dialog
        3. Select the specific operation
        4. Rename the step node to step_name
        """
        auth = self._ensure_logged_in()
        if not auth.get("ok"):
            return auth

        try:
            # Step 1: Click "Add Step" / "+" button
            add_step_btn = self._click_with_retry(
                [
                    "button[data-test*='add-step']",
                    "button[aria-label*='Add']",
                    "button[class*='add-step']",
                    "button[class*='AddStep']",
                    "[data-test*='add-connector']",
                    "button[class*='add']",
                    ".add-step-button",
                    "button[title*='Add']",
                ],
                description="Add Step button",
            )
            if not add_step_btn:
                # Try finding "+" text button
                plus_btn = self._find_element_by_text("button", "+")
                if plus_btn:
                    plus_btn.click()
                else:
                    self._save_screenshot("trayio_add_step_not_found.png")
                    return {"ok": False, "error": "Could not find 'Add Step' button"}
            time.sleep(2)

            # Step 2: Search for the connector
            searched = self._search_and_select(
                search_selector="input[placeholder*='Search'], input[type='search'], input[class*='search']",
                query=connector_name,
                result_selector="[class*='connector-item'], [class*='step-item'], [data-test*='connector'], li[class*='result'], div[role='option']",
                timeout=10,
            )
            if not searched:
                # Fallback: try clicking text matching connector_name
                conn_elem = self._find_element_by_text("div", connector_name)
                if conn_elem:
                    conn_elem.click()
                    searched = True
                else:
                    self._save_screenshot("trayio_connector_search_failed.png")
                    return {
                        "ok": False,
                        "error": f"Could not find connector '{connector_name}' in step picker",
                    }
            time.sleep(2)

            # Step 3: Select the operation
            if operation:
                op_elem = self._find_element_by_text("div", operation) or \
                          self._find_element_by_text("button", operation) or \
                          self._find_element_by_text("span", operation) or \
                          self._find_element_by_text("li", operation)
                if op_elem:
                    op_elem.click()
                    time.sleep(1)
                else:
                    logger.warning(f"Could not find operation '{operation}', connector may auto-select default")

            time.sleep(2)

            # Step 4: Try to rename the step
            # Look for the step name label/input and rename it
            rename_selectors = [
                "input[class*='step-name']",
                "input[class*='node-name']",
                "[contenteditable='true']",
                "input[data-test*='step-name']",
            ]
            for sel in rename_selectors:
                try:
                    name_elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if name_elem.is_displayed():
                        name_elem.clear()
                        name_elem.send_keys(step_name)
                        logger.info(f"Renamed step to: {step_name}")
                        break
                except Exception:
                    pass

            return {
                "ok": True,
                "message": f"Added step '{step_name}' ({connector_name} → {operation})",
                "step_name": step_name,
                "connector": connector_name,
                "operation": operation,
            }

        except Exception as e:
            self._save_screenshot("trayio_add_step_error.png")
            logger.exception("Error adding workflow step")
            return {"ok": False, "error": f"Error adding step: {e}"}

    def configure_step(self, step_name: str, config: Dict[str, str]) -> Dict[str, Any]:
        """Click a step node and fill in its configuration fields.

        Args:
            step_name: Display name of the step to configure.
            config: Key-value pairs to fill into the config panel.
        """
        try:
            # Click the step node to open its config
            step_elem = self._find_element_by_text("div", step_name) or \
                        self._find_element_by_text("span", step_name)
            if not step_elem:
                return {"ok": False, "error": f"Could not find step '{step_name}' on canvas"}

            step_elem.click()
            time.sleep(2)

            # Fill in config fields
            fields_set = 0
            for key, value in config.items():
                # Try to find the field by label, placeholder, or name
                field_selectors = [
                    f"input[name='{key}']",
                    f"input[placeholder*='{key}']",
                    f"textarea[name='{key}']",
                    f"textarea[placeholder*='{key}']",
                    f"input[data-test*='{key}']",
                    f"select[name='{key}']",
                ]

                filled = False
                for sel in field_selectors:
                    try:
                        field_elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                        if field_elem.is_displayed():
                            if field_elem.tag_name == "select":
                                from selenium.webdriver.support.ui import Select
                                Select(field_elem).select_by_visible_text(value)
                            else:
                                field_elem.clear()
                                field_elem.send_keys(value)
                            filled = True
                            fields_set += 1
                            break
                    except Exception:
                        pass

                # Fallback: try finding label text and clicking adjacent input
                if not filled:
                    label_elem = self._find_element_by_text("label", key)
                    if label_elem:
                        try:
                            label_for = label_elem.get_attribute("for")
                            if label_for:
                                input_elem = self.driver.find_element(By.ID, label_for)
                                input_elem.clear()
                                input_elem.send_keys(value)
                                fields_set += 1
                                filled = True
                        except Exception:
                            pass

                if not filled:
                    logger.warning(f"Could not find config field '{key}' for step '{step_name}'")

            # Close config panel / save
            save_btn = self._click_with_retry(
                [
                    "button[class*='save']",
                    "button[class*='done']",
                    "button[class*='close']",
                    "button[data-test*='save']",
                    "button[aria-label*='Close']",
                ],
                description="Save/Close config",
            )

            return {
                "ok": True,
                "message": f"Configured step '{step_name}': {fields_set}/{len(config)} fields set",
                "fields_set": fields_set,
                "fields_total": len(config),
            }

        except Exception as e:
            self._save_screenshot("trayio_configure_step_error.png")
            logger.exception("Error configuring step")
            return {"ok": False, "error": f"Error configuring step: {e}"}

    def get_workflow_editor_state(self) -> Dict[str, Any]:
        """Scrape the current workflow editor canvas to see what steps exist."""
        try:
            steps = []

            # Try to find step/node elements on the canvas
            node_selectors = [
                "[class*='node']",
                "[class*='step']",
                "[class*='Step']",
                "[data-test*='node']",
                "[data-test*='step']",
                "[class*='canvas'] [class*='item']",
            ]

            for sel in node_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for elem in elements:
                        text = elem.text.strip()
                        if text and 2 < len(text) < 200:
                            steps.append({
                                "name": text,
                                "selector": sel,
                            })
                except Exception:
                    pass

            # Deduplicate
            seen = set()
            unique_steps = []
            for step in steps:
                key = step["name"].lower()
                if key not in seen:
                    seen.add(key)
                    unique_steps.append(step)

            return {
                "ok": True,
                "steps": unique_steps,
                "step_count": len(unique_steps),
                "url": self.driver.current_url,
            }

        except Exception as e:
            self._save_screenshot("trayio_editor_state_error.png")
            return {"ok": False, "error": f"Error reading editor state: {e}"}

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
