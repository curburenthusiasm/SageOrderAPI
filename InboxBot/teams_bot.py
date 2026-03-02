#!/usr/bin/env python3
"""
Microsoft Teams DM Bot (local, draft-only) — Ollama + UI Automation

What it does:
- Connects to the Teams desktop window (must be running)
- Filters to Unread chats
- Processes 1:1 DMs only (best-effort heuristic)
- Generates a reply with Ollama and types it into the compose box
- Does NOT send (no Ctrl+Enter)

Requirements:
- Windows + Teams Desktop
- pip install pywinauto requests
- Ollama running: http://localhost:11434
"""

import json
import os
import re
import sys
import time
import ctypes
from ctypes import wintypes
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None

try:
    from pywinauto import Application, Desktop
except Exception:
    Application = None
    Desktop = None

try:
    import msvcrt  # Windows-only file locking
except Exception:
    msvcrt = None


BASE_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOCK_FILE = os.path.join(BASE_DIR, ".teams_bot.lock")
ACTION_LOG = os.path.join(LOG_DIR, "teams_actions.jsonl")
LLM_LOG = os.path.join(LOG_DIR, "teams_llm_calls.jsonl")

CHAT_LAST_DRAFT_TS: Dict[str, float] = {}


DEFAULT_CONFIG: Dict[str, Any] = {
    "loop_seconds": 45,
    "max_chats_per_pass": 15,
    "debug": True,
    "single_instance_lock": True,
    "error_backoff_seconds": 15,
    "max_backoff_seconds": 300,
    "reload_config_each_pass": True,
    "debug_dump_on_empty": True,
    "require_unread_indicator": True,
    "debug_dump_unread_probe": True,
    "unread_text_regex": [
        r"\bunread\b",
        r"\bnew message\b",
        r"\bnew\b",
    ],
    "require_unread_in_chat": False,
    "unread_in_chat_text_regex": [
        r"\blast read\b",
        r"\bnew messages?\b",
        r"\bunread messages?\b",
    ],
    "unread_in_chat_wait_seconds": 3.0,
    "unread_in_chat_poll_seconds": 0.2,
    "debug_dump_list_item_props": True,
    "debug_dump_list_item_props_limit": 8,
    "chat_list_min_width": 160,
    "chat_list_min_height": 28,
    "chat_list_max_height": 120,
    "chat_list_ignore_text": [
        "activity", "calls", "onedrive", "calendar", "apps",
        "copilot", "discover", "mentions", "followed threads",
        "favorites", "teams and channels", "chats", "chat",
        "unread", "channels", "meeting chats",
    ],
    "unread_pixel_enabled": False,
    "unread_pixel_min_b": 150,
    "unread_pixel_min_g": 140,
    "unread_pixel_max_r": 130,
    "unread_pixel_min_b_over_r": 40,
    "unread_pixel_min_b_over_g": 10,
    "unread_pixel_sample_step": 3,
    "unread_pixel_left_pad": 2,
    "unread_pixel_width": 10,
    "unread_pixel_right_pad": 2,
    "unread_pixel_right_width": 12,
    "debug_dump_unread_pixel_stats": False,
    "debug_dump_unread_pixel_stats_limit": 6,
    "process_all_chats": True,
    "allow_text_fallback": True,
    "llm_decide_reply": True,
    "llm_decide_reply_prompt": (
        "Decide if a reply is needed. If no reply is needed, output exactly NO_REPLY. "
        "If a reply is needed, output only the draft reply."
    ),

    "ollama_host": "http://localhost:11434",
    "ollama_model": "qwen2.5:7b-instruct",
    "ollama_timeout_seconds": 180,

    "dm_only": True,
    "group_chat_title_regex": [
        r"\s/\s",
        r"\s&\s",
        r",",
    ],

    "blocked_chat_name_regex": [
        r"^no-?reply\b",
    ],
    "blocked_chat_title_exact": [
        "calls",
        "calendar",
        "activity",
        "apps",
    ],
    "dedupe_chats_per_pass": True,
    "min_seconds_between_drafts_per_chat": 600,
    "advance_after_draft": True,
    "advance_after_draft_keys": "^{DOWN}",
    "advance_after_draft_delay": 0.3,
    "keyboard_cycle_unread": False,
    "keyboard_next_chat_keys": "^{DOWN}",
    "keyboard_next_chat_delay": 0.3,

    "reply_style": (
        "You are a professional, friendly, respectful service-oriented assistant. "
        "Write a concise reply suitable for Microsoft Teams chat. "
        "Acknowledge the request, ask 1–3 clarifying questions if needed, "
        "and state next steps. Do not invent facts. No signature."
    ),
}


def now_local() -> datetime:
    return datetime.now()


def log_jsonl(path: str, obj: Dict[str, Any]) -> None:
    try:
        rec = dict(obj)
        rec["ts"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Wrote default Teams config to {path}. Edit it if needed.")
        return DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return DEFAULT_CONFIG
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def acquire_single_instance_lock(enabled: bool, debug: bool) -> Optional[object]:
    if not enabled or msvcrt is None:
        return None
    try:
        f = open(LOCK_FILE, "a+")
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f
    except Exception as e:
        if debug:
            print(f"DEBUG: could not acquire lock: {e}")
        return None


def ollama_chat(host: str, model: str, messages: List[Dict[str, str]], timeout_s: int) -> str:
    if requests is None:
        raise RuntimeError("requests not installed. Run: pip install requests")
    url = host.rstrip("/") + "/api/chat"
    payload = {"model": model, "messages": messages, "stream": False}
    r = requests.post(url, json=payload, timeout=timeout_s)
    if r.status_code == 404:
        # Fallback for older Ollama builds
        prompt = "\n".join([f"{m.get('role','')}: {m.get('content','')}" for m in messages])
        url = host.rstrip("/") + "/api/generate"
        payload = {"model": model, "prompt": prompt, "stream": False}
        r = requests.post(url, json=payload, timeout=timeout_s)
    try:
        r.raise_for_status()
    except Exception as e:
        detail = ""
        try:
            detail = r.text or ""
        except Exception:
            pass
        raise RuntimeError(f"Ollama HTTP {r.status_code}: {detail}") from e
    data = r.json()
    if "message" in data:
        return (data.get("message", {}) or {}).get("content", "").strip()
    return (data.get("response", "") or "").strip()


def build_prompt(sender: str, last_message: str, reply_style: str) -> str:
    return (
        f"{reply_style}\n\n"
        f"From: {sender}\n"
        f"Message:\n{last_message}\n"
    )


def generate_reply_ollama(host: str, model: str, timeout_s: int, sender: str, last_message: str, reply_style: str) -> str:
    prompt = build_prompt(sender, last_message, reply_style)
    log_jsonl(LLM_LOG, {"event": "ollama_request", "model": model, "sender": sender, "prompt_preview": prompt[:1500]})
    messages = [
        {"role": "system", "content": "Draft accurate, professional Teams replies. No signature."},
        {"role": "user", "content": prompt},
    ]
    reply = ollama_chat(host=host, model=model, messages=messages, timeout_s=timeout_s)
    log_jsonl(LLM_LOG, {"event": "ollama_response", "model": model, "reply_preview": reply[:1500]})
    return reply.strip()


def generate_reply_or_no_reply_ollama(
    host: str,
    model: str,
    timeout_s: int,
    sender: str,
    last_message: str,
    reply_style: str,
    decide_prompt: str,
) -> str:
    prompt = (
        f"{decide_prompt}\n\n"
        f"{reply_style}\n\n"
        f"From: {sender}\n"
        f"Message:\n{last_message}\n"
    )
    log_jsonl(LLM_LOG, {"event": "ollama_request", "model": model, "sender": sender, "prompt_preview": prompt[:1500]})
    messages = [
        {"role": "system", "content": "You decide whether to reply and draft it. Output NO_REPLY or the reply only."},
        {"role": "user", "content": prompt},
    ]
    reply = ollama_chat(host=host, model=model, messages=messages, timeout_s=timeout_s)
    log_jsonl(LLM_LOG, {"event": "ollama_response", "model": model, "reply_preview": reply[:1500]})
    return reply.strip()


def safe_lower(s: str) -> str:
    return (s or "").strip().lower()


def normalize_title(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    return s.strip()


class TeamsUI:
    def __init__(self, debug: bool):
        self.debug = debug
        self.app = None
        self.win = None
        self.assume_unread_filter = False
        self._cached_chat_col_rect = None

    def connect(self) -> bool:
        if Application is None or Desktop is None:
            print("ERROR: pywinauto is not installed. Run: pip install pywinauto")
            return False
        try:
            desk = Desktop(backend="uia")
            for pat in (r".*Microsoft Teams.*", r".*Chat \| Microsoft Teams.*", r".*Teams.*"):
                try:
                    win = desk.window(title_re=pat)
                    if win.exists(timeout=2):
                        self.win = win
                        self.app = None
                        if self.debug:
                            print(f"DEBUG: connected to window: {win.window_text()}")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            self.app = Application(backend="uia").connect(title_re=".*Microsoft Teams.*", timeout=5)
            self.win = self.app.top_window()
            if self.debug:
                print(f"DEBUG: connected to window: {self.win.window_text()}")
            return True
        except Exception as e:
            if self.debug:
                print(f"DEBUG: Teams connect failed: {e}")
            return False

    def focus(self) -> None:
        if self.win is None:
            return
        try:
            self.win.set_focus()
        except Exception:
            pass

    def send_keys(self, keys: str) -> None:
        if self.win is None:
            return
        try:
            self.win.type_keys(keys, with_spaces=True, set_foreground=True)
        except Exception:
            pass

    def open_chat_view(self) -> None:
        if self.win is None:
            return
        self.focus()
        # Shortcut: open Chat view
        self.send_keys("^%c")
        time.sleep(0.4)
        # Fallback: click Chat navigation if available
        for ct in ("Button", "TabItem", "ListItem"):
            try:
                btn = self.win.child_window(title_re=r"^Chat( and channels)?$", control_type=ct)
                if btn.exists(timeout=1):
                    btn.click_input()
                    time.sleep(0.3)
                    return
            except Exception:
                continue

    def open_unread_chats(self) -> None:
        # Shortcut per Microsoft: See all unread chats = Ctrl+Alt+U
        self.open_chat_view()
        self.send_keys("^%u")
        time.sleep(0.5)
        self.click_unread_tab()
        self.assume_unread_filter = True

    def click_unread_tab(self) -> None:
        if self.win is None:
            return
        for ct in ("TabItem", "Button"):
            try:
                btn = self.win.child_window(title="Unread", control_type=ct)
                if btn.exists(timeout=1):
                    btn.click_input()
                    time.sleep(0.3)
                    return
            except Exception:
                continue

    def get_unread_tab_rect(self):
        if self.win is None:
            return None
        for ct in ("TabItem", "Button"):
            try:
                btn = self.win.child_window(title="Unread", control_type=ct)
                if btn.exists(timeout=1):
                    return btn.rectangle()
            except Exception:
                continue
        return None

    def is_unread_filter_active(self) -> bool:
        if self.win is None:
            return False
        for ct in ("TabItem", "Button"):
            try:
                btn = self.win.child_window(title="Unread", control_type=ct)
                if not btn.exists(timeout=1):
                    continue
                try:
                    if hasattr(btn, "get_toggle_state") and btn.get_toggle_state() == 1:
                        return True
                except Exception:
                    pass
                try:
                    if btn.iface_toggle.CurrentToggleState == 1:
                        return True
                except Exception:
                    pass
                try:
                    if btn.iface_selection_item.CurrentIsSelected:
                        return True
                except Exception:
                    pass
                try:
                    if hasattr(btn, "is_selected") and btn.is_selected():
                        return True
                except Exception:
                    pass
            except Exception:
                continue
        return False

    def open_all_chats(self) -> None:
        # See all chat conversations = Ctrl+Alt+C
        self.send_keys("^%c")
        time.sleep(0.5)

    def _find_chat_list(self):
        if self.win is None:
            return None
        left_rect = self._left_pane_rect()
        tab_rect = self.get_unread_tab_rect()
        try:
            lists = self.win.descendants(control_type="List")
        except Exception:
            lists = []
        candidates = []
        for lst in lists:
            try:
                r = lst.rectangle()
            except Exception:
                continue
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            if not self._is_in_rect(left_rect, cx, cy):
                continue
            if tab_rect and r.top <= tab_rect.bottom + 4:
                continue
            if r.width() < 140 or r.height() < 140:
                continue
            area = r.width() * r.height()
            candidates.append((area, r.top, lst))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2]

    def _left_pane_rect(self, tight: bool = False):
        if self.win is None:
            return None
        try:
            r = self.win.rectangle()
            left = r.left
            width_ratio = 0.30 if tight else 0.38
            right = r.left + int(r.width() * width_ratio)
            top = r.top + int(r.height() * 0.12)
            bottom = r.bottom - int(r.height() * 0.08)
            return (left, top, right, bottom)
        except Exception:
            return None

    def _right_pane_rect(self):
        if self.win is None:
            return None
        try:
            r = self.win.rectangle()
            left = r.left + int(r.width() * 0.42)
            right = r.right
            top = r.top + int(r.height() * 0.12)
            bottom = r.bottom - int(r.height() * 0.08)
            return (left, top, right, bottom)
        except Exception:
            return None

    def _right_pane_rect(self):
        if self.win is None:
            return None
        try:
            r = self.win.rectangle()
            left = r.left + int(r.width() * 0.42)
            right = r.right
            top = r.top + int(r.height() * 0.12)
            bottom = r.bottom - int(r.height() * 0.08)
            return (left, top, right, bottom)
        except Exception:
            return None

    def _is_in_rect(self, rect, x, y) -> bool:
        if rect is None:
            return False
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def _is_rect_within(self, rect, r, pad: int = 0) -> bool:
        if rect is None or r is None:
            return False
        left, top, right, bottom = rect
        return r.left >= (left + pad) and r.right <= (right - pad) and r.top >= (top + pad) and r.bottom <= (bottom - pad)

    def _is_chat_list_item(self, item, min_w: int, min_h: int, max_h: int, ignore_text: List[str]) -> bool:
        if self.win is None:
            return False
        rect = self._left_pane_rect()
        try:
            r = item.rectangle()
        except Exception:
            return False
        cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2
        if not self._is_in_rect(rect, cx, cy):
            return False
        if rect and r.right > rect[2] + 4:
            return False
        if r.width() < min_w or r.height() < min_h or r.height() > max_h:
            return False
        try:
            name = (item.window_text() or "").strip()
        except Exception:
            name = ""
        if name:
            low = safe_lower(name)
            if low in ignore_text:
                return False
            if low.isdigit():
                return False
        return True

    def _capture_region_bgr(self, left: int, top: int, right: int, bottom: int) -> Optional[bytes]:
        width = max(0, right - left)
        height = max(0, bottom - top)
        if width <= 0 or height <= 0:
            return None

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hdc = user32.GetDC(0)
        if not hdc:
            return None
        mdc = gdi32.CreateCompatibleDC(hdc)
        if not mdc:
            user32.ReleaseDC(0, hdc)
            return None
        bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
        if not bmp:
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(0, hdc)
            return None
        gdi32.SelectObject(mdc, bmp)
        gdi32.BitBlt(mdc, 0, 0, width, height, hdc, left, top, 0x00CC0020)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = 0

        buf_len = width * height * 3
        buffer = ctypes.create_string_buffer(buf_len)
        gdi32.GetDIBits(mdc, bmp, 0, height, buffer, ctypes.byref(bmi), 0)

        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, hdc)

        return buffer.raw

    def _region_has_unread_pixel(self, left: int, top: int, right: int, bottom: int, cfg: Dict[str, Any]) -> bool:
        data = self._capture_region_bgr(left, top, right, bottom)
        if not data:
            return False
        width = max(1, right - left)
        height = max(1, bottom - top)
        step = max(1, int(cfg.get("unread_pixel_sample_step", 3)))
        min_b = int(cfg.get("unread_pixel_min_b", 150))
        min_g = int(cfg.get("unread_pixel_min_g", 140))
        max_r = int(cfg.get("unread_pixel_max_r", 130))
        min_b_over_r = int(cfg.get("unread_pixel_min_b_over_r", 40))
        min_b_over_g = int(cfg.get("unread_pixel_min_b_over_g", 10))

        row_stride = width * 3
        for y in range(0, height, step):
            row = y * row_stride
            for x in range(0, width, step):
                i = row + x * 3
                if i + 2 >= len(data):
                    continue
                b = data[i]
                g = data[i + 1]
                r = data[i + 2]
                if b >= min_b and g >= min_g and r <= max_r:
                    if (b - r) >= min_b_over_r and (b - g) >= min_b_over_g:
                        return True
        return False

    def _region_unread_pixel_stats(self, left: int, top: int, right: int, bottom: int, cfg: Dict[str, Any]) -> Dict[str, Any]:
        data = self._capture_region_bgr(left, top, right, bottom)
        if not data:
            return {"capture": False}
        width = max(1, right - left)
        height = max(1, bottom - top)
        step = max(1, int(cfg.get("unread_pixel_sample_step", 3)))
        max_b = 0
        max_g = 0
        min_r = 255
        max_b_over_r = -255
        max_b_over_g = -255
        row_stride = width * 3
        for y in range(0, height, step):
            row = y * row_stride
            for x in range(0, width, step):
                i = row + x * 3
                if i + 2 >= len(data):
                    continue
                b = data[i]
                g = data[i + 1]
                r = data[i + 2]
                if b > max_b:
                    max_b = b
                if g > max_g:
                    max_g = g
                if r < min_r:
                    min_r = r
                bor = b - r
                bog = b - g
                if bor > max_b_over_r:
                    max_b_over_r = bor
                if bog > max_b_over_g:
                    max_b_over_g = bog
        return {
            "capture": True,
            "max_b": int(max_b),
            "max_g": int(max_g),
            "min_r": int(min_r),
            "max_b_over_r": int(max_b_over_r),
            "max_b_over_g": int(max_b_over_g),
            "w": width,
            "h": height,
        }

    def item_has_unread_pixel(self, item, cfg: Dict[str, Any]) -> bool:
        try:
            r = item.rectangle()
        except Exception:
            return False
        top = r.top + 2
        bottom = r.bottom - 2

        left_strip_l = r.left + int(cfg.get("unread_pixel_left_pad", 2))
        left_strip_r = left_strip_l + int(cfg.get("unread_pixel_width", 10))

        right_strip_r = r.right - int(cfg.get("unread_pixel_right_pad", 2))
        right_strip_l = right_strip_r - int(cfg.get("unread_pixel_right_width", 12))

        if self._region_has_unread_pixel(left_strip_l, top, left_strip_r, bottom, cfg):
            return True
        if self._region_has_unread_pixel(right_strip_l, top, right_strip_r, bottom, cfg):
            return True
        return False

    def list_chat_items_fallback(self, min_w: int, min_h: int, max_h: int, ignore_text: List[str]) -> List[object]:
        if self.win is None:
            return []
        try:
            items = self.win.descendants(control_type="ListItem")
        except Exception:
            return []
        out = []
        for it in items:
            try:
                if self._is_chat_list_item(it, min_w, min_h, max_h, ignore_text):
                    out.append(it)
            except Exception:
                continue
        return out

    def list_chat_items_text_fallback(self, ignore_text: List[str], min_w: int, min_h: int, max_h: int) -> List[object]:
        if self.win is None:
            return []
        rect = self._left_pane_rect(tight=True)
        ignore = {safe_lower(x) for x in ignore_text}
        ignore.update({
            "chat", "unread", "channels", "chats", "meeting chats",
            "copilot", "discover", "mentions", "followed threads",
            "favorites", "teams and channels", "apps",
        })
        seen = set()
        out = []
        try:
            texts = self.win.descendants(control_type="Text")
        except Exception:
            return []
        for t in texts:
            try:
                name = (t.window_text() or "").strip()
                if not name:
                    continue
                if safe_lower(name) in ignore:
                    continue
                if name.isdigit():
                    continue
                if len(name) <= 1:
                    continue
                r = t.rectangle()
                if r.width() < min_w or r.height() < min_h or r.height() > max_h:
                    continue
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                if not self._is_in_rect(rect, cx, cy):
                    continue
                if not self._is_rect_within(rect, r, pad=2):
                    continue
                clickable = self._find_clickable_chat_row(t)
                key = (name, r.left, r.top, r.right, r.bottom, getattr(clickable.element_info, "runtime_id", None))
                if key in seen:
                    continue
                seen.add(key)
                out.append(clickable)
            except Exception:
                continue
        return out

    def _find_clickable_chat_row(self, el):
        cur = el
        for _ in range(6):
            try:
                if getattr(cur.element_info, "control_type", None) == "ListItem":
                    return cur
            except Exception:
                pass
            try:
                cur = cur.parent()
            except Exception:
                break
        return el

    def list_chat_items(self, min_w: int, min_h: int, max_h: int, ignore_text: List[str]) -> List[object]:
        chat_list = self._find_chat_list()
        if chat_list is None:
            return []
        try:
            items = chat_list.children(control_type="ListItem")
        except Exception:
            items = []
        out = []
        for it in items:
            try:
                if self._is_chat_list_item(it, min_w, min_h, max_h, ignore_text):
                    out.append(it)
            except Exception:
                continue
        return out

    def dump_list_items_debug(self) -> List[str]:
        if self.win is None:
            return []
        names = []
        try:
            items = self.win.descendants(control_type="ListItem")
            for it in items[:50]:
                try:
                    t = it.window_text().strip()
                    if t:
                        names.append(t)
                except Exception:
                    continue
        except Exception:
            pass
        return names

    def dump_text_items_debug(self) -> List[str]:
        if self.win is None:
            return []
        names = []
        try:
            items = self.win.descendants(control_type="Text")
            for it in items[:80]:
                try:
                    t = it.window_text().strip()
                    if t:
                        names.append(t)
                except Exception:
                    continue
        except Exception:
            pass
        return names

    def _extract_chat_name_from_item_text(self, text: str) -> str:
        s = (text or "").strip()
        if not s:
            return ""
        s = re.sub(r"^begin reference,\s*", "", s, flags=re.IGNORECASE)
        if " by " in s:
            parts = s.rsplit(" by ", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
        return s

    def verify_chat_is_open(self, expected_name: str, max_wait: float = 1.0) -> bool:
        """Verify that the expected chat is actually open and active."""
        if not expected_name:
            return False

        expected_key = safe_lower(normalize_title(expected_name))
        end_time = time.time() + max_wait

        while time.time() < end_time:
            current_title = self.get_chat_title()
            if current_title and not self._is_dateish_title(current_title):
                current_key = safe_lower(normalize_title(current_title))
                if current_key == expected_key:
                    return True
            time.sleep(0.1)

        return False

    def open_chat_item(self, item, prev_title: str = "") -> bool:
        if self.win is None:
            return False
        self.focus()
        prev_key = safe_lower(prev_title)
        item_text = ""
        try:
            item_text = (item.window_text() or "").strip()
        except Exception:
            item_text = ""
        item_name = self._extract_chat_name_from_item_text(item_text)
        item_key = safe_lower(item_name)

        for attempt in range(3):
            try:
                item.click_input()
            except Exception:
                pass
            try:
                if hasattr(item, "select"):
                    item.select()
            except Exception:
                pass
            try:
                if hasattr(item, "invoke"):
                    item.invoke()
            except Exception:
                pass
            try:
                r = item.rectangle()
                x = r.left + max(8, int(r.width() * 0.2))
                y = (r.top + r.bottom) // 2
                self.win.click_input(coords=(x, y))
            except Exception:
                pass
            try:
                self.win.type_keys("{ENTER}")
            except Exception:
                pass

            # Wait longer on first attempt to allow chat to load
            wait_time = 0.6 if attempt == 0 else 0.3
            time.sleep(wait_time)

            # Verify the correct chat opened
            if item_name and self.verify_chat_is_open(item_name, max_wait=0.5):
                return True

            # Check if we got a different valid chat
            title = self.get_chat_title()
            if title and not self._is_dateish_title(title):
                title_key = safe_lower(title)
                if item_key and title_key == item_key:
                    return True
                if not prev_key or title_key != prev_key:
                    return True

        return False

    def _element_strings(self, el) -> List[str]:
        out = []
        try:
            ei = el.element_info
        except Exception:
            ei = None
        if ei is None:
            return out
        for attr in ("name", "automation_id", "class_name", "control_type", "localized_control_type"):
            try:
                val = getattr(ei, attr, None)
            except Exception:
                val = None
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
        try:
            props = ei.get_properties()
        except Exception:
            props = {}
        for key in ("Name", "AutomationId", "ClassName", "ControlType", "LocalizedControlType", "HelpText"):
            try:
                val = props.get(key)
            except Exception:
                val = None
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
        return out

    def _any_unread_match(self, strings: List[str], unread_text_regex: List[str]) -> bool:
        for s in strings:
            for pat in unread_text_regex:
                try:
                    if re.search(pat, s, re.IGNORECASE):
                        return True
                except Exception:
                    continue
        return False

    def is_item_unread(self, item, unread_text_regex: List[str]) -> bool:
        # Best-effort: check for "Unread"/"New message" indicators inside the list item.
        if self._any_unread_match(self._element_strings(item), unread_text_regex):
            return True
        try:
            descendants = item.descendants()
        except Exception:
            descendants = []
        for d in descendants:
            try:
                if self._any_unread_match(self._element_strings(d), unread_text_regex):
                    return True
            except Exception:
                continue
        return False

    def dump_list_item_probe(self, item, limit: int = 40) -> List[str]:
        # Compact diagnostic dump of child elements for unread detection.
        out = []
        try:
            base = self._element_strings(item)
        except Exception:
            base = []
        if base:
            out.append("ITEM: " + " | ".join(base[:6]))
        try:
            descendants = item.descendants()
        except Exception:
            descendants = []
        for d in descendants[:limit]:
            try:
                s = self._element_strings(d)
                if s:
                    out.append("CHILD: " + " | ".join(s[:6]))
            except Exception:
                continue
        return out

    def dump_list_item_properties(self, item, child_limit: int = 12) -> Dict[str, Any]:
        props: Dict[str, Any] = {}
        try:
            ei = item.element_info
        except Exception:
            ei = None
        if ei is not None:
            try:
                props["element"] = {k: v for k, v in ei.get_properties().items() if v not in (None, "", [])}
            except Exception:
                props["element"] = {}
        try:
            sel = item.iface_selection_item
            props["selection"] = {"is_selected": bool(sel.CurrentIsSelected)}
        except Exception:
            pass
        try:
            tog = item.iface_toggle
            props["toggle"] = {"state": int(tog.CurrentToggleState)}
        except Exception:
            pass
        try:
            leg = item.iface_legacy_iaccessible
            props["legacy"] = {
                "state": int(getattr(leg, "CurrentState", 0)),
                "help": getattr(leg, "CurrentHelp", ""),
                "name": getattr(leg, "CurrentName", ""),
            }
        except Exception:
            pass

        children = []
        try:
            descendants = item.descendants()
        except Exception:
            descendants = []
        for d in descendants[:child_limit]:
            try:
                dei = d.element_info
            except Exception:
                dei = None
            if dei is None:
                continue
            try:
                dprops = dei.get_properties()
            except Exception:
                dprops = {}
            slim = {}
            for k in ("Name", "AutomationId", "ClassName", "ControlType", "LocalizedControlType", "HelpText"):
                try:
                    v = dprops.get(k)
                except Exception:
                    v = None
                if isinstance(v, str) and v.strip():
                    slim[k] = v.strip()
            if slim:
                children.append(slim)
        if children:
            props["children"] = children
        return props

    def get_chat_header_text(self) -> str:
        if self.win is None:
            return ""
        try:
            header = self.win.child_window(control_type="Group", title_re=".*Chat.*")
            return header.window_text()
        except Exception:
            return ""

    def get_participants_count(self) -> Optional[int]:
        if self.win is None:
            return None
        try:
            btn = self.win.child_window(title_re=r".*Participants.*", control_type="Button")
            txt = btn.window_text()
            m = re.search(r"(\d+)", txt)
            if m:
                return int(m.group(1))
        except Exception:
            return None
        return None

    def is_dm_header(self, title: str, group_title_regex: List[str]) -> bool:
        # Best-effort: use header + title heuristics to avoid group chats.
        header = safe_lower(self.get_chat_header_text())
        if "channel" in header or "team" in header:
            return False
        for pat in group_title_regex:
            try:
                if re.search(pat, title, re.IGNORECASE):
                    return False
            except Exception:
                continue
        pcount = self.get_participants_count()
        if pcount is not None and pcount >= 3:
            return False
        return True

    def get_chat_title(self) -> str:
        if self.win is None:
            return ""
        try:
            wt = (self.win.window_text() or "").strip()
        except Exception:
            wt = ""
        if wt:
            m = re.search(r"\bChat\s*\|\s*(.*?)\s*\|\s*Microsoft Teams", wt, re.IGNORECASE)
            if m:
                return m.group(1).strip()
            parts = [p.strip() for p in wt.split("|") if p.strip()]
            if parts:
                filtered = []
                for p in parts:
                    lp = safe_lower(p)
                    if lp == "chat":
                        continue
                    if "microsoft teams" in lp:
                        continue
                    filtered.append(p)
                if filtered:
                    return filtered[0]
        try:
            r = self.win.rectangle()
        except Exception:
            r = None
        ignore = {
            "chat", "shared", "storyline", "search",
            "calendar", "calls", "activity",
        }
        try:
            texts = self.win.descendants(control_type="Text")
        except Exception:
            return ""
        candidates = []
        for t in texts:
            try:
                name = (t.window_text() or "").strip()
                if not name:
                    continue
                if safe_lower(name) in ignore:
                    continue
                if self._is_dateish_title(name):
                    continue
                tr = t.rectangle()
                if r:
                    if tr.top < r.top + int(r.height() * 0.05) or tr.top > r.top + int(r.height() * 0.22):
                        continue
                    if tr.left < r.left + int(r.width() * 0.22):
                        continue
                if 2 <= len(name) <= 60:
                    candidates.append((tr.top, name))
            except Exception:
                continue
        if not candidates:
            return ""
        candidates.sort()
        return candidates[0][1]

    def _is_dateish_title(self, name: str) -> bool:
        if not name:
            return False
        s = safe_lower(name)
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        months = ["january", "february", "march", "april", "may", "june", "july", "august",
                  "september", "october", "november", "december"]
        for w in weekdays:
            if s.startswith(w):
                return True
        for m in months:
            if s.startswith(m):
                return True
        if re.search(r"\b(?:am|pm)\b", s) and re.search(r"\d", s):
            return True
        if re.search(r"\b\d{1,2}/\d{1,2}\b", s):
            return True
        return False

    def is_chat_view_active(self) -> bool:
        if self.win is None:
            return False
        try:
            if self.get_unread_tab_rect() is not None:
                return True
        except Exception:
            pass
        try:
            if self._find_chat_list() is not None:
                return True
        except Exception:
            pass
        try:
            wt = (self.win.window_text() or "").strip()
        except Exception:
            wt = ""
        if wt and re.search(r"\bchat\b", wt, re.IGNORECASE):
            return True
        return False

    def get_last_message_text(self) -> str:
        if self.win is None:
            return ""
        try:
            msg_list = self.win.child_window(title_re=".*Conversation.*", control_type="List")
            items = msg_list.children(control_type="ListItem")
            for it in reversed(items):
                try:
                    txt = it.window_text()
                    if txt.strip():
                        return txt.strip()
                except Exception:
                    continue
        except Exception:
            pass
        # Fallback: scan text elements in the right pane, pick lowest on screen
        try:
            r = self.win.rectangle()
        except Exception:
            r = None
        candidates = []
        try:
            texts = self.win.descendants(control_type="Text")
        except Exception:
            return ""
        for t in texts:
            try:
                name = (t.window_text() or "").strip()
                if not name:
                    continue
                tr = t.rectangle()
                if r:
                    if tr.left < r.left + int(r.width() * 0.45):
                        continue
                    if tr.top < r.top + int(r.height() * 0.2):
                        continue
                if len(name) < 2:
                    continue
                candidates.append((tr.bottom, name))
            except Exception:
                continue
        if not candidates:
            return ""
        candidates.sort()
        return candidates[-1][1]

    def chat_has_unread_marker(self, unread_text_regex: List[str]) -> bool:
        if self.win is None:
            return False
        rect = self._right_pane_rect()
        try:
            texts = self.win.descendants(control_type="Text")
        except Exception:
            return False
        for t in texts:
            try:
                name = (t.window_text() or "").strip()
                if not name:
                    continue
                tr = t.rectangle()
                cx = (tr.left + tr.right) // 2
                cy = (tr.top + tr.bottom) // 2
                if not self._is_in_rect(rect, cx, cy):
                    continue
                for pat in unread_text_regex:
                    try:
                        if re.search(pat, name, re.IGNORECASE):
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def wait_for_unread_marker(self, unread_text_regex: List[str], timeout_s: float, poll_s: float) -> bool:
        end = time.time() + max(0.1, float(timeout_s))
        poll = max(0.05, float(poll_s))
        while time.time() < end:
            if self.chat_has_unread_marker(unread_text_regex):
                return True
            time.sleep(poll)
        return False

    def find_compose_box(self):
        """Find the compose box Edit control in the right pane."""
        if self.win is None:
            return None
        rect = self._right_pane_rect()
        try:
            edits = self.win.descendants(control_type="Edit")
        except Exception:
            return None

        candidates = []
        for edit in edits:
            try:
                r = edit.rectangle()
                # Check if in right pane
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                if not self._is_in_rect(rect, cx, cy):
                    continue
                # Prefer edit boxes in the lower portion (compose area)
                if rect:
                    vertical_pos = (r.top - rect[1]) / max(1, rect[3] - rect[1])
                    if vertical_pos > 0.6:  # In lower 40% of right pane
                        candidates.append((vertical_pos, edit))
            except Exception:
                continue

        if not candidates:
            # Fallback: try any Edit control in right pane
            for edit in edits:
                try:
                    r = edit.rectangle()
                    cx = (r.left + r.right) // 2
                    cy = (r.top + r.bottom) // 2
                    if self._is_in_rect(rect, cx, cy):
                        return edit
                except Exception:
                    continue
            return None

        # Return the edit box that's lowest on screen
        candidates.sort(reverse=True)
        return candidates[0][1]

    def focus_compose_box(self) -> bool:
        """Focus the compose box. Returns True if successful."""
        # Try shortcut first (Ctrl+R for compose)
        self.send_keys("^r")
        time.sleep(0.3)

        # Try to find and click the compose box directly
        compose = self.find_compose_box()
        if compose:
            try:
                compose.set_focus()
                time.sleep(0.1)
                return True
            except Exception:
                pass
            try:
                compose.click_input()
                time.sleep(0.1)
                return True
            except Exception:
                pass

        # Fallback: try clicking near bottom of right pane
        try:
            rect = self._right_pane_rect()
            if rect:
                # Click in lower portion of right pane where compose usually is
                x = rect[0] + (rect[2] - rect[0]) // 2
                y = rect[3] - 80  # 80 pixels from bottom
                self.win.click_input(coords=(x, y))
                time.sleep(0.2)
                return True
        except Exception:
            pass

        return False

    def type_reply(self, text: str) -> bool:
        """Type reply text into the compose box. Returns True if successful."""
        if self.win is None:
            return False

        # Find the compose box and verify we can type into it
        compose = self.find_compose_box()
        if compose:
            try:
                # Try typing directly into the compose box element
                compose.type_keys(text, with_spaces=True)
                return True
            except Exception:
                pass

        # Fallback: type into the window (if focus is already on compose)
        try:
            self.win.type_keys(text, with_spaces=True, set_foreground=True)
            return True
        except Exception:
            return False

    def advance_after_draft(self, keys: str, delay_s: float) -> None:
        if not keys:
            return
        self.send_keys(keys)
        time.sleep(max(0.1, float(delay_s)))


def should_block_chat(chat_name: str, rules: Dict[str, Any]) -> bool:
    for pat in rules.get("blocked_chat_name_regex", []):
        try:
            if re.search(pat, chat_name, re.IGNORECASE):
                return True
        except Exception:
            continue
    return False


def process_pass(ui: TeamsUI, cfg: Dict[str, Any]) -> None:
    rules = cfg
    debug = bool(cfg.get("debug", True))
    processed_titles: set = set()

    if bool(cfg.get("keyboard_cycle_unread", False)):
        ui.focus()
        ui.open_unread_chats()
        unread_filter_active = ui.is_unread_filter_active() or ui.assume_unread_filter
        if debug:
            log_jsonl(ACTION_LOG, {"event": "unread_filter_active", "active": unread_filter_active})

        processed = 0
        max_chats = int(cfg.get("max_chats_per_pass", 15))
        for i in range(max_chats):
            if i > 0:
                ui.send_keys(cfg.get("keyboard_next_chat_keys", "^{DOWN}"))
                time.sleep(float(cfg.get("keyboard_next_chat_delay", 0.3)))
            if not ui.is_chat_view_active():
                ui.open_unread_chats()
                time.sleep(0.4)
            if not ui.is_chat_view_active():
                try:
                    wt = ui.win.window_text() if ui.win is not None else ""
                except Exception:
                    wt = ""
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_not_chat_view", "window": wt})
                continue

            chat_name = ui.get_chat_title()
            if not chat_name:
                log_jsonl(ACTION_LOG, {"event": "chat_title_missing"})
                continue
            if ui._is_dateish_title(chat_name):
                try:
                    wt = ui.win.window_text() if ui.win is not None else ""
                except Exception:
                    wt = ""
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_bad_title", "chat": chat_name, "window": wt})
                continue
            if safe_lower(chat_name) in set([safe_lower(x) for x in cfg.get("blocked_chat_title_exact", [])]):
                log_jsonl(ACTION_LOG, {"event": "chat_blocked_title_exact", "chat": chat_name})
                continue
            title_key = safe_lower(chat_name)
            if bool(cfg.get("dedupe_chats_per_pass", True)) and title_key in processed_titles:
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_duplicate_in_pass", "chat": chat_name})
                continue
            processed_titles.add(title_key)
            min_gap = int(cfg.get("min_seconds_between_drafts_per_chat", 0))
            if min_gap > 0:
                last_ts = CHAT_LAST_DRAFT_TS.get(title_key)
                if last_ts and (time.time() - last_ts) < min_gap:
                    log_jsonl(ACTION_LOG, {"event": "chat_skipped_cooldown", "chat": chat_name})
                    continue

            if should_block_chat(chat_name, rules):
                log_jsonl(ACTION_LOG, {"event": "chat_blocked", "chat": chat_name})
                continue

            if bool(cfg.get("dm_only", True)):
                if not ui.is_dm_header(chat_name, cfg.get("group_chat_title_regex", [])):
                    try:
                        wt = ui.win.window_text() if ui.win is not None else ""
                    except Exception:
                        wt = ""
                    log_jsonl(ACTION_LOG, {"event": "chat_skipped_non_dm", "chat": chat_name, "window": wt})
                    continue

            last_message = ui.get_last_message_text()
            if not last_message:
                log_jsonl(ACTION_LOG, {"event": "chat_no_message", "chat": chat_name})
                continue

            try:
                if bool(cfg.get("llm_decide_reply", True)):
                    reply = generate_reply_or_no_reply_ollama(
                        host=cfg.get("ollama_host"),
                        model=cfg.get("ollama_model"),
                        timeout_s=int(cfg.get("ollama_timeout_seconds", 180)),
                        sender=chat_name or "Unknown",
                        last_message=last_message,
                        reply_style=cfg.get("reply_style", ""),
                        decide_prompt=cfg.get("llm_decide_reply_prompt", ""),
                    )
                else:
                    reply = generate_reply_ollama(
                        host=cfg.get("ollama_host"),
                        model=cfg.get("ollama_model"),
                        timeout_s=int(cfg.get("ollama_timeout_seconds", 180)),
                        sender=chat_name or "Unknown",
                        last_message=last_message,
                        reply_style=cfg.get("reply_style", ""),
                    )
            except Exception as e:
                log_jsonl(ACTION_LOG, {"event": "ollama_error", "chat": chat_name, "error": str(e)})
                if debug:
                    print(f"DEBUG: Ollama error: {e}")
                continue

            if reply:
                if reply.strip().upper().startswith("NO_REPLY"):
                    log_jsonl(ACTION_LOG, {"event": "chat_skipped_llm_no_reply", "chat": chat_name})
                    continue

                # Try to focus compose box and type reply
                if not ui.focus_compose_box():
                    log_jsonl(ACTION_LOG, {"event": "compose_box_focus_failed", "chat": chat_name})
                    if debug:
                        print(f"DEBUG: Failed to focus compose box for {chat_name}")
                    continue

                if not ui.type_reply(reply):
                    log_jsonl(ACTION_LOG, {"event": "type_reply_failed", "chat": chat_name})
                    if debug:
                        print(f"DEBUG: Failed to type reply for {chat_name}")
                    continue

                log_jsonl(ACTION_LOG, {"event": "draft_typed", "chat": chat_name, "preview": reply[:300]})
                processed += 1
                CHAT_LAST_DRAFT_TS[title_key] = time.time()

        ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] chats_processed={processed}")
        return

    ui.focus()
    ui.open_unread_chats()
    unread_filter_active = ui.is_unread_filter_active() or ui.assume_unread_filter
    if debug:
        log_jsonl(ACTION_LOG, {"event": "unread_filter_active", "active": unread_filter_active})

    min_w = int(cfg.get("chat_list_min_width", 160))
    min_h = int(cfg.get("chat_list_min_height", 28))
    max_h = int(cfg.get("chat_list_max_height", 120))
    ignore_text = [safe_lower(x) for x in cfg.get("chat_list_ignore_text", [])]

    items = ui.list_chat_items(min_w, min_h, max_h, ignore_text)
    if not items:
        items = ui.list_chat_items_fallback(min_w, min_h, max_h, ignore_text)
    if not items and bool(cfg.get("allow_text_fallback", False)):
        items = ui.list_chat_items_text_fallback(ignore_text, min_w, min_h, max_h)
    if debug:
        print(f"DEBUG: unread chat items={len(items)}")
    if not items:
        log_jsonl(ACTION_LOG, {"event": "no_unread_chats"})
        if bool(cfg.get("debug_dump_on_empty", True)) and debug:
            log_jsonl(ACTION_LOG, {"event": "debug_listitems", "items": ui.dump_list_items_debug()})
            log_jsonl(ACTION_LOG, {"event": "debug_textitems", "items": ui.dump_text_items_debug()})
        return

    processed = 0

    if debug and bool(cfg.get("debug_dump_list_item_props", True)):
        for item in items[: int(cfg.get("debug_dump_list_item_props_limit", 8))]:
            try:
                log_jsonl(ACTION_LOG, {"event": "list_item_props", "props": ui.dump_list_item_properties(item)})
            except Exception:
                continue

    if debug and bool(cfg.get("debug_dump_unread_pixel_stats", True)) and bool(cfg.get("unread_pixel_enabled", True)):
        for item in items[: int(cfg.get("debug_dump_unread_pixel_stats_limit", 6))]:
            try:
                r = item.rectangle()
                top = r.top + 2
                bottom = r.bottom - 2
                left_strip_l = r.left + int(cfg.get("unread_pixel_left_pad", 2))
                left_strip_r = left_strip_l + int(cfg.get("unread_pixel_width", 10))
                right_strip_r = r.right - int(cfg.get("unread_pixel_right_pad", 2))
                right_strip_l = right_strip_r - int(cfg.get("unread_pixel_right_width", 12))
                stats_left = ui._region_unread_pixel_stats(left_strip_l, top, left_strip_r, bottom, cfg)
                stats_right = ui._region_unread_pixel_stats(right_strip_l, top, right_strip_r, bottom, cfg)
                log_jsonl(
                    ACTION_LOG,
                    {
                        "event": "unread_pixel_stats",
                        "chat": (item.window_text() or "").strip(),
                        "left": stats_left,
                        "right": stats_right,
                    },
                )
            except Exception:
                continue

    process_all_chats = bool(cfg.get("process_all_chats", True))
    require_item_unread = (not process_all_chats) and bool(cfg.get("require_unread_indicator", True)) and not unread_filter_active
    if require_item_unread:
        any_unread = False
        for item in items:
            try:
                if ui.is_item_unread(item, cfg.get("unread_text_regex", [])):
                    any_unread = True
                    break
            except Exception:
                continue
        if not any_unread:
            log_jsonl(ACTION_LOG, {"event": "unread_indicator_absent", "note": "disabling item unread gate for this pass"})
            require_item_unread = False

    for item in items[: int(cfg.get("max_chats_per_pass", 15))]:
        if require_item_unread:
            if not ui.is_item_unread(item, cfg.get("unread_text_regex", [])):
                if debug:
                    probe = []
                    if bool(cfg.get("debug_dump_unread_probe", True)):
                        probe = ui.dump_list_item_probe(item)
                    log_jsonl(
                        ACTION_LOG,
                        {
                            "event": "chat_skipped_read",
                            "chat": (item.window_text() or "").strip(),
                            "probe": probe[:60],
                        },
                    )
                continue
        if (not process_all_chats) and bool(cfg.get("unread_pixel_enabled", True)):
            try:
                if not ui.item_has_unread_pixel(item, cfg):
                    if debug:
                        log_jsonl(ACTION_LOG, {"event": "chat_skipped_no_unread_pixel", "chat": (item.window_text() or "").strip()})
                    continue
            except Exception as e:
                if debug:
                    log_jsonl(ACTION_LOG, {"event": "unread_pixel_error", "error": str(e)})
        prev_title = ui.get_chat_title()
        try:
            item_text = (item.window_text() or "").strip()
        except Exception:
            item_text = ""
        item_name = ui._extract_chat_name_from_item_text(item_text)
        prev_key = safe_lower(normalize_title(prev_title))
        item_key = safe_lower(normalize_title(item_name))
        if item_name.isdigit():
            log_jsonl(ACTION_LOG, {"event": "chat_skipped_bad_item", "chat": item_text})
            continue
        if not (item_key and prev_key and item_key == prev_key):
            if not ui.open_chat_item(item, prev_title=prev_title):
                log_jsonl(
                    ACTION_LOG,
                    {
                        "event": "chat_open_failed",
                        "chat": item_text,
                        "item_name": item_name,
                        "current_title": prev_title,
                    },
                )
                continue
        if not ui.is_chat_view_active():
            try:
                wt = ui.win.window_text() if ui.win is not None else ""
            except Exception:
                wt = ""
            log_jsonl(ACTION_LOG, {"event": "chat_skipped_not_chat_view", "window": wt})
            continue
        if (not process_all_chats) and bool(cfg.get("require_unread_in_chat", True)):
            if not ui.wait_for_unread_marker(
                cfg.get("unread_in_chat_text_regex", []),
                float(cfg.get("unread_in_chat_wait_seconds", 3.0)),
                float(cfg.get("unread_in_chat_poll_seconds", 0.2)),
            ):
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_no_unread_marker", "chat": ui.get_chat_title()})
                continue

        chat_name = ui.get_chat_title()
        if not chat_name:
            log_jsonl(ACTION_LOG, {"event": "chat_title_missing"})
            continue
        if ui._is_dateish_title(chat_name):
            try:
                wt = ui.win.window_text() if ui.win is not None else ""
            except Exception:
                wt = ""
            log_jsonl(ACTION_LOG, {"event": "chat_skipped_bad_title", "chat": chat_name, "window": wt})
            continue
        if safe_lower(chat_name) in set([safe_lower(x) for x in cfg.get("blocked_chat_title_exact", [])]):
            log_jsonl(ACTION_LOG, {"event": "chat_blocked_title_exact", "chat": chat_name})
            continue
        title_key = safe_lower(chat_name)
        if bool(cfg.get("dedupe_chats_per_pass", True)) and title_key in processed_titles:
            log_jsonl(ACTION_LOG, {"event": "chat_skipped_duplicate_in_pass", "chat": chat_name})
            continue
        processed_titles.add(title_key)
        min_gap = int(cfg.get("min_seconds_between_drafts_per_chat", 0))
        if min_gap > 0:
            last_ts = CHAT_LAST_DRAFT_TS.get(title_key)
            if last_ts and (time.time() - last_ts) < min_gap:
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_cooldown", "chat": chat_name})
                continue
        if should_block_chat(chat_name, rules):
            log_jsonl(ACTION_LOG, {"event": "chat_blocked", "chat": chat_name})
            continue

        if bool(cfg.get("dm_only", True)):
            if not ui.is_dm_header(chat_name, cfg.get("group_chat_title_regex", [])):
                try:
                    wt = ui.win.window_text() if ui.win is not None else ""
                except Exception:
                    wt = ""
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_non_dm", "chat": chat_name, "window": wt})
                continue

        last_message = ui.get_last_message_text()
        if not last_message:
            log_jsonl(ACTION_LOG, {"event": "chat_no_message", "chat": chat_name})
            continue

        try:
            if bool(cfg.get("llm_decide_reply", True)):
                reply = generate_reply_or_no_reply_ollama(
                    host=cfg.get("ollama_host"),
                    model=cfg.get("ollama_model"),
                    timeout_s=int(cfg.get("ollama_timeout_seconds", 180)),
                    sender=chat_name or "Unknown",
                    last_message=last_message,
                    reply_style=cfg.get("reply_style", ""),
                    decide_prompt=cfg.get("llm_decide_reply_prompt", ""),
                )
            else:
                reply = generate_reply_ollama(
                    host=cfg.get("ollama_host"),
                    model=cfg.get("ollama_model"),
                    timeout_s=int(cfg.get("ollama_timeout_seconds", 180)),
                    sender=chat_name or "Unknown",
                    last_message=last_message,
                    reply_style=cfg.get("reply_style", ""),
                )
        except Exception as e:
            log_jsonl(ACTION_LOG, {"event": "ollama_error", "chat": chat_name, "error": str(e)})
            if debug:
                print(f"DEBUG: Ollama error: {e}")
            continue

        if reply:
            if reply.strip().upper().startswith("NO_REPLY"):
                log_jsonl(ACTION_LOG, {"event": "chat_skipped_llm_no_reply", "chat": chat_name})
                continue

            # Try to focus compose box and type reply
            if not ui.focus_compose_box():
                log_jsonl(ACTION_LOG, {"event": "compose_box_focus_failed", "chat": chat_name})
                if debug:
                    print(f"DEBUG: Failed to focus compose box for {chat_name}")
                continue

            if not ui.type_reply(reply):
                log_jsonl(ACTION_LOG, {"event": "type_reply_failed", "chat": chat_name})
                if debug:
                    print(f"DEBUG: Failed to type reply for {chat_name}")
                continue

            log_jsonl(ACTION_LOG, {"event": "draft_typed", "chat": chat_name, "preview": reply[:300]})
            processed += 1
            CHAT_LAST_DRAFT_TS[title_key] = time.time()
            if bool(cfg.get("advance_after_draft", True)):
                ui.advance_after_draft(
                    cfg.get("advance_after_draft_keys", "^{DOWN}"),
                    float(cfg.get("advance_after_draft_delay", 0.3)),
                )

    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] chats_processed={processed}")


def main():
    config_path = os.path.join(BASE_DIR, "teams_bot.config.json")
    cfg = load_config(config_path)

    print("Starting Teams DM Bot (draft-only)...")
    print("NOTE: This does NOT send. It only types replies.")

    if requests is None:
        print("ERROR: requests is not installed. Run: pip install requests")
        sys.exit(1)

    lock = acquire_single_instance_lock(bool(cfg.get("single_instance_lock", True)), bool(cfg.get("debug", True)))
    if cfg.get("single_instance_lock", True) and lock is None:
        print("Another Teams Bot instance is already running. Exiting.")
        sys.exit(1)

    ui = TeamsUI(debug=bool(cfg.get("debug", True)))
    backoff = int(cfg.get("error_backoff_seconds", 15))

    while True:
        try:
            if bool(cfg.get("reload_config_each_pass", True)):
                cfg = load_config(config_path)

            if not ui.connect():
                time.sleep(backoff)
                backoff = min(int(cfg.get("max_backoff_seconds", 300)), backoff * 2)
                continue

            process_pass(ui, cfg)
            backoff = int(cfg.get("error_backoff_seconds", 15))
        except KeyboardInterrupt:
            print("Stopping (Ctrl+C).")
            sys.exit(0)
        except Exception as e:
            ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] ERROR: {e}")
            log_jsonl(ACTION_LOG, {"event": "process_error", "error": str(e)})
            time.sleep(backoff)
            backoff = min(int(cfg.get("max_backoff_seconds", 300)), backoff * 2)

        time.sleep(int(cfg.get("loop_seconds", 45)))


if __name__ == "__main__":
    main()
