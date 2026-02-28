"""LeadTime Bot - Desktop GUI

A user-friendly Tkinter interface for managing customer lead time rules.
Wraps the LeadTime Bot agent (Claude API) in a chat-style desktop application.

Usage:
    python leadtime_gui.py

Can also be bundled with PyInstaller:
    pyinstaller --onefile --windowed --name "LeadTime Bot" leadtime_gui.py
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

# ---------------------------------------------------------------------------
# PyInstaller: ensure bundled data files are on the import path
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle - add the temp extract dir to sys.path
    _bundle_dir = sys._MEIPASS
    if _bundle_dir not in sys.path:
        sys.path.insert(0, _bundle_dir)
    # Also load .env from the bundle
    _env_path = os.path.join(_bundle_dir, ".env")
else:
    _env_path = ".env"

from dotenv import load_dotenv

load_dotenv(_env_path)

# Explicit imports so PyInstaller bundles these modules
import anthropic  # noqa: F401
import pydantic  # noqa: F401
import httpx  # noqa: F401
import pyodbc  # noqa: F401
import leadtime_bot  # noqa: F401
import leadtime_tools  # noqa: F401
import leadtime_parser  # noqa: F401
import leadtime_models  # noqa: F401


# ---------------------------------------------------------------------------
# Colors & Styling
# ---------------------------------------------------------------------------

BG_COLOR = "#1e1e2e"           # Dark background
SIDEBAR_BG = "#181825"         # Slightly darker sidebar
INPUT_BG = "#313244"           # Input field background
TEXT_COLOR = "#cdd6f4"         # Main text
ACCENT_COLOR = "#89b4fa"       # Blue accent
USER_COLOR = "#a6e3a1"        # Green for user messages
BOT_COLOR = "#89b4fa"          # Blue for bot messages
STATUS_COLOR = "#f9e2af"       # Yellow for status
ERROR_COLOR = "#f38ba8"        # Red for errors
BUTTON_BG = "#89b4fa"         # Button background
BUTTON_FG = "#1e1e2e"         # Button text
HOVER_BG = "#74c7ec"          # Button hover


class LeadTimeGUI:
    """Desktop GUI for the LeadTime Bot."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LeadTime Bot - Jeffco Fibres")
        self.root.geometry("1000x700")
        self.root.minsize(800, 500)
        self.root.configure(bg=BG_COLOR)

        # Set app icon if available
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        self.processing = False

        self._build_ui()
        self._check_api_key()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Build the main UI layout."""
        # Configure grid weights for resizing
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # Left sidebar with quick actions
        self._build_sidebar()

        # Main content area
        main_frame = tk.Frame(self.root, bg=BG_COLOR)
        main_frame.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        main_frame.grid_rowconfigure(1, weight=1)
        main_frame.grid_columnconfigure(0, weight=1)

        # Header
        self._build_header(main_frame)

        # Chat display
        self._build_chat_area(main_frame)

        # Input area
        self._build_input_area(main_frame)

        # Status bar
        self._build_status_bar(main_frame)

    def _build_sidebar(self):
        """Build the left sidebar with quick-action buttons."""
        sidebar = tk.Frame(self.root, bg=SIDEBAR_BG, width=220)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)

        # Sidebar header
        header = tk.Label(
            sidebar,
            text="Quick Actions",
            font=("Segoe UI", 12, "bold"),
            fg=ACCENT_COLOR,
            bg=SIDEBAR_BG,
            pady=15,
        )
        header.pack(fill="x")

        # Separator
        sep = tk.Frame(sidebar, bg=ACCENT_COLOR, height=1)
        sep.pack(fill="x", padx=15, pady=(0, 10))

        # Quick action buttons
        quick_actions = [
            ("List All Customers", "List all customers and their lead times"),
            ("Check Shipping File", "List customers from the shipping file only"),
            ("Check Production File", "List customers from the production file only"),
        ]

        sep2_label = tk.Label(
            sidebar,
            text="LOOKUP",
            font=("Segoe UI", 9),
            fg="#6c7086",
            bg=SIDEBAR_BG,
            anchor="w",
            padx=15,
        )
        sep2_label.pack(fill="x", pady=(5, 2))

        for label, command in quick_actions:
            self._make_sidebar_button(sidebar, label, command)

        # Separator
        sep3_label = tk.Label(
            sidebar,
            text="COMMON UPDATES",
            font=("Segoe UI", 9),
            fg="#6c7086",
            bg=SIDEBAR_BG,
            anchor="w",
            padx=15,
        )
        sep3_label.pack(fill="x", pady=(15, 2))

        update_actions = [
            ("Check Connection", "Check connectivity to the SQL Server Agent jobs"),
            ("Check Differences", "Show customers that have different rules between shipping and production jobs"),
        ]

        for label, command in update_actions:
            self._make_sidebar_button(sidebar, label, command)

        # Separator
        sep4_label = tk.Label(
            sidebar,
            text="HELP",
            font=("Segoe UI", 9),
            fg="#6c7086",
            bg=SIDEBAR_BG,
            anchor="w",
            padx=15,
        )
        sep4_label.pack(fill="x", pady=(15, 2))

        help_actions = [
            ("How To Use", "What can you help me with? Give me a summary of your capabilities."),
        ]

        for label, command in help_actions:
            self._make_sidebar_button(sidebar, label, command)

        # Spacer
        spacer = tk.Frame(sidebar, bg=SIDEBAR_BG)
        spacer.pack(fill="both", expand=True)

        # Version label at bottom
        version_label = tk.Label(
            sidebar,
            text="LeadTime Bot v1.0\nPowered by Claude",
            font=("Segoe UI", 8),
            fg="#6c7086",
            bg=SIDEBAR_BG,
            pady=10,
        )
        version_label.pack(side="bottom", fill="x")

    def _make_sidebar_button(self, parent, label: str, command: str):
        """Create a styled sidebar button."""
        btn = tk.Button(
            parent,
            text=f"  {label}",
            font=("Segoe UI", 10),
            fg=TEXT_COLOR,
            bg=SIDEBAR_BG,
            activeforeground=ACCENT_COLOR,
            activebackground="#313244",
            bd=0,
            anchor="w",
            padx=15,
            pady=6,
            cursor="hand2",
            command=lambda cmd=command: self._send_quick_action(cmd),
        )
        btn.pack(fill="x")

        # Hover effects
        btn.bind("<Enter>", lambda e, b=btn: b.config(bg="#313244", fg=ACCENT_COLOR))
        btn.bind("<Leave>", lambda e, b=btn: b.config(bg=SIDEBAR_BG, fg=TEXT_COLOR))

    def _build_header(self, parent):
        """Build the header bar."""
        header_frame = tk.Frame(parent, bg=BG_COLOR, pady=10, padx=15)
        header_frame.grid(row=0, column=0, sticky="ew")

        title = tk.Label(
            header_frame,
            text="LeadTime Bot",
            font=("Segoe UI", 18, "bold"),
            fg=TEXT_COLOR,
            bg=BG_COLOR,
        )
        title.pack(side="left")

        subtitle = tk.Label(
            header_frame,
            text="  Customer Lead Time Manager",
            font=("Segoe UI", 11),
            fg="#6c7086",
            bg=BG_COLOR,
        )
        subtitle.pack(side="left", padx=(5, 0))

        # Connection indicator
        self.connection_dot = tk.Label(
            header_frame,
            text="\u25cf",
            font=("Segoe UI", 12),
            fg="#a6e3a1",
            bg=BG_COLOR,
        )
        self.connection_dot.pack(side="right")

        self.connection_label = tk.Label(
            header_frame,
            text="Ready",
            font=("Segoe UI", 10),
            fg="#a6e3a1",
            bg=BG_COLOR,
        )
        self.connection_label.pack(side="right", padx=(0, 5))

    def _build_chat_area(self, parent):
        """Build the chat message display area."""
        chat_frame = tk.Frame(parent, bg=BG_COLOR, padx=15)
        chat_frame.grid(row=1, column=0, sticky="nsew")
        chat_frame.grid_rowconfigure(0, weight=1)
        chat_frame.grid_columnconfigure(0, weight=1)

        self.chat_display = scrolledtext.ScrolledText(
            chat_frame,
            wrap=tk.WORD,
            font=("Consolas", 10),
            bg="#11111b",
            fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
            selectbackground=ACCENT_COLOR,
            selectforeground=BG_COLOR,
            bd=0,
            padx=12,
            pady=12,
            state=tk.DISABLED,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#313244",
            highlightcolor=ACCENT_COLOR,
        )
        self.chat_display.grid(row=0, column=0, sticky="nsew")

        # Configure text tags for colored messages
        self.chat_display.tag_config("user", foreground=USER_COLOR, font=("Consolas", 10, "bold"))
        self.chat_display.tag_config("bot", foreground=BOT_COLOR)
        self.chat_display.tag_config("status", foreground=STATUS_COLOR, font=("Consolas", 9, "italic"))
        self.chat_display.tag_config("error", foreground=ERROR_COLOR)
        self.chat_display.tag_config("timestamp", foreground="#6c7086", font=("Consolas", 8))
        self.chat_display.tag_config("tool", foreground="#fab387", font=("Consolas", 9))

        # Welcome message
        self._append_message(
            "Welcome to LeadTime Bot!\n\n"
            "I help you manage customer lead time rules in the SQL Server Agent job scripts.\n\n"
            "Try asking me things like:\n"
            '  - "List all customers and their lead times"\n'
            '  - "What are the rules for Mattress Firm?"\n'
            '  - "Change Mattress Firm lead time to 4 business days"\n'
            '  - "Add new customer ACME Corp with 5 day lead time"\n\n'
            "Or use the Quick Actions on the left to get started.\n",
            tag="bot",
        )

    def _build_input_area(self, parent):
        """Build the message input area."""
        input_frame = tk.Frame(parent, bg=BG_COLOR, padx=15)
        input_frame.grid(row=2, column=0, sticky="ew", pady=(5, 10))
        input_frame.grid_columnconfigure(0, weight=1)

        # Input row
        input_row = tk.Frame(input_frame, bg=INPUT_BG, highlightthickness=1,
                            highlightbackground="#45475a", highlightcolor=ACCENT_COLOR)
        input_row.grid(row=0, column=0, sticky="ew")
        input_row.grid_columnconfigure(0, weight=1)

        self.input_field = tk.Entry(
            input_row,
            font=("Segoe UI", 12),
            bg=INPUT_BG,
            fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
            bd=0,
            relief="flat",
        )
        self.input_field.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        self.input_field.bind("<Return>", self._on_enter)
        self.input_field.focus_set()

        # Send button
        self.send_btn = tk.Button(
            input_row,
            text="Send",
            font=("Segoe UI", 10, "bold"),
            fg=BUTTON_FG,
            bg=BUTTON_BG,
            activeforeground=BUTTON_FG,
            activebackground=HOVER_BG,
            bd=0,
            padx=20,
            pady=6,
            cursor="hand2",
            command=self._on_send,
        )
        self.send_btn.grid(row=0, column=1, padx=(0, 8), pady=6)

        # Hint text
        hint = tk.Label(
            input_frame,
            text="Press Enter to send  |  Connected directly to SQL Server Agent jobs on JEF-SQL",
            font=("Segoe UI", 8),
            fg="#6c7086",
            bg=BG_COLOR,
        )
        hint.grid(row=1, column=0, sticky="w", pady=(3, 0))

    def _build_status_bar(self, parent):
        """Build the bottom status bar."""
        status_frame = tk.Frame(parent, bg=SIDEBAR_BG, padx=15, pady=5)
        status_frame.grid(row=3, column=0, sticky="ew")
        status_frame.grid_columnconfigure(0, weight=1)

        self.status_label = tk.Label(
            status_frame,
            text="Ready",
            font=("Segoe UI", 9),
            fg="#6c7086",
            bg=SIDEBAR_BG,
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        network_label = tk.Label(
            status_frame,
            text="SQL Server: JEF-SQL (direct)",
            font=("Segoe UI", 9),
            fg="#6c7086",
            bg=SIDEBAR_BG,
            anchor="e",
        )
        network_label.grid(row=0, column=1, sticky="e")

    # ------------------------------------------------------------------
    # Message Display
    # ------------------------------------------------------------------

    def _append_message(self, text: str, tag: str = "bot", prefix: str = ""):
        """Append a message to the chat display."""
        self.chat_display.config(state=tk.NORMAL)

        # Add timestamp
        timestamp = datetime.now().strftime("%H:%M")

        if prefix:
            self.chat_display.insert(tk.END, f"[{timestamp}] {prefix}\n", "timestamp")
        elif tag == "user":
            self.chat_display.insert(tk.END, f"\n[{timestamp}] You:\n", "timestamp")
        elif tag == "bot":
            if self.chat_display.get("1.0", tk.END).strip():
                self.chat_display.insert(tk.END, f"\n[{timestamp}] LeadTime Bot:\n", "timestamp")

        self.chat_display.insert(tk.END, text + "\n", tag)
        self.chat_display.config(state=tk.DISABLED)
        self.chat_display.see(tk.END)

    def _set_status(self, text: str, color: str = "#6c7086"):
        """Update the status bar."""
        self.status_label.config(text=text, fg=color)

    def _set_connection(self, text: str, color: str):
        """Update the connection indicator."""
        self.connection_dot.config(fg=color)
        self.connection_label.config(text=text, fg=color)

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    def _on_enter(self, event):
        """Handle Enter key in input field."""
        self._on_send()

    def _on_send(self):
        """Handle send button click."""
        text = self.input_field.get().strip()
        if not text or self.processing:
            return

        self.input_field.delete(0, tk.END)
        self._append_message(text, tag="user")
        self._run_agent_async(text)

    def _send_quick_action(self, command: str):
        """Send a quick action command."""
        if self.processing:
            return
        self._append_message(command, tag="user")
        self._run_agent_async(command)

    def _run_agent_async(self, request: str):
        """Run the agent in a background thread."""
        self.processing = True
        self.send_btn.config(state=tk.DISABLED, bg="#45475a")
        self._set_status("Processing...", STATUS_COLOR)
        self._set_connection("Working...", STATUS_COLOR)

        # Show thinking indicator
        self._append_message("Thinking...", tag="status")

        thread = threading.Thread(target=self._run_agent_thread, args=(request,), daemon=True)
        thread.start()

    def _run_agent_thread(self, request: str):
        """Agent execution in background thread."""
        try:
            from leadtime_bot import run_agent, ANTHROPIC_API_KEY

            if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "sk-ant-your-key-here":
                self.root.after(0, self._on_agent_error,
                              "ANTHROPIC_API_KEY not configured.\n\n"
                              "Please set a valid API key in the .env file:\n"
                              "ANTHROPIC_API_KEY=sk-ant-...")
                return

            response = run_agent(request)
            self.root.after(0, self._on_agent_response, response)

        except ImportError as e:
            self.root.after(0, self._on_agent_error,
                          f"Missing dependency: {e}\n\nRun: pip install anthropic pydantic python-dotenv")
        except Exception as e:
            self.root.after(0, self._on_agent_error, str(e))

    def _on_agent_response(self, response: str):
        """Handle agent response on the main thread."""
        # Remove the "Thinking..." status line
        self._remove_last_status()

        self._append_message(response, tag="bot")
        self._finish_processing()

    def _on_agent_error(self, error: str):
        """Handle agent error on the main thread."""
        self._remove_last_status()
        self._append_message(f"Error: {error}", tag="error")
        self._finish_processing()

    def _finish_processing(self):
        """Reset UI after processing completes."""
        self.processing = False
        self.send_btn.config(state=tk.NORMAL, bg=BUTTON_BG)
        self._set_status("Ready", "#6c7086")
        self._set_connection("Ready", "#a6e3a1")
        self.input_field.focus_set()

    def _remove_last_status(self):
        """Remove the last 'Thinking...' status message."""
        self.chat_display.config(state=tk.NORMAL)
        # Search backwards for the "Thinking..." text
        content = self.chat_display.get("1.0", tk.END)
        idx = content.rfind("Thinking...")
        if idx >= 0:
            # Find the line containing "Thinking..."
            line_start = content.rfind("\n", 0, idx)
            line_end = content.find("\n", idx)
            if line_start == -1:
                line_start = 0
            else:
                line_start += 1

            # Convert character offset to tkinter index
            start_line = content[:line_start].count("\n") + 1
            end_line = content[:line_end + 1].count("\n") + 1 if line_end >= 0 else tk.END

            self.chat_display.delete(f"{start_line}.0", f"{end_line}.0")
        self.chat_display.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Startup checks
    # ------------------------------------------------------------------

    def _check_api_key(self):
        """Check if API key is configured."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key or api_key == "sk-ant-your-key-here":
            self._set_connection("No API Key", ERROR_COLOR)
            self._set_status("Warning: ANTHROPIC_API_KEY not set in .env", ERROR_COLOR)
            self._append_message(
                "Warning: ANTHROPIC_API_KEY is not configured.\n"
                "Please add your Anthropic API key to the .env file:\n"
                "  ANTHROPIC_API_KEY=sk-ant-...\n",
                tag="error",
            )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        """Start the GUI event loop."""
        self.root.mainloop()


def main():
    """Launch the LeadTime Bot GUI."""
    app = LeadTimeGUI()
    app.run()


if __name__ == "__main__":
    main()
