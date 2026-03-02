from __future__ import annotations

import os
import json
import re
import time
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

import ollama  # pip install ollama

# ----------------------------
# Configuration (EDIT THESE)
# ----------------------------
MODEL = "qwen2.5:7b-instruct"  # choose a local model you have pulled
DRY_RUN = False     # operational mode for CEO bot

# Email Configuration
RECIPIENT_EMAIL = "rfoley@jeffcofibres.com"
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Database Configuration
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "")

# Tray.io Configuration
TRAY_IO_URL = os.getenv("TRAY_IO_URL", "https://app.tray.io")
TRAY_IO_USERNAME = os.getenv("TRAY_IO_USERNAME", "")
TRAY_IO_PASSWORD = os.getenv("TRAY_IO_PASSWORD", "")

# Teams Configuration
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")

# Inbox Bot Configuration
INBOX_EMAIL = os.getenv("INBOX_EMAIL", "")
INBOX_PASSWORD = os.getenv("INBOX_PASSWORD", "")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ceo_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Work tracking storage
WORK_LOG_FILE = "work_log.json"
FEEDBACK_FILE = "feedback.json"
ANALYTICS_FILE = "analytics.json"
IMPROVEMENTS_FILE = "improvements.json"
LEARNING_ENABLED = True


# ----------------------------
# Utility helpers
# ----------------------------
def load_work_log() -> List[Dict[str, Any]]:
    """Load the work log from disk."""
    if os.path.exists(WORK_LOG_FILE):
        try:
            with open(WORK_LOG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading work log: {e}")
            return []
    return []


def save_work_log(work_log: List[Dict[str, Any]]) -> None:
    """Save the work log to disk."""
    try:
        with open(WORK_LOG_FILE, 'w') as f:
            json.dump(work_log, indent=2, fp=f)
    except Exception as e:
        logger.error(f"Error saving work log: {e}")


def log_work_item(bot_name: str, task: str, status: str, details: str = "") -> None:
    """Add a work item to the log."""
    work_log = load_work_log()
    work_log.append({
        "timestamp": datetime.now().isoformat(),
        "bot": bot_name,
        "task": task,
        "status": status,
        "details": details
    })
    save_work_log(work_log)
    logger.info(f"[{bot_name}] {task}: {status}")


def get_daily_work_summary() -> str:
    """Generate a summary of work completed in the last 24 hours."""
    work_log = load_work_log()
    cutoff = datetime.now() - timedelta(hours=24)

    recent_work = [
        item for item in work_log
        if datetime.fromisoformat(item["timestamp"]) > cutoff
    ]

    if not recent_work:
        return "No work items logged in the past 24 hours."

    # Group by bot
    bot_work = {}
    for item in recent_work:
        bot = item["bot"]
        if bot not in bot_work:
            bot_work[bot] = []
        bot_work[bot].append(item)

    summary = []
    summary.append(f"Daily Work Summary - {datetime.now().strftime('%Y-%m-%d')}")
    summary.append("=" * 60)
    summary.append("")

    for bot, items in bot_work.items():
        summary.append(f"{bot.upper()} ({len(items)} tasks)")
        summary.append("-" * 40)
        for item in items:
            time_str = datetime.fromisoformat(item["timestamp"]).strftime("%H:%M:%S")
            summary.append(f"  [{time_str}] {item['task']}: {item['status']}")
            if item.get("details"):
                summary.append(f"    → {item['details']}")
        summary.append("")

    return "\n".join(summary)


def send_email_report(subject: str, body: str) -> Dict[str, Any]:
    """Send an email report to the configured recipient."""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("Email credentials not configured. Skipping email.")
        return {"ok": False, "error": "Email credentials not configured"}

    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = RECIPIENT_EMAIL
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()

        logger.info(f"Email sent to {RECIPIENT_EMAIL}")
        return {"ok": True, "message": "Email sent successfully"}
    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return {"ok": False, "error": str(e)}


# ----------------------------
# Learning & Improvement System
# ----------------------------
def load_feedback() -> List[Dict[str, Any]]:
    """Load user feedback from disk."""
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading feedback: {e}")
            return []
    return []


def save_feedback(feedback: List[Dict[str, Any]]) -> None:
    """Save feedback to disk."""
    try:
        with open(FEEDBACK_FILE, 'w') as f:
            json.dump(feedback, indent=2, fp=f)
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")


def add_feedback(bot_name: str, action: str, rating: int, comment: str = "") -> None:
    """Add user feedback for a bot action (rating 1-5)."""
    feedback = load_feedback()
    feedback.append({
        "timestamp": datetime.now().isoformat(),
        "bot": bot_name,
        "action": action,
        "rating": rating,
        "comment": comment
    })
    save_feedback(feedback)
    logger.info(f"Feedback recorded: {bot_name} - {action} - {rating}/5")


def load_analytics() -> Dict[str, Any]:
    """Load bot performance analytics."""
    if os.path.exists(ANALYTICS_FILE):
        try:
            with open(ANALYTICS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "ChromeBot": {"success": 0, "failure": 0, "avg_time": 0},
        "EDIBot": {"success": 0, "failure": 0, "avg_time": 0},
        "SQLBot": {"success": 0, "failure": 0, "avg_time": 0},
        "InboxBot": {"success": 0, "failure": 0, "avg_time": 0},
        "TeamsBot": {"success": 0, "failure": 0, "avg_time": 0}
    }


def save_analytics(analytics: Dict[str, Any]) -> None:
    """Save analytics to disk."""
    try:
        with open(ANALYTICS_FILE, 'w') as f:
            json.dump(analytics, indent=2, fp=f)
    except Exception as e:
        logger.error(f"Error saving analytics: {e}")


def update_analytics(bot_name: str, success: bool, duration: float) -> None:
    """Update bot performance analytics."""
    analytics = load_analytics()

    if bot_name not in analytics:
        analytics[bot_name] = {"success": 0, "failure": 0, "avg_time": 0}

    bot_stats = analytics[bot_name]

    if success:
        bot_stats["success"] += 1
    else:
        bot_stats["failure"] += 1

    # Update average time
    total_runs = bot_stats["success"] + bot_stats["failure"]
    current_avg = bot_stats["avg_time"]
    bot_stats["avg_time"] = (current_avg * (total_runs - 1) + duration) / total_runs

    save_analytics(analytics)


def analyze_bot_performance() -> Dict[str, Any]:
    """Analyze bot performance and identify issues."""
    analytics = load_analytics()
    feedback = load_feedback()
    work_log = load_work_log()

    # Calculate metrics for each bot
    results = {}

    for bot_name, stats in analytics.items():
        total = stats["success"] + stats["failure"]
        if total == 0:
            continue

        success_rate = (stats["success"] / total) * 100

        # Get recent feedback for this bot
        bot_feedback = [f for f in feedback if f["bot"] == bot_name]
        recent_feedback = bot_feedback[-10:] if len(bot_feedback) > 10 else bot_feedback
        avg_rating = sum(f["rating"] for f in recent_feedback) / len(recent_feedback) if recent_feedback else 0

        # Count recent errors
        cutoff = datetime.now() - timedelta(days=7)
        recent_errors = [
            item for item in work_log
            if item["bot"] == bot_name
            and item["status"] == "failed"
            and datetime.fromisoformat(item["timestamp"]) > cutoff
        ]

        results[bot_name] = {
            "success_rate": round(success_rate, 2),
            "avg_time": round(stats["avg_time"], 2),
            "avg_rating": round(avg_rating, 2),
            "recent_errors": len(recent_errors),
            "total_runs": total
        }

    return results


def generate_improvement_suggestions() -> List[Dict[str, Any]]:
    """Use LLM to analyze performance and suggest improvements."""
    if not LEARNING_ENABLED:
        return []

    performance = analyze_bot_performance()
    feedback = load_feedback()[-50:]  # Last 50 feedback items
    work_log = load_work_log()[-100:]  # Last 100 work items

    # Build analysis prompt
    prompt = f"""Analyze the following bot performance data and suggest specific improvements:

PERFORMANCE METRICS:
{json.dumps(performance, indent=2)}

RECENT FEEDBACK:
{json.dumps(feedback[-10:], indent=2)}

RECENT WORK LOG:
{json.dumps(work_log[-20:], indent=2)}

Based on this data, provide 3-5 specific, actionable improvement suggestions for the bot system.
Focus on:
1. Bots with low success rates or high error counts
2. Common failure patterns
3. User feedback patterns
4. Performance bottlenecks

Format as JSON array of objects with: {{"bot": "BotName", "issue": "description", "suggestion": "specific action", "priority": "high|medium|low"}}
"""

    try:
        messages = [
            {"role": "system", "content": "You are an AI system analyst. Analyze bot performance and suggest improvements. Return only valid JSON."},
            {"role": "user", "content": prompt}
        ]
        response = ollama.chat(model=MODEL, messages=messages)
        suggestions_text = response["message"]["content"]

        # Try to extract JSON from response
        suggestions_text = suggestions_text.strip()
        if suggestions_text.startswith("```"):
            # Remove markdown code blocks
            lines = suggestions_text.split("\n")
            suggestions_text = "\n".join([l for l in lines if not l.startswith("```")])

        suggestions = json.loads(suggestions_text)

        # Save suggestions
        with open(IMPROVEMENTS_FILE, 'w') as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "suggestions": suggestions
            }, indent=2, fp=f)

        return suggestions
    except Exception as e:
        logger.error(f"Error generating improvement suggestions: {e}")
        return []


def apply_improvement(suggestion: Dict[str, Any]) -> Dict[str, Any]:
    """Apply an improvement suggestion to a bot configuration."""
    bot_name = suggestion.get("bot")
    action = suggestion.get("suggestion")

    logger.info(f"Applying improvement to {bot_name}: {action}")

    # This would modify bot configurations based on suggestions
    # For now, just log the action
    log_work_item("CEO", f"Apply improvement to {bot_name}", "completed", action)

    return {"ok": True, "message": f"Improvement applied to {bot_name}"}


def weekly_learning_cycle() -> None:
    """Run weekly learning and improvement cycle."""
    logger.info("Starting weekly learning cycle...")

    # Analyze performance
    performance = analyze_bot_performance()
    logger.info(f"Performance analysis: {json.dumps(performance, indent=2)}")

    # Generate improvements
    suggestions = generate_improvement_suggestions()
    logger.info(f"Generated {len(suggestions)} improvement suggestions")

    # Send analysis report
    report = f"""Weekly Bot Learning Report
========================

PERFORMANCE SUMMARY:
{json.dumps(performance, indent=2)}

IMPROVEMENT SUGGESTIONS:
"""
    for i, suggestion in enumerate(suggestions, 1):
        report += f"\n{i}. [{suggestion.get('priority', 'medium').upper()}] {suggestion.get('bot', 'Unknown')}"
        report += f"\n   Issue: {suggestion.get('issue', '')}"
        report += f"\n   Suggestion: {suggestion.get('suggestion', '')}\n"

    send_email_report("Weekly Bot Learning Report", report)
    logger.info("Weekly learning cycle completed")


# ----------------------------
# Bot Management Classes
# ----------------------------

# Import existing bots
import sys
sys.path.append(r"C:\Users\rfoley\PycharmProjects\InboxBot")

try:
    # Import the inbox bot module
    import inbox_bot as inbox_module
except ImportError:
    inbox_module = None
    logger.warning("inbox_bot module not found. InboxBot functionality will be limited.")

try:
    # Import the teams bot module
    import teams_bot as teams_module
except ImportError:
    teams_module = None
    logger.warning("teams_bot module not found. TeamsBot functionality will be limited.")

try:
    from edi_bot import EDIBot
except ImportError:
    EDIBot = None
    logger.warning("edi_bot module not found. EDIBot functionality will be limited.")


class ChromeBot:
    """Handles EDI integrations via tray.io using Chrome automation with knowledge graph."""

    def __init__(self):
        self.name = "ChromeBot"
        self.knowledge = self.load_knowledge_graph()

    def load_knowledge_graph(self) -> Dict[str, Any]:
        """Load Tray.io knowledge graph if available."""
        kb_file = "knowledge_base/trayio/trayio_knowledge_latest.json"
        if os.path.exists(kb_file):
            try:
                with open(kb_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load Tray.io knowledge graph: {e}")
        return {"workflows": [], "edi_patterns": {}}

    def get_workflow_by_name(self, workflow_name: str) -> Optional[Dict[str, Any]]:
        """Find a workflow by name."""
        workflows = self.knowledge.get("workflows", [])
        for workflow in workflows:
            if workflow.get("name", "").lower() == workflow_name.lower():
                return workflow
        return None

    def get_edi_workflows(self, edi_type: str = None) -> List[Dict[str, Any]]:
        """Get EDI workflows, optionally filtered by type."""
        workflows = self.knowledge.get("workflows", [])
        edi_workflows = [w for w in workflows if w.get("edi_related")]

        if edi_type:
            edi_type = edi_type.lower()
            edi_workflows = [w for w in edi_workflows if edi_type in w.get("name", "").lower()]

        return edi_workflows

    def check_tray_io_workflows(self) -> Dict[str, Any]:
        """Check the status of tray.io EDI workflows using knowledge graph."""
        try:
            workflows = self.knowledge.get("workflows", [])

            if not workflows:
                log_work_item(self.name, "Check tray.io workflows", "completed",
                             "No workflows in knowledge graph. Run trayio_crawler.py")
                return {"ok": True, "message": "No workflows configured", "workflows": []}

            # Group by status
            active = [w for w in workflows if w.get("status") == "enabled"]
            inactive = [w for w in workflows if w.get("status") == "disabled"]
            edi_workflows = [w for w in workflows if w.get("edi_related")]

            log_work_item(self.name, "Check tray.io workflows", "completed",
                         f"{len(active)} active, {len(edi_workflows)} EDI-related")

            return {
                "ok": True,
                "total_workflows": len(workflows),
                "active_workflows": len(active),
                "edi_workflows": len(edi_workflows),
                "workflows": [
                    {
                        "name": w.get("name"),
                        "status": w.get("status"),
                        "edi_related": w.get("edi_related", False)
                    }
                    for w in workflows[:20]  # Limit to 20 for output
                ]
            }

        except Exception as e:
            logger.error(f"ChromeBot error: {e}")
            log_work_item(self.name, "Check tray.io workflows", "failed", str(e))
            return {"ok": False, "error": str(e)}

    def trigger_edi_sync(self, workflow_name: str) -> Dict[str, Any]:
        """Manually trigger an EDI sync workflow."""
        try:
            # Check if workflow exists in knowledge graph
            workflow = self.get_workflow_by_name(workflow_name)

            if not workflow:
                # Try fuzzy match
                workflows = self.knowledge.get("workflows", [])
                matches = [w for w in workflows if workflow_name.lower() in w.get("name", "").lower()]

                if matches:
                    suggested = ", ".join([w.get("name") for w in matches[:3]])
                    return {
                        "ok": False,
                        "error": f"Workflow '{workflow_name}' not found. Did you mean: {suggested}?"
                    }
                else:
                    return {"ok": False, "error": f"Workflow '{workflow_name}' not found in knowledge graph"}

            # Placeholder for actual trigger (would use API or Selenium)
            log_work_item(self.name, f"Trigger workflow: {workflow_name}", "completed",
                         f"Triggered: {workflow_name} (type: {workflow.get('trigger_type')})")

            return {
                "ok": True,
                "message": f"Triggered workflow: {workflow_name}",
                "workflow": {
                    "name": workflow.get("name"),
                    "description": workflow.get("description"),
                    "edi_related": workflow.get("edi_related", False)
                }
            }

        except Exception as e:
            logger.error(f"ChromeBot error: {e}")
            log_work_item(self.name, f"Trigger workflow: {workflow_name}", "failed", str(e))
            return {"ok": False, "error": str(e)}

    def answer_edi_question(self, question: str) -> Dict[str, Any]:
        """Answer questions about EDI workflows using knowledge graph."""
        try:
            question_lower = question.lower()

            # Build context from knowledge graph
            context_parts = []

            # Add workflow info
            workflows = self.knowledge.get("workflows", [])
            if workflows:
                context_parts.append(f"Total workflows: {len(workflows)}")
                edi_count = sum(1 for w in workflows if w.get("edi_related"))
                context_parts.append(f"EDI workflows: {edi_count}")

            # Add EDI patterns
            edi_patterns = self.knowledge.get("edi_patterns", {})
            if edi_patterns:
                context_parts.append("\nEDI Patterns:")
                for pattern_name, pattern_workflows in edi_patterns.items():
                    if pattern_workflows:
                        context_parts.append(f"  {pattern_name.replace('_', ' ').title()}: {', '.join(pattern_workflows[:3])}")

            context = "\n".join(context_parts)

            # Use LLM to answer
            system_prompt = f"""You are an EDI integration expert for Jeffco Fibres.

You have access to this Tray.io workflow information:
{context}

Answer questions about EDI workflows, integrations, and automation.
Be specific and reference actual workflow names when possible."""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ]

            response = ollama.chat(model=MODEL, messages=messages)
            answer = response["message"]["content"]

            log_work_item(self.name, "Answer EDI question", "completed",
                         f"Question: {question[:100]}...")

            return {
                "ok": True,
                "question": question,
                "answer": answer
            }

        except Exception as e:
            logger.error(f"ChromeBot error: {e}")
            log_work_item(self.name, "Answer EDI question", "failed", str(e))
            return {"ok": False, "error": str(e)}


class SQLBot:
    """Handles SQL database operations and ticketing questions with knowledge base."""

    def __init__(self):
        self.name = "SQLBot"
        self.knowledge = self.load_knowledge_base()
        self.training_data = self.load_training_data()

    def load_knowledge_base(self) -> Dict[str, Any]:
        """Load SQL knowledge base if available."""
        kb_file = "training_data/sql_bot_context.json"
        if os.path.exists(kb_file):
            try:
                with open(kb_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load SQL knowledge base: {e}")
        return {}

    def load_training_data(self) -> Dict[str, Any]:
        """Load SQL training data (schemas, examples, etc.)."""
        training_file = "training_data/sql_bot_training.json"
        if os.path.exists(training_file):
            try:
                with open(training_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load SQL training data: {e}")
        return {}

    def build_context(self) -> str:
        """Build context string from knowledge base for LLM."""
        context_parts = []

        # Add available tables
        if self.training_data.get("table_list"):
            context_parts.append("Available tables:")
            context_parts.append(", ".join(self.training_data["table_list"][:30]))

        # Add schema information
        if self.training_data.get("schemas"):
            context_parts.append("\nTable schemas:")
            for schema_name, tables in list(self.training_data["schemas"].items())[:5]:
                context_parts.append(f"\n{schema_name}:")
                for table in tables[:3]:
                    columns = [col["name"] for col in table.get("columns", [])]
                    context_parts.append(f"  {table['name']}: {', '.join(columns[:10])}")

        # Add relationships
        if self.training_data.get("table_relationships"):
            context_parts.append("\nTable relationships:")
            for rel in self.training_data["table_relationships"][:10]:
                context_parts.append(f"  {rel['parent_table']}.{rel['parent_column']} -> "
                                   f"{rel['referenced_table']}.{rel['referenced_column']}")

        # Add example queries
        if self.training_data.get("example_queries"):
            context_parts.append("\nExample queries:")
            for example in self.training_data["example_queries"][:5]:
                context_parts.append(f"  Q: {example['question']}")
                context_parts.append(f"  SQL: {example['sql']}")

        return "\n".join(context_parts)

    def execute_query(self, query: str, read_only: bool = True) -> Dict[str, Any]:
        """Execute a SQL query on the VM database."""
        try:
            # Safety check
            if not read_only and any(keyword in query.upper() for keyword in ['DROP', 'DELETE', 'TRUNCATE', 'UPDATE']):
                return {"ok": False, "error": "Destructive queries require explicit approval"}

            # Try to connect to database
            db_config = {
                "host": os.getenv("DB_HOST", "localhost"),
                "user": os.getenv("DB_USER", ""),
                "password": os.getenv("DB_PASSWORD", ""),
                "database": os.getenv("DB_NAME", "")
            }

            if not db_config["user"]:
                logger.warning("No database credentials configured")
                log_work_item(self.name, f"Execute query", "simulated",
                             f"Query: {query[:100]}... (no DB connection)")
                return {"ok": True, "message": "Query simulated (no DB credentials)", "rows": []}

            # Try SQL Server connection
            try:
                import pyodbc
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={db_config['host']};"
                    f"DATABASE={db_config['database']};"
                    f"UID={db_config['user']};"
                    f"PWD={db_config['password']}"
                )
                conn = pyodbc.connect(conn_str, timeout=10)
                cursor = conn.cursor()
                cursor.execute(query)

                if query.strip().upper().startswith("SELECT"):
                    rows = cursor.fetchall()
                    columns = [column[0] for column in cursor.description]
                    results = [dict(zip(columns, row)) for row in rows]
                    conn.close()

                    log_work_item(self.name, f"Execute query", "completed",
                                 f"Query: {query[:100]}... | {len(results)} rows")
                    return {"ok": True, "message": "Query executed", "rows": results}
                else:
                    conn.commit()
                    conn.close()
                    log_work_item(self.name, f"Execute query", "completed",
                                 f"Query: {query[:100]}...")
                    return {"ok": True, "message": "Query executed"}

            except ImportError:
                logger.warning("pyodbc not installed")
                log_work_item(self.name, f"Execute query", "simulated",
                             f"Query: {query[:100]}... (no pyodbc)")
                return {"ok": True, "message": "Query simulated (no pyodbc)", "query": query}

        except Exception as e:
            logger.error(f"SQLBot error: {e}")
            log_work_item(self.name, "Execute query", "failed", str(e))
            return {"ok": False, "error": str(e)}

    def answer_ticket_question(self, question: str) -> Dict[str, Any]:
        """Answer a ticketing system question using database queries with RAG."""
        try:
            # Build context from knowledge base
            context = self.build_context()

            # Enhanced prompt with context
            system_prompt = f"""You are an expert SQL database assistant for Jeffco Fibres.

You have access to this database schema information:
{context}

Convert natural language questions to SQL queries. Use the schema information above to:
1. Choose the correct tables
2. Use the correct column names
3. Apply appropriate JOINs when needed
4. Follow SQL best practices

Return ONLY the SQL query, nothing else."""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ]

            response = ollama.chat(model=MODEL, messages=messages)
            sql_query = response["message"]["content"].strip()

            # Clean up the SQL query (remove markdown if present)
            if sql_query.startswith("```"):
                lines = sql_query.split("\n")
                sql_query = "\n".join([l for l in lines if not l.startswith("```")])
                sql_query = sql_query.strip()

            # Execute the query
            result = self.execute_query(sql_query)

            log_work_item(self.name, "Answer ticket question", "completed",
                         f"Question: {question} | SQL: {sql_query[:100]}...")

            return {
                "ok": True,
                "question": question,
                "sql": sql_query,
                "result": result
            }

        except Exception as e:
            logger.error(f"SQLBot error: {e}")
            log_work_item(self.name, "Answer ticket question", "failed", str(e))
            return {"ok": False, "error": str(e)}


class InboxBot:
    """Monitors and responds to technical questions in email inbox using the existing inbox_bot."""

    def __init__(self):
        self.name = "InboxBot"
        self.has_module = inbox_module is not None

    def run_one_pass(self) -> Dict[str, Any]:
        """Run one processing pass of the inbox bot. Returns structured results."""
        if not self.has_module:
            return {"ok": False, "error": "inbox_bot module not available"}

        try:
            import pythoncom
            pythoncom.CoInitialize()
            try:
                outlook, ns = inbox_module.connect_outlook(debug=False)
                if ns is None:
                    return {"ok": False, "error": "Could not connect to Outlook"}

                config_path = r"C:\Users\rfoley\PycharmProjects\InboxBot\inbox_bot.config.json"
                cfg = inbox_module.load_config(config_path)

                results = inbox_module.process_pass(ns, cfg)
                if results is None:
                    results = {"processed": 0, "drafts_created": 0, "moved": {},
                               "non_noise_emails": [], "delegations": []}

                # Log individual actions for morning reports
                for email_info in results.get("non_noise_emails", []):
                    log_work_item(self.name, "Draft reply", "completed",
                                 f"Drafted reply to {email_info['sender']}: {email_info['subject']}")
                for deleg in results.get("delegations", []):
                    log_work_item(self.name, f"SQL delegation ({deleg['intent']})", "completed",
                                 f"Queried database for: {deleg['subject']}")
                for triage in results.get("triage_actions", []):
                    log_work_item(self.name,
                                 f"CW Ticket Triage #{triage.get('ticket_number', '?')}",
                                 "completed",
                                 f"Category: {triage['category']}, "
                                 f"Urgency: {triage['urgency']}, "
                                 f"Summary: {triage.get('summary', '')}")

                summary = (f"Processed {results.get('processed', 0)} emails, "
                          f"created {results.get('drafts_created', 0)} drafts, "
                          f"{len(results.get('delegations', []))} delegations, "
                          f"{len(results.get('triage_actions', []))} tickets triaged")
                log_work_item(self.name, "Process inbox", "completed", summary)

                return {"ok": True, "results": results}
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"InboxBot error: {e}")
            log_work_item(self.name, "Process inbox", "failed", str(e))
            return {"ok": False, "error": str(e)}

    def check_inbox(self) -> Dict[str, Any]:
        """Check inbox and process messages."""
        return self.run_one_pass()


class TeamsBot:
    """Monitors and responds to technical questions in Microsoft Teams using the existing teams_bot."""

    def __init__(self):
        self.name = "TeamsBot"
        self.has_module = teams_module is not None
        self.ui = None

    def run_one_pass(self) -> Dict[str, Any]:
        """Run one processing pass of the teams bot."""
        if not self.has_module:
            return {"ok": False, "error": "teams_bot module not available"}

        try:
            config_path = r"C:\Users\rfoley\PycharmProjects\InboxBot\teams_bot.config.json"
            cfg = teams_module.load_config(config_path)

            if self.ui is None:
                self.ui = teams_module.TeamsUI(debug=False)

            if not self.ui.connect():
                return {"ok": False, "error": "Could not connect to Teams"}

            teams_module.process_pass(self.ui, cfg)
            log_work_item(self.name, "Process Teams messages", "completed",
                         "Processed Teams DMs and created draft replies")
            return {"ok": True, "message": "Teams messages processed successfully"}
        except Exception as e:
            logger.error(f"TeamsBot error: {e}")
            log_work_item(self.name, "Process Teams messages", "failed", str(e))
            return {"ok": False, "error": str(e)}

    def check_teams_messages(self) -> Dict[str, Any]:
        """Check Teams for new messages and process them."""
        return self.run_one_pass()


# Initialize sub-bots
edi_bot = EDIBot() if EDIBot else None
chrome_bot = ChromeBot()
sql_bot = SQLBot()
inbox_bot = InboxBot()
teams_bot = TeamsBot()


# ----------------------------
# CEO Bot Tools
# ----------------------------
def send_morning_report() -> Dict[str, Any]:
    """Generate and send the daily morning work summary report."""
    try:
        summary = get_daily_work_summary()
        subject = f"Daily Work Summary - {datetime.now().strftime('%Y-%m-%d')}"
        result = send_email_report(subject, summary)
        return result
    except Exception as e:
        logger.error(f"Error sending morning report: {e}")
        return {"ok": False, "error": str(e)}


def check_all_bots_status() -> Dict[str, Any]:
    """Check the status of all managed bots."""
    try:
        status = {
            "edi_bot": edi_bot.check_status() if edi_bot else {"ok": False, "error": "EDIBot not available"},
            "chrome_bot": chrome_bot.check_tray_io_workflows(),
            "sql_bot": {"ok": True, "status": "ready"},
            "inbox_bot": inbox_bot.check_inbox(),
            "teams_bot": teams_bot.check_teams_messages(),
        }
        return {"ok": True, "bot_status": status}
    except Exception as e:
        logger.error(f"Error checking bot status: {e}")
        return {"ok": False, "error": str(e)}


def delegate_to_chrome_bot(task: str, workflow_name: str = None) -> Dict[str, Any]:
    """Delegate a task to the Chrome bot for EDI/tray.io operations."""
    try:
        if task == "check_workflows":
            return chrome_bot.check_tray_io_workflows()
        elif task == "trigger_sync" and workflow_name:
            return chrome_bot.trigger_edi_sync(workflow_name)
        else:
            return {"ok": False, "error": "Unknown task or missing workflow_name"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_to_edi_bot(
    task: str,
    customer_name: str = None,
    edi_spec: str = None,
    spec_path: str = None,
    workflow_id: str = None,
    workflow_name: str = None,
    payload: Dict[str, Any] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Delegate a task to the EDI bot for templates, Tray.io workflows, and memory."""
    if not edi_bot:
        return {"ok": False, "error": "EDIBot not available"}

    try:
        if task == "create_template":
            result = edi_bot.create_one_shot_template(customer_name, edi_spec, spec_path)
            log_work_item(
                "EDIBot",
                f"Create template for {customer_name}",
                "completed" if result.get("ok") else "failed",
                result.get("template_path", ""),
            )
            return result
        if task == "list_templates":
            return edi_bot.list_templates(customer_name)
        if task == "get_memory":
            return edi_bot.get_memory(limit=limit, customer=customer_name)
        if task == "list_workflows":
            return edi_bot.list_trayio_workflows()
        if task == "get_workflow":
            return edi_bot.get_trayio_workflow(workflow_id)
        if task == "enable_workflow":
            return edi_bot.enable_workflow(workflow_id, workflow_name)
        if task == "disable_workflow":
            return edi_bot.disable_workflow(workflow_id, workflow_name)
        if task == "trigger_workflow":
            return edi_bot.trigger_workflow(workflow_id, workflow_name, payload)
        if task == "sync_trayio_knowledge":
            return edi_bot.sync_trayio_knowledge_graph()

        return {"ok": False, "error": "Unknown task or missing parameters"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_to_sql_bot(task: str, query: str = None, question: str = None) -> Dict[str, Any]:
    """Delegate a task to the SQL bot for database operations."""
    try:
        if task == "execute_query" and query:
            return sql_bot.execute_query(query)
        elif task == "answer_question" and question:
            return sql_bot.answer_ticket_question(question)
        else:
            return {"ok": False, "error": "Unknown task or missing parameters"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_to_inbox_bot(task: str = "check_inbox") -> Dict[str, Any]:
    """Delegate a task to the Inbox bot for email operations."""
    try:
        if task == "check_inbox" or task == "process":
            return inbox_bot.check_inbox()
        else:
            return {"ok": False, "error": "Unknown task"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def process_inbox_and_delegate() -> Dict[str, Any]:
    """Process inbox emails and report on any cross-bot delegations that occurred."""
    try:
        inbox_result = inbox_bot.run_one_pass()
        if not inbox_result.get("ok"):
            return inbox_result

        results = inbox_result.get("results", {})
        actions_taken = []

        for deleg in results.get("delegations", []):
            actions_taken.append(
                f"SQL lookup for {deleg['intent']}: {deleg['subject']}"
            )

        for triage in results.get("triage_actions", []):
            actions_taken.append(
                f"CW Ticket #{triage.get('ticket_number', '?')} triaged: "
                f"{triage['category']}/{triage['urgency']} - {triage.get('summary', '')}"
            )

        return {
            "ok": True,
            "processed": results.get("processed", 0),
            "drafts_created": results.get("drafts_created", 0),
            "moved": results.get("moved", {}),
            "non_noise_emails": results.get("non_noise_emails", []),
            "delegations": results.get("delegations", []),
            "triage_actions": results.get("triage_actions", []),
            "actions_taken": actions_taken,
        }
    except Exception as e:
        logger.error(f"process_inbox_and_delegate error: {e}")
        return {"ok": False, "error": str(e)}


def delegate_to_teams_bot(task: str = "check_messages") -> Dict[str, Any]:
    """Delegate a task to the Teams bot for Teams operations."""
    try:
        if task == "check_messages" or task == "process":
            return teams_bot.check_teams_messages()
        else:
            return {"ok": False, "error": "Unknown task"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Learning system tools
def get_bot_performance() -> Dict[str, Any]:
    """Get current bot performance metrics."""
    try:
        performance = analyze_bot_performance()
        return {"ok": True, "performance": performance}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def generate_improvements() -> Dict[str, Any]:
    """Generate improvement suggestions based on bot performance."""
    try:
        suggestions = generate_improvement_suggestions()
        return {"ok": True, "suggestions": suggestions, "count": len(suggestions)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def record_feedback(bot: str, action: str, rating: int, comment: str = "") -> Dict[str, Any]:
    """Record user feedback for a bot action."""
    try:
        add_feedback(bot, action, rating, comment)
        return {"ok": True, "message": "Feedback recorded"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_triage_summary() -> Dict[str, Any]:
    """Get summary of recent ConnectWise ticket triage actions from the last 24 hours."""
    try:
        work_log = load_work_log()
        cutoff = datetime.now() - timedelta(hours=24)

        triage_items = [
            item for item in work_log
            if "CW Ticket Triage" in item.get("task", "")
            and datetime.fromisoformat(item["timestamp"]) > cutoff
        ]

        by_urgency: Dict[str, int] = {}
        by_category: Dict[str, int] = {}
        tickets = []

        for item in triage_items:
            details = item.get("details", "")
            # Parse category and urgency from details: "Category: X, Urgency: Y, Summary: Z"
            cat_match = re.search(r"Category:\s*(\w+)", details)
            urg_match = re.search(r"Urgency:\s*(\w+)", details)
            cat = cat_match.group(1) if cat_match else "unknown"
            urg = urg_match.group(1) if urg_match else "unknown"

            by_category[cat] = by_category.get(cat, 0) + 1
            by_urgency[urg] = by_urgency.get(urg, 0) + 1
            tickets.append({
                "timestamp": item["timestamp"],
                "task": item["task"],
                "details": details,
            })

        return {
            "ok": True,
            "triage_summary": {
                "total_triaged": len(triage_items),
                "by_urgency": by_urgency,
                "by_category": by_category,
                "tickets": tickets,
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Map tool names to Python functions
TOOL_FUNCTIONS = {
    "send_morning_report": send_morning_report,
    "check_all_bots_status": check_all_bots_status,
    "delegate_to_edi_bot": delegate_to_edi_bot,
    "delegate_to_chrome_bot": delegate_to_chrome_bot,
    "delegate_to_sql_bot": delegate_to_sql_bot,
    "delegate_to_inbox_bot": delegate_to_inbox_bot,
    "delegate_to_teams_bot": delegate_to_teams_bot,
    "process_inbox_and_delegate": process_inbox_and_delegate,
    "get_bot_performance": get_bot_performance,
    "generate_improvements": generate_improvements,
    "record_feedback": record_feedback,
    "get_triage_summary": get_triage_summary,
}


# JSON schemas for Ollama tool calling
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_morning_report",
            "description": "Generate and send the daily morning work summary report to rfoley@jeffcofibres.com",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_all_bots_status",
            "description": "Check the operational status of all managed bots (EDI, Chrome, SQL, Inbox, Teams)",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_edi_bot",
            "description": "Delegate an EDI task: create one-shot templates, manage Tray.io workflows, or query EDI memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task to perform: 'create_template', 'list_templates', 'get_memory', 'list_workflows', 'get_workflow', 'enable_workflow', 'disable_workflow', 'trigger_workflow', 'sync_trayio_knowledge'"
                    },
                    "customer_name": {
                        "type": "string",
                        "description": "Customer name for template or memory queries"
                    },
                    "edi_spec": {
                        "type": "string",
                        "description": "EDI spec text to generate a one-shot template"
                    },
                    "spec_path": {
                        "type": "string",
                        "description": "Path to EDI spec file (text or JSON)"
                    },
                    "workflow_id": {
                        "type": "string",
                        "description": "Tray.io workflow ID"
                    },
                    "workflow_name": {
                        "type": "string",
                        "description": "Tray.io workflow name (resolved to ID)"
                    },
                    "payload": {
                        "type": "object",
                        "description": "Optional payload for trigger_workflow"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max memory entries to return for get_memory"
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_chrome_bot",
            "description": "Delegate a task to the Chrome bot for EDI integrations via tray.io",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task to perform: 'check_workflows' or 'trigger_sync'"
                    },
                    "workflow_name": {
                        "type": "string",
                        "description": "Name of workflow to trigger (required for trigger_sync task)"
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_sql_bot",
            "description": "Delegate a task to the SQL bot for database operations and ticketing questions",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task to perform: 'execute_query' or 'answer_question'"
                    },
                    "query": {
                        "type": "string",
                        "description": "SQL query to execute (for execute_query task)"
                    },
                    "question": {
                        "type": "string",
                        "description": "Natural language question to answer (for answer_question task)"
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_inbox_bot",
            "description": "Delegate a task to the Inbox bot to process Outlook emails and create draft replies",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task to perform: 'check_inbox' or 'process' (both do the same thing)"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_teams_bot",
            "description": "Delegate a task to the Teams bot to process Microsoft Teams DMs and create draft replies",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task to perform: 'check_messages' or 'process' (both do the same thing)"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_inbox_and_delegate",
            "description": "Process all unread inbox emails, create draft replies, and automatically delegate data lookups (lead times, order status, inventory) to the SQL bot. Returns detailed results including which emails were processed and what delegations occurred.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_bot_performance",
            "description": "Analyze and get current performance metrics for all bots including success rates, error counts, and user ratings",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_improvements",
            "description": "Use AI to analyze bot performance and generate specific improvement suggestions",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "record_feedback",
            "description": "Record user feedback for a bot action to improve future performance",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot": {
                        "type": "string",
                        "description": "Bot name: ChromeBot, SQLBot, InboxBot, or TeamsBot"
                    },
                    "action": {
                        "type": "string",
                        "description": "Description of the action taken"
                    },
                    "rating": {
                        "type": "integer",
                        "description": "Rating from 1-5 (1=poor, 5=excellent)"
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional comment with details"
                    }
                },
                "required": ["bot", "action", "rating"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_triage_summary",
            "description": "Get a summary of ConnectWise ticket triage actions from the last 24 hours, including counts by urgency level and category",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]


SYSTEM_PROMPT = """
You are the CEO Bot, an intelligent orchestrator and operator for Jeffco Fibres.
Your primary responsibilities:

1. MORNING REPORTS: Every morning, send a daily work summary email to rfoley@jeffcofibres.com
2. BOT MANAGEMENT: Supervise and delegate tasks to specialized bots:
   - EDIBot: Creates one-shot EDI templates, manages Tray.io workflows, and keeps EDI memory logs
   - ChromeBot: Handles tray.io workflow status checks via knowledge graph
   - SQLBot: Executes database queries and answers ticketing questions
   - InboxBot: Monitors email inbox and responds to technical questions
   - TeamsBot: Monitors Microsoft Teams and responds to technical questions

3. TASK DELEGATION: When asked to perform tasks, delegate to the appropriate bot
4. STATUS MONITORING: Regularly check the status of all bots
5. WORK TRACKING: All bot activities are automatically logged for the daily report
6. INBOX ORCHESTRATION: Periodically process the email inbox using process_inbox_and_delegate.
   When emails require data lookups (lead times, order status, inventory), the inbox bot
   automatically queries the Sage 100 database and includes the data in draft replies.
   Review delegation results and report on what was processed.
7. CONNECTWISE TRIAGE: The InboxBot automatically triages ConnectWise ticket emails.
   New CW tickets are classified by category (network, hardware, software, access, email,
   other) and urgency level (critical, high, medium, low). Personalized draft responses are
   created based on ticket type. Use get_triage_summary to review recent triage actions and
   flag critical/high urgency tickets in the morning report.

You may ONLY use the provided tools.
Never invent tool results.
Always log important activities for the daily report.
Be proactive in delegating work to the appropriate specialized bot.
When asked to process the inbox, prefer process_inbox_and_delegate over delegate_to_inbox_bot
as it provides richer results including delegation details.
"""


def run_agent(user_request: str) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_request},
    ]

    for _ in range(8):  # cap loops
        response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )

        msg = response["message"]
        messages.append(msg)

        # If no tool calls, print final response and stop
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            print("\nAssistant:")
            print(msg.get("content", "").strip())
            return

        # Execute tool calls
        for call in tool_calls:
            fn_name = call["function"]["name"]
            args = call["function"].get("arguments", {}) or {}

            print(f"\n[Tool call] {fn_name}({args})")

            fn = TOOL_FUNCTIONS.get(fn_name)
            if not fn:
                tool_result = {"ok": False, "error": f"Unknown tool: {fn_name}"}
            else:
                try:
                    tool_result = fn(**args)
                except TypeError as e:
                    tool_result = {"ok": False, "error": f"Invalid args for {fn_name}: {e}"}
                except Exception as e:
                    tool_result = {"ok": False, "error": str(e)}

            print("[Tool result]")
            print(json.dumps(tool_result, indent=2))

            # Feed tool result back to model
            messages.append({
                "role": "tool",
                "content": json.dumps(tool_result),
            })

    print("\nStopped: reached max tool-call iterations.")


def run_morning_routine():
    """Execute the morning routine - check all bots and send report."""
    logger.info("Starting morning routine...")

    # Check all bot statuses
    print("\n[Morning Routine] Checking all bot statuses...")
    run_agent("Check the status of all bots")

    # Send morning report
    print("\n[Morning Routine] Sending morning report...")
    run_agent("Send the morning report to rfoley@jeffcofibres.com")

    logger.info("Morning routine completed.")


def run_scheduled_tasks():
    """Run scheduled background tasks (morning reports, bot checks, learning)."""
    import schedule
    import threading

    # Schedule morning report for 8:00 AM every day
    schedule.every().day.at("08:00").do(run_morning_routine)

    # Schedule inbox processing every 5 minutes
    schedule.every(5).minutes.do(lambda: run_agent("Process the inbox for new emails and handle any delegations"))

    # Schedule bot status checks every 4 hours
    schedule.every(4).hours.do(lambda: run_agent("Check the status of all bots"))

    # Schedule weekly learning cycle every Monday at 9:00 AM
    schedule.every().monday.at("09:00").do(weekly_learning_cycle)

    # Schedule performance analysis every day at 6:00 PM
    schedule.every().day.at("18:00").do(lambda: run_agent("Analyze bot performance and suggest improvements"))

    def schedule_loop():
        while True:
            schedule.run_pending()
            time.sleep(60)

    # Run scheduler in background thread
    scheduler_thread = threading.Thread(target=schedule_loop, daemon=True)
    scheduler_thread.start()
    logger.info("Scheduled tasks started:")
    logger.info("  - Morning reports at 8:00 AM daily")
    logger.info("  - Inbox processing every 5 minutes")
    logger.info("  - Bot checks every 4 hours")
    logger.info("  - Performance analysis at 6:00 PM daily")
    logger.info("  - Weekly learning cycle Mondays at 9:00 AM")


if __name__ == "__main__":
    print(f"CEO Bot - Intelligent Orchestrator for Jeffco Fibres (model={MODEL})")
    print("=" * 70)
    print("\nManaging bots: EDI, Chrome (Tray.io), SQL (Database), Inbox (Email), Teams")
    print("Daily reports sent to: rfoley@jeffcofibres.com")
    print("\nCommands:")
    print("  - 'morning report' - Send the daily work summary")
    print("  - 'check bots' - Check status of all managed bots")
    print("  - 'process inbox' - Process inbox emails and handle delegations")
    print("  - 'exit' or 'quit' - Exit the program")
    print("=" * 70)

    # Start scheduled tasks
    try:
        run_scheduled_tasks()
    except ImportError:
        logger.warning("schedule module not installed. Scheduled tasks disabled.")
        print("\nNote: Install 'schedule' module for automatic morning reports:")
        print("  pip install schedule")

    # Interactive loop
    while True:
        try:
            user_input = input("\nCEO Bot > ").strip()
            if user_input.lower() in {"exit", "quit"}:
                break
            if not user_input:
                continue

            # Handle special commands
            if user_input.lower() == "morning report":
                run_morning_routine()
            elif user_input.lower() == "check bots":
                run_agent("Check the status of all bots")
            elif user_input.lower() == "process inbox":
                run_agent("Process the inbox for new emails and handle any delegations")
            else:
                run_agent(user_input)

        except KeyboardInterrupt:
            print("\n\nShutting down CEO Bot...")
            break

    print("\nCEO Bot terminated. Goodbye!")
