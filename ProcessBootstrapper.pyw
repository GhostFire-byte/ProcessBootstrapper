# -*- coding: utf-8 -*-
"""
prank_gui.py

A local, single-machine utility for YOUR OWN laptop.

Press INSERT to pop up a GUI listing your own running processes.
- Top group: processes with a visible window (you can drop a "GOTCHA" banner
  over their window, or suspend/resume/kill them).
- Bottom group: processes with no visible window (background/system-ish
  processes) -- nothing to overlay, but you can still suspend/resume/kill.

Requires (Windows only), no new libraries beyond what you already installed:
    pip install psutil pywin32 keyboard
(webbrowser is part of the Python standard library -- nothing to install.)

Written for Python 3.5.1 syntax (no f-strings, no 3.6+-only features).

This only touches processes ON THE MACHINE IT RUNS ON. It does not send
anything over a network and does not target other computers.
"""

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser  # stdlib, no install needed -- used for the Chrome tab opener

try:
    import psutil
except ImportError:
    print("Missing dependency: pip install psutil")
    sys.exit(1)

try:
    import win32gui
    import win32process
    import win32con
    import win32api
except ImportError:
    print("Missing dependency: pip install pywin32")
    sys.exit(1)

try:
    import keyboard  # global hotkey listener
except ImportError:
    print("Missing dependency: pip install keyboard")
    sys.exit(1)

try:
    import GPUtil  # optional -- GPU usage. pip install gputil
    HAS_GPUTIL = True
except ImportError:
    HAS_GPUTIL = False

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError:
    print("tkinter not available in this Python install.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Palette / style constants
# ---------------------------------------------------------------------------

BG_DARK = "#1e1f26"
BG_PANEL = "#262832"
BG_ROW_EVEN = "#2b2d38"
BG_ROW_ODD = "#262832"
BG_ROW_NOWIN = "#232430"
FG_TEXT = "#e8e8ec"
FG_DIM = "#9a9aa5"
ACCENT = "#6c8cff"
ACCENT_HOVER = "#8aa4ff"
DANGER = "#ff5c5c"
WARN = "#ffb454"
OK_GREEN = "#4cd672"
FONT_UI = ("Segoe UI", 9)
FONT_UI_BOLD = ("Segoe UI", 9, "bold")
FONT_HEADER = ("Segoe UI", 12, "bold")


# ---------------------------------------------------------------------------
# Process / window helpers
# ---------------------------------------------------------------------------

def get_visible_windows_by_pid():
    """
    Returns a dict: pid -> list of (hwnd, title) for visible, titled top
    level windows belonging to that pid.
    """
    result = {}

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        result.setdefault(pid, []).append((hwnd, title))

    win32gui.EnumWindows(enum_handler, None)
    return result


def list_processes():
    """
    Returns two lists of dicts: (with_window, without_window)
    Each dict includes pid, name, cpu percent, memory MB, and (for windowed
    processes) hwnd + title.
    """
    win_map = get_visible_windows_by_pid()

    with_window = []
    without_window = []

    for proc in psutil.process_iter(["pid", "name"]):
        pid = proc.info["pid"]
        name = proc.info["name"] or "?"

        try:
            cpu = proc.cpu_percent(interval=None)
        except Exception:
            cpu = 0.0
        try:
            mem_mb = proc.memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            mem_mb = 0.0

        if pid in win_map:
            hwnd, title = win_map[pid][0]
            try:
                hung = bool(win32gui.IsHungAppWindow(hwnd))
            except Exception:
                hung = False
            with_window.append({
                "pid": pid, "name": name, "cpu": cpu, "mem": mem_mb,
                "hwnd": hwnd, "title": title, "hung": hung,
            })
        else:
            without_window.append({
                "pid": pid, "name": name, "cpu": cpu, "mem": mem_mb,
            })

    with_window.sort(key=lambda d: d["name"].lower())
    without_window.sort(key=lambda d: d["name"].lower())
    return with_window, without_window


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def suspend_pid(pid):
    try:
        p = psutil.Process(pid)
        p.suspend()
        return True, "Suspended {0} (pid {1})".format(p.name(), pid)
    except Exception as e:
        return False, str(e)


def resume_pid(pid):
    try:
        p = psutil.Process(pid)
        p.resume()
        return True, "Resumed {0} (pid {1})".format(p.name(), pid)
    except Exception as e:
        return False, str(e)


def kill_pid(pid):
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.terminate()
        try:
            p.wait(timeout=3)
        except psutil.TimeoutExpired:
            p.kill()
        return True, "Killed {0} (pid {1})".format(name, pid)
    except Exception as e:
        return False, str(e)


def crash_pid(pid):
    """
    Immediate, forceful termination -- no graceful terminate/wait like
    kill_pid does. Skips straight to the hard kill (equivalent to
    TerminateProcess with a nonzero exit code). The process dies instantly
    with no chance to clean up/save/prompt, which is closer to what people
    mean by "crash" than a polite terminate.
    """
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.kill()  # SIGKILL-equivalent hard kill via TerminateProcess on Windows
        return True, "Crashed {0} (pid {1})".format(name, pid)
    except Exception as e:
        return False, str(e)


def minimize_hwnd(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return True, "Minimized window."
    except Exception as e:
        return False, str(e)


def maximize_hwnd(hwnd):
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return True, "Maximized window."
    except Exception as e:
        return False, str(e)


def flash_hwnd(hwnd, count=6):
    """
    Flashes the target window's taskbar icon -- a lightweight, harmless
    attention-grabber (like an IM notification flash).
    """
    try:
        win32gui.FlashWindow(hwnd, True)
        return True, "Flashed window."
    except Exception as e:
        return False, str(e)


def show_gotcha_banner(hwnd, title, message="GOTCHA!", duration_ms=4000):
    """
    Places a small always-on-top borderless Tk window centered over the
    target window's rectangle, then auto-closes it after duration_ms.
    """
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception as e:
        messagebox.showerror("Error", "Could not get window rect: {0}".format(e))
        return

    left, top, right, bottom = rect
    target_w = max(right - left, 1)
    target_h = max(bottom - top, 1)

    banner_w = min(520, target_w)
    banner_h = 160
    x = left + (target_w - banner_w) // 2
    y = top + (target_h - banner_h) // 2

    banner = tk.Toplevel()
    banner.overrideredirect(True)
    banner.attributes("-topmost", True)
    banner.geometry("{0}x{1}+{2}+{3}".format(banner_w, banner_h, x, y))
    banner.configure(bg=DANGER, highlightthickness=2, highlightbackground="white")

    label = tk.Label(
        banner, text=message, font=("Segoe UI", 28, "bold"),
        fg="white", bg=DANGER, wraplength=banner_w - 20,
    )
    label.pack(expand=True, fill="both")

    sub = tk.Label(
        banner, text="(over: {0}) -- click to dismiss".format(title),
        font=("Segoe UI", 9), fg="white", bg=DANGER,
    )
    sub.pack(pady=(0, 8))

    banner.bind("<Button-1>", lambda e: banner.destroy())
    label.bind("<Button-1>", lambda e: banner.destroy())
    sub.bind("<Button-1>", lambda e: banner.destroy())

    banner.after(duration_ms, lambda: banner.destroy() if banner.winfo_exists() else None)


def _decode(b):
    if b is None:
        return ""
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:
        try:
            return b.decode("cp437", errors="replace")
        except Exception:
            return str(b)


def sha256_of_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        return "(error: {0})".format(e)


# --- digital signature check via WinVerifyTrust (wintrust.dll) -------------
# This is the standard native Windows API for checking Authenticode
# signatures; no extra library needed.

class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", ctypes.c_uint32),
        ("pcwszFilePath", ctypes.c_wchar_p),
        ("hFile", ctypes.c_void_p),
        ("pgKnownSubject", ctypes.c_void_p),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", ctypes.c_uint32),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", ctypes.c_uint32),
        ("fdwRevocationChecks", ctypes.c_uint32),
        ("dwUnionChoice", ctypes.c_uint32),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", ctypes.c_uint32),
        ("hWVTStateData", ctypes.c_void_p),
        ("pwszURLReference", ctypes.c_wchar_p),
        ("dwProvFlags", ctypes.c_uint32),
        ("dwUIContext", ctypes.c_uint32),
    ]


def check_digital_signature(path):
    """
    Returns a short human-readable string: 'Signed (valid)', 'Not signed',
    'Signature invalid', or '(check unavailable)' if the API call fails.
    """
    try:
        WINTRUST_ACTION_GENERIC_VERIFY_V2 = _GUID(
            0x00AAC56B, 0xCD44, 0x11d0,
            (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
        )

        file_info = _WINTRUST_FILE_INFO()
        file_info.cbStruct = ctypes.sizeof(_WINTRUST_FILE_INFO)
        file_info.pcwszFilePath = path
        file_info.hFile = None
        file_info.pgKnownSubject = None

        data = _WINTRUST_DATA()
        ctypes.memset(ctypes.byref(data), 0, ctypes.sizeof(data))
        data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
        data.dwUIChoice = 2  # WTD_UI_NONE
        data.fdwRevocationChecks = 0  # WTD_REVOKE_NONE
        data.dwUnionChoice = 1  # WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(file_info)
        data.dwStateAction = 0  # WTD_STATEACTION_IGNORE
        data.dwProvFlags = 0x00000010  # WTD_CACHE_ONLY_URL_RETRIEVAL (safe default)

        wintrust = ctypes.windll.wintrust
        result = wintrust.WinVerifyTrust(
            None, ctypes.byref(WINTRUST_ACTION_GENERIC_VERIFY_V2), ctypes.byref(data)
        )

        if result == 0:
            return "Signed (valid)"
        elif result == 2148204800:  # TRUST_E_NOSIGNATURE (0x800B0100), signed value can appear negative
            return "Not signed"
        else:
            return "Signature invalid/untrusted (code {0})".format(result)
    except Exception as e:
        return "(check unavailable: {0})".format(e)


def get_gpu_usage_summary():
    """Returns a short string summarizing GPU load, or a note if unavailable."""
    if not HAS_GPUTIL:
        return "GPUtil not installed (pip install gputil) -- GPU monitoring unavailable."
    try:
        gpus = GPUtil.getGPUs()
        if not gpus:
            return "No GPUs detected by GPUtil."
        lines = []
        for gpu in gpus:
            lines.append("{0}: load {1:.0f}%  mem {2:.0f}/{3:.0f} MB".format(
                gpu.name, gpu.load * 100, gpu.memoryUsed, gpu.memoryTotal))
        return "\n".join(lines)
    except Exception as e:
        return "GPU query failed: {0}".format(e)


def restart_pid(pid):
    """Kills the process, then relaunches it using its saved exe path + args."""
    try:
        p = psutil.Process(pid)
        exe = p.exe()
        cmdline = p.cmdline()
        cwd = p.cwd()
    except Exception as e:
        return False, "Could not read process info before restart: {0}".format(e)

    ok, msg = kill_pid(pid)
    if not ok:
        return False, "Restart failed at kill step: {0}".format(msg)

    time.sleep(1)

    try:
        launch_cmd = cmdline if cmdline else [exe]
        subprocess.Popen(launch_cmd, cwd=cwd)
        return True, "Restarted {0}".format(exe)
    except Exception as e:
        return False, "Killed but relaunch failed: {0}".format(e)



def open_chrome_tab(url):
    """
    Opens `url` in a new Chrome tab using the stdlib webbrowser module.
    Tries to specifically target Chrome; falls back to the OS default
    browser if Chrome isn't registered.
    """
    if not url.strip():
        return False, "Enter a URL first."

    if "://" not in url:
        url = "https://" + url

    try:
        try:
            chrome = webbrowser.get("chrome")
        except webbrowser.Error:
            chrome = None

        if chrome is not None:
            chrome.open_new_tab(url)
            return True, "Opened in Chrome: {0}".format(url)
        else:
            webbrowser.open_new_tab(url)
            return True, "Chrome not registered with Python; opened in default browser: {0}".format(url)
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class ProcessPranker(object):
    def __init__(self, root):
        self.root = root
        self.root.title("Local Process Tool")
        self.root.minsize(640, 480)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG_DARK)

        self.favorites = set()
        self.pid_to_window = {}
        self.pid_to_all_windows = {}
        self.sort_column = "name"
        self.sort_reverse = False
        self.auto_refresh_job = None

        self.log_history = []
        self.profiles_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_profiles.json")
        self.profiles = self._load_profiles()

        self.settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prank_gui_settings.json")
        self.settings = self._load_settings()

        self.autoclose_names = set(self.settings.get("autoclose_names", []))
        self.autoclose_enabled = tk.BooleanVar(value=self.settings.get("autoclose_enabled", False))

        self.watch_new_enabled = tk.BooleanVar(value=self.settings.get("watch_new_enabled", False))
        self.known_pids = set()
        self.watch_job = None

        self.keyboard_shortcuts_enabled = tk.BooleanVar(value=self.settings.get("keyboard_shortcuts_enabled", True))
        self.remember_window_size = tk.BooleanVar(value=self.settings.get("remember_window_size", True))
        self.hotkey_name = self.settings.get("hotkey_name", "insert")

        geometry = self.settings.get("window_geometry", "860x830")
        self.root.geometry(geometry)

        self._setup_style()
        self._build_header()
        self._build_search_bar()
        self._build_tree()
        self._build_message_bar()
        self._build_chrome_bar()
        self._build_action_bar()
        self._build_status_log()

        # apply remaining persisted preferences to widgets that now exist
        self.message_var.set(self.settings.get("default_banner_message", "GOTCHA!"))
        self.url_var.set(self.settings.get("default_chrome_url", "https://"))
        self.auto_refresh_var.set(self.settings.get("auto_refresh_default", False))
        if self.auto_refresh_var.get():
            self._toggle_auto_refresh()
        if self.watch_new_enabled.get():
            self._toggle_watch_new()

        self._setup_keyboard_shortcuts()

        self.known_pids = set(p.pid for p in psutil.process_iter())
        self.refresh()

    # -- settings persistence ----------------------------------------------

    def _load_settings(self):
        try:
            with open(self.settings_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        self.settings["autoclose_names"] = sorted(self.autoclose_names)
        self.settings["autoclose_enabled"] = self.autoclose_enabled.get()
        self.settings["watch_new_enabled"] = self.watch_new_enabled.get()
        self.settings["keyboard_shortcuts_enabled"] = self.keyboard_shortcuts_enabled.get()
        self.settings["remember_window_size"] = self.remember_window_size.get()
        self.settings["hotkey_name"] = self.hotkey_name
        self.settings["default_banner_message"] = self.message_var.get()
        self.settings["default_chrome_url"] = self.url_var.get()
        self.settings["auto_refresh_default"] = self.auto_refresh_var.get()
        if self.remember_window_size.get():
            self.settings["window_geometry"] = self.root.geometry()
        try:
            with open(self.settings_path, "w") as f:
                json.dump(self.settings, f, indent=2)
            return True
        except Exception as e:
            self.set_status("Could not save settings: {0}".format(e))
            return False

    def _setup_keyboard_shortcuts(self):
        def maybe(handler):
            def wrapper(event=None):
                if self.keyboard_shortcuts_enabled.get():
                    handler()
            return wrapper

        self.root.bind("<F5>", maybe(self.refresh))
        self.root.bind("<Delete>", maybe(self.do_kill))
        self.root.bind("<Control-f>", maybe(lambda: self.search_entry.focus_set()))
        self.root.bind("<Control-b>", maybe(self.do_banner))
        self.root.bind("<Control-s>", maybe(self.do_suspend))
        self.root.bind("<Control-r>", maybe(self.do_resume))
        self.root.bind("<Control-q>", maybe(self.root.withdraw))


    # -- style -------------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "Treeview",
            background=BG_ROW_ODD, fieldbackground=BG_ROW_ODD,
            foreground=FG_TEXT, rowheight=24, borderwidth=0, font=FONT_UI,
        )
        style.configure(
            "Treeview.Heading",
            background=BG_PANEL, foreground=FG_TEXT, font=FONT_UI_BOLD,
            borderwidth=0, relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "white")],
        )
        style.map("Treeview.Heading", background=[("active", BG_PANEL)])

    def _panel(self, parent, **kwargs):
        f = tk.Frame(parent, bg=BG_DARK)
        f.pack(**kwargs)
        return f

    # -- header --------------------------------------------------------

    def _build_header(self):
        header = tk.Frame(self.root, bg=BG_PANEL)
        header.pack(fill="x")

        tk.Label(
            header, text="Local Process Tool", font=FONT_HEADER,
            fg=FG_TEXT, bg=BG_PANEL, padx=12, pady=10,
        ).pack(side="left")

        tk.Label(
            header, text="Press INSERT anywhere to reopen this window",
            font=("Segoe UI", 8), fg=FG_DIM, bg=BG_PANEL, padx=12,
        ).pack(side="right")

    # -- search bar ------------------------------------------------------

    def _build_search_bar(self):
        bar = self._panel(self.root, fill="x", padx=10, pady=(8, 4))

        tk.Label(bar, text="Search:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *args: self.refresh(preserve_selection=True))
        search_entry = tk.Entry(
            bar, textvariable=self.search_var, bg=BG_PANEL, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", font=FONT_UI,
        )
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 10), ipady=3)
        self.search_entry = search_entry

        self.auto_refresh_var = tk.BooleanVar(value=False)
        auto_chk = tk.Checkbutton(
            bar, text="Auto-refresh (3s)", variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh, bg=BG_DARK, fg=FG_TEXT,
            selectcolor=BG_PANEL, activebackground=BG_DARK, activeforeground=FG_TEXT,
            font=FONT_UI,
        )
        auto_chk.pack(side="left")

        watch_chk = tk.Checkbutton(
            bar, text="Watch new processes", variable=self.watch_new_enabled,
            command=self._toggle_watch_new, bg=BG_DARK, fg=FG_TEXT,
            selectcolor=BG_PANEL, activebackground=BG_DARK, activeforeground=FG_TEXT,
            font=FONT_UI,
        )
        watch_chk.pack(side="left", padx=(10, 0))

        self._styled_button(bar, "Refresh", self.refresh, side="right")

    # -- tree --------------------------------------------------------------

    def _build_tree(self):
        frame = tk.Frame(self.root, bg=BG_DARK)
        frame.pack(fill="both", expand=True, padx=10, pady=4)

        columns = ("fav", "pid", "name", "cpu", "mem", "status", "group", "info")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")

        headings = {
            "fav": ("\u2605", 32),
            "pid": ("PID", 60),
            "name": ("Name", 140),
            "cpu": ("CPU %", 55),
            "mem": ("Mem (MB)", 75),
            "status": ("Status", 100),
            "group": ("Group", 100),
            "info": ("Window Title", 200),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text, command=lambda c=col: self._sort_by(c))
            anchor = "center" if col in ("fav", "pid", "cpu", "mem") else "w"
            self.tree.column(col, width=width, anchor=anchor)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("even", background=BG_ROW_EVEN)
        self.tree.tag_configure("odd", background=BG_ROW_ODD)
        self.tree.tag_configure("nowindow", background=BG_ROW_NOWIN, foreground=FG_DIM)
        self.tree.tag_configure("favorite", foreground=WARN)
        self.tree.tag_configure("hung", foreground=DANGER)

        self.tree.bind("<Double-1>", lambda e: self.do_banner())
        self.tree.bind("<space>", lambda e: self._toggle_favorite())

    # -- message bar ---------------------------------------------------

    def _build_message_bar(self):
        bar = self._panel(self.root, fill="x", padx=10, pady=(4, 4))
        tk.Label(bar, text="Banner message:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI).pack(side="left")
        self.message_var = tk.StringVar(value="GOTCHA!")
        tk.Entry(
            bar, textvariable=self.message_var, bg=BG_PANEL, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", font=FONT_UI,
        ).pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=3)

    # -- chrome bar ----------------------------------------------------

    def _build_chrome_bar(self):
        bar = self._panel(self.root, fill="x", padx=10, pady=(0, 4))
        tk.Label(bar, text="Chrome URL:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI).pack(side="left")
        self.url_var = tk.StringVar(value="https://")
        url_entry = tk.Entry(
            bar, textvariable=self.url_var, bg=BG_PANEL, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", font=FONT_UI,
        )
        url_entry.pack(side="left", fill="x", expand=True, padx=(6, 10), ipady=3)
        url_entry.bind("<Return>", lambda e: self.do_open_chrome())
        self._styled_button(bar, "Open in Chrome", self.do_open_chrome, side="left")

    # -- action bar ------------------------------------------------------

    def _build_action_bar(self):
        row1 = self._panel(self.root, fill="x", padx=10, pady=(4, 2))
        row2 = self._panel(self.root, fill="x", padx=10, pady=(0, 2))
        row3 = self._panel(self.root, fill="x", padx=10, pady=(0, 2))
        row4 = self._panel(self.root, fill="x", padx=10, pady=(0, 2))
        row5 = self._panel(self.root, fill="x", padx=10, pady=(0, 6))

        self._styled_button(row1, "GOTCHA Banner", self.do_banner, side="left", color=DANGER)
        self._styled_button(row1, "Flash", self.do_flash, side="left")
        self._styled_button(row1, "Minimize", self.do_minimize, side="left")
        self._styled_button(row1, "Maximize", self.do_maximize, side="left")
        self._styled_button(row1, "Focus", self.do_focus, side="left")
        self._styled_button(row1, "Rename Title", self.do_rename_title, side="left")
        self._styled_button(row1, "Pin \u2605", self._toggle_favorite, side="left")

        self._styled_button(row2, "Suspend", self.do_suspend, side="left", color=WARN)
        self._styled_button(row2, "Resume", self.do_resume, side="left", color=OK_GREEN)
        self._styled_button(row2, "Kill", self.do_kill, side="left", color=DANGER)
        self._styled_button(row2, "Crash", self.do_crash, side="left", color=DANGER)
        self._styled_button(row2, "Restart", self.do_restart, side="left", color=WARN)
        self._styled_button(row2, "Priority", self.open_priority_dialog, side="left")
        self._styled_button(row2, "Affinity", self.open_affinity_dialog, side="left")

        self._styled_button(row3, "Info", self.open_info_panel, side="left")
        self._styled_button(row3, "Signature/Hash", self.open_signature_hash, side="left")
        self._styled_button(row3, "All Windows", self.open_all_windows, side="left")
        self._styled_button(row3, "Find Task", self.open_task_finder, side="left")
        self._styled_button(row3, "Command Console", self.open_command_console, side="left")

        self._styled_button(row4, "Process Tree", self.open_process_tree, side="left")
        self._styled_button(row4, "Live Graph", self.open_live_graph, side="left")
        self._styled_button(row4, "GPU Usage", self.open_gpu_usage, side="left")
        self._styled_button(row4, "Duplicates", self.open_duplicates, side="left")
        self._styled_button(row4, "Leak Alert", self.open_leak_alert, side="left")

        row6 = self._panel(self.root, fill="x", padx=10, pady=(0, 6))
        self._styled_button(row6, "Disk I/O", self.open_disk_io, side="left")
        self._styled_button(row6, "Network", self.open_network_info, side="left")
        self._styled_button(row6, "Loaded DLLs", self.open_dll_list, side="left")
        self._styled_button(row6, "Settings", self.open_settings, side="left", color=ACCENT)

        self._styled_button(row5, "Startup Programs", self.open_startup_programs, side="left")
        self._styled_button(row5, "Launch New", self.open_launch_new, side="left")
        self._styled_button(row5, "Profiles", self.open_profiles, side="left")
        self._styled_button(row5, "Auto-Close Rules", self.open_autoclose_rules, side="left")
        self._styled_button(row5, "Export Log", self.do_export_log, side="left")
        self._styled_button(row5, "Close", self.root.withdraw, side="right")

    def _styled_button(self, parent, text, command, side="left", color=ACCENT):
        btn = tk.Button(
            parent, text=text, command=command, bg=BG_PANEL, fg=color,
            activebackground=ACCENT_HOVER, activeforeground="white",
            relief="flat", font=FONT_UI_BOLD, padx=8, pady=5, cursor="hand2",
            highlightthickness=0, bd=0, anchor="center",
        )
        btn.pack(side=side, padx=3, ipadx=2)
        return btn

    # -- status log ------------------------------------------------------

    def _build_status_log(self):
        frame = tk.Frame(self.root, bg=BG_PANEL)
        frame.pack(fill="x", padx=0, pady=0, side="bottom")

        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(
            frame, textvariable=self.status_var, anchor="w", bg=BG_PANEL,
            fg=FG_DIM, font=("Segoe UI", 8), padx=10, pady=6,
        ).pack(fill="x")

    def set_status(self, text):
        timestamp = time.strftime("%H:%M:%S")
        line = "[{0}] {1}".format(timestamp, text)
        self.status_var.set(line)
        self.log_history.append(line)

    # -- data / refresh ----------------------------------------------------

    def refresh(self, preserve_selection=False):
        selected_pids = set()
        if preserve_selection:
            for iid in self.tree.selection():
                vals = self.tree.item(iid, "values")
                if vals:
                    selected_pids.add(int(vals[1]))

        for row in self.tree.get_children():
            self.tree.delete(row)
        self.pid_to_window = {}
        self.pid_to_all_windows = get_visible_windows_by_pid()

        with_window, without_window = list_processes()

        # Auto-close: kill any process whose name matches the configured
        # list, then drop it from what we're about to display.
        if self.autoclose_enabled.get() and self.autoclose_names:
            killed_any = []
            for lst in (with_window, without_window):
                keep = []
                for item in lst:
                    if item["name"].lower() in self.autoclose_names:
                        kill_pid(item["pid"])
                        killed_any.append(item["name"])
                    else:
                        keep.append(item)
                lst[:] = keep
            if killed_any:
                self.set_status("Auto-closed: {0}".format(", ".join(killed_any)))

        query = self.search_var.get().strip().lower()

        if query:
            with_window = [p for p in with_window if query in p["name"].lower()]
            without_window = [p for p in without_window if query in p["name"].lower()]

        with_window = self._apply_sort(with_window)
        without_window = self._apply_sort(without_window)

        to_reselect = []

        idx = 0
        for item in with_window:
            fav = "\u2605" if item["pid"] in self.favorites else ""
            status = "Not Responding" if item.get("hung") else "Running"
            if item.get("hung"):
                tag = "hung"
            elif item["pid"] in self.favorites:
                tag = "favorite"
            else:
                tag = "even" if idx % 2 == 0 else "odd"
            iid = self.tree.insert(
                "", "end",
                values=(fav, item["pid"], item["name"], "{0:.1f}".format(item["cpu"]),
                        "{0:.1f}".format(item["mem"]), status, "Has window", item["title"]),
                tags=(tag,),
            )
            self.pid_to_window[item["pid"]] = (item["hwnd"], item["title"])
            if item["pid"] in selected_pids:
                to_reselect.append(iid)
            idx += 1

        for item in without_window:
            fav = "\u2605" if item["pid"] in self.favorites else ""
            tag = "favorite" if item["pid"] in self.favorites else "nowindow"
            iid = self.tree.insert(
                "", "end",
                values=(fav, item["pid"], item["name"], "{0:.1f}".format(item["cpu"]),
                        "{0:.1f}".format(item["mem"]), "-", "No window", ""),
                tags=(tag,),
            )
            if item["pid"] in selected_pids:
                to_reselect.append(iid)

        if to_reselect:
            self.tree.selection_set(to_reselect)

        self.set_status("Loaded {0} windowed + {1} background processes.".format(
            len(with_window), len(without_window)))

    def _apply_sort(self, items):
        col = self.sort_column
        key_map = {
            "pid": lambda d: d["pid"],
            "name": lambda d: d["name"].lower(),
            "cpu": lambda d: d["cpu"],
            "mem": lambda d: d["mem"],
        }
        key_fn = key_map.get(col, key_map["name"])
        return sorted(items, key=key_fn, reverse=self.sort_reverse)

    def _sort_by(self, col):
        if col not in ("pid", "name", "cpu", "mem"):
            return
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False
        self.refresh(preserve_selection=True)

    def _toggle_auto_refresh(self):
        if self.auto_refresh_var.get():
            self._auto_refresh_tick()
        else:
            if self.auto_refresh_job is not None:
                self.root.after_cancel(self.auto_refresh_job)
                self.auto_refresh_job = None

    def _auto_refresh_tick(self):
        self.refresh(preserve_selection=True)
        if self.auto_refresh_var.get():
            self.auto_refresh_job = self.root.after(3000, self._auto_refresh_tick)

    # -- selection helpers -------------------------------------------------

    def get_selected_pids(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select at least one process first.")
            return []
        pids = []
        for iid in sel:
            vals = self.tree.item(iid, "values")
            pids.append(int(vals[1]))
        return pids

    def get_selected_pid(self):
        pids = self.get_selected_pids()
        if not pids:
            return None
        return pids[0]

    def _toggle_favorite(self):
        pids = self.get_selected_pids()
        for pid in pids:
            if pid in self.favorites:
                self.favorites.discard(pid)
            else:
                self.favorites.add(pid)
        self.refresh(preserve_selection=True)

    # -- actions -------------------------------------------------------------

    def do_banner(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        if pid not in self.pid_to_window:
            messagebox.showinfo("No window", "This process has no visible window to overlay.")
            return
        hwnd, title = self.pid_to_window[pid]
        custom_message = self.message_var.get().strip() or "GOTCHA!"
        show_gotcha_banner(hwnd, title, message=custom_message)
        self.set_status("Showed banner \"{0}\" over pid {1}.".format(custom_message, pid))

    def do_flash(self):
        pid = self.get_selected_pid()
        if pid is None or pid not in self.pid_to_window:
            messagebox.showinfo("No window", "Select a windowed process.")
            return
        hwnd, _ = self.pid_to_window[pid]
        ok, msg = flash_hwnd(hwnd)
        self.set_status(msg)

    def do_minimize(self):
        pid = self.get_selected_pid()
        if pid is None or pid not in self.pid_to_window:
            messagebox.showinfo("No window", "Select a windowed process.")
            return
        hwnd, _ = self.pid_to_window[pid]
        ok, msg = minimize_hwnd(hwnd)
        self.set_status(msg)

    def do_maximize(self):
        pid = self.get_selected_pid()
        if pid is None or pid not in self.pid_to_window:
            messagebox.showinfo("No window", "Select a windowed process.")
            return
        hwnd, _ = self.pid_to_window[pid]
        ok, msg = maximize_hwnd(hwnd)
        self.set_status(msg)

    def do_focus(self):
        pid = self.get_selected_pid()
        if pid is None or pid not in self.pid_to_window:
            messagebox.showinfo("No window", "Select a windowed process.")
            return
        hwnd, title = self.pid_to_window[pid]
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            self.set_status("Focused: {0}".format(title))
        except Exception as e:
            self.set_status("Focus failed: {0}".format(e))

    def do_rename_title(self):
        pid = self.get_selected_pid()
        if pid is None or pid not in self.pid_to_window:
            messagebox.showinfo("No window", "Select a windowed process.")
            return
        hwnd, title = self.pid_to_window[pid]

        win = tk.Toplevel(self.root)
        win.title("Rename Window Title")
        win.geometry("360x130")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(win, text="New title for: {0}".format(title), bg=BG_DARK, fg=FG_TEXT,
                 font=FONT_UI, wraplength=320).pack(padx=10, pady=(12, 6))
        name_var = tk.StringVar(value=title)
        entry = tk.Entry(win, textvariable=name_var, bg=BG_PANEL, fg=FG_TEXT,
                          insertbackground=FG_TEXT, relief="flat", font=FONT_UI)
        entry.pack(fill="x", padx=10)
        entry.focus_set()

        def apply_rename():
            try:
                win32gui.SetWindowText(hwnd, name_var.get())
                self.set_status("Renamed window title.")
                self.refresh(preserve_selection=True)
            except Exception as e:
                self.set_status("Rename failed: {0}".format(e))
            win.destroy()

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=10)
        self._styled_button(btn_row, "Apply", apply_rename, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Cancel", win.destroy, side="right")

    def do_suspend(self):
        pids = self.get_selected_pids()
        results = [suspend_pid(pid) for pid in pids]
        self._report_bulk(results, "Suspend")

    def do_resume(self):
        pids = self.get_selected_pids()
        results = [resume_pid(pid) for pid in pids]
        self._report_bulk(results, "Resume")

    def do_kill(self):
        pids = self.get_selected_pids()
        if not pids:
            return
        if not messagebox.askyesno("Confirm", "Kill {0} selected process(es)?".format(len(pids))):
            return
        results = [kill_pid(pid) for pid in pids]
        self._report_bulk(results, "Kill")
        self.refresh()

    def do_crash(self):
        pids = self.get_selected_pids()
        if not pids:
            return
        if not messagebox.askyesno(
            "Confirm",
            "CRASH {0} selected process(es)?\n\n"
            "This is an immediate hard kill with no graceful shutdown -- "
            "unsaved work in that app will be lost instantly.".format(len(pids)),
        ):
            return
        results = [crash_pid(pid) for pid in pids]
        self._report_bulk(results, "Crash")
        self.refresh()

    def do_restart(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        if not messagebox.askyesno("Confirm", "Restart pid {0}? It will be killed and relaunched.".format(pid)):
            return
        ok, msg = restart_pid(pid)
        self.set_status(msg)
        if not ok:
            messagebox.showerror("Restart failed", msg)
        self.refresh()

    def _report_bulk(self, results, verb):
        ok_count = sum(1 for ok, _ in results if ok)
        fail_count = len(results) - ok_count
        last_msgs = [msg for _, msg in results]
        self.set_status("{0}: {1} ok, {2} failed. Last: {3}".format(
            verb, ok_count, fail_count, last_msgs[-1] if last_msgs else ""))
        if fail_count and ok_count == 0:
            messagebox.showerror("{0} failed".format(verb), last_msgs[-1] if last_msgs else "Unknown error")

    def do_open_chrome(self):
        url = self.url_var.get()
        ok, msg = open_chrome_tab(url)
        self.set_status(msg)
        if not ok:
            messagebox.showerror("Could not open URL", msg)

    # -- task finder -------------------------------------------------------

    def select_pid_in_tree(self, pid):
        """Selects and scrolls to the row for `pid` in the main tree, if present."""
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if vals and int(vals[1]) == pid:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return True
        return False

    def open_task_finder(self):
        win = tk.Toplevel(self.root)
        win.title("Find Task")
        win.geometry("420x420")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(
            win, text="Search by name (live filter):", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=10, pady=(10, 2))

        search_var = tk.StringVar()
        entry = tk.Entry(
            win, textvariable=search_var, bg=BG_PANEL, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", font=FONT_UI,
        )
        entry.pack(fill="x", padx=10, ipady=3)
        entry.focus_set()

        tk.Label(
            win, text="All running processes (PID - name):", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=10, pady=(10, 2))

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, selectbackground=ACCENT,
            relief="flat", font=FONT_UI, activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        all_procs = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                all_procs.append((p.info["pid"], p.info["name"] or "?"))
            except Exception:
                continue
        all_procs.sort(key=lambda t: t[1].lower())

        def populate(filter_text=""):
            listbox.delete(0, "end")
            filter_text = filter_text.strip().lower()
            for pid, name in all_procs:
                if filter_text and filter_text not in name.lower() and filter_text not in str(pid):
                    continue
                listbox.insert("end", "{0}  -  {1}".format(pid, name))

        populate()
        search_var.trace("w", lambda *args: populate(search_var.get()))

        def on_select(event=None):
            sel = listbox.curselection()
            if not sel:
                return
            text = listbox.get(sel[0])
            pid = int(text.split("-")[0].strip())
            found = self.select_pid_in_tree(pid)
            if found:
                self.set_status("Selected pid {0} in main list. Use the action buttons on it.".format(pid))
                win.destroy()
            else:
                messagebox.showinfo(
                    "Not in current view",
                    "That process isn't in the current filtered/sorted list. "
                    "Clear the main search box and try again.",
                )

        listbox.bind("<Double-1>", on_select)

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        self._styled_button(btn_row, "Select", on_select, side="left")
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- command console -----------------------------------------------

    def open_command_console(self):
        win = tk.Toplevel(self.root)
        win.title("Command Console")
        win.geometry("620x520")
        win.configure(bg=BG_DARK)

        top = tk.Frame(win, bg=BG_DARK)
        top.pack(fill="x", padx=10, pady=(10, 4))

        tk.Label(top, text="Syntax:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI).pack(side="left")
        syntax_var = tk.StringVar(value="cmd")
        syntax_menu = ttk.Combobox(
            top, textvariable=syntax_var, state="readonly",
            values=["cmd", "python", "batch", "vbs", "js", "ps1"], width=12,
        )
        syntax_menu.pack(side="left", padx=(6, 0))

        tk.Label(
            win, text="Command / script:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=10, pady=(8, 2))

        code_box = tk.Text(
            win, height=10, bg=BG_PANEL, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", font=("Consolas", 10),
        )
        code_box.pack(fill="both", expand=False, padx=10)

        tk.Label(
            win, text="Output:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=10, pady=(8, 2))

        output_box = tk.Text(
            win, height=12, bg="#111218", fg="#b6ffb6", insertbackground=FG_TEXT,
            relief="flat", font=("Consolas", 9), state="disabled",
        )
        output_box.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        def write_output(text):
            output_box.configure(state="normal")
            output_box.insert("end", text)
            output_box.see("end")
            output_box.configure(state="disabled")

        def run_code():
            code = code_box.get("1.0", "end-1c")
            syntax = syntax_var.get()
            write_output("\n> running as {0} ...\n".format(syntax))

            try:
                if syntax == "cmd":
                    proc = subprocess.Popen(
                        code, shell=True, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    out, _ = proc.communicate()
                    write_output(_decode(out))

                elif syntax == "python":
                    proc = subprocess.Popen(
                        [sys.executable, "-c", code],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    out, _ = proc.communicate()
                    write_output(_decode(out))

                elif syntax in ("batch", "vbs", "js", "ps1"):
                    ext_map = {"batch": ".bat", "vbs": ".vbs", "js": ".js", "ps1": ".ps1"}
                    ext = ext_map[syntax]
                    fd, path = tempfile.mkstemp(suffix=ext)
                    os.close(fd)
                    with open(path, "w") as f:
                        f.write(code)

                    if syntax == "batch":
                        cmd = ["cmd", "/c", path]
                    elif syntax == "vbs":
                        cmd = ["cscript", "//nologo", path]
                    elif syntax == "js":
                        cmd = ["cscript", "//nologo", path]
                    elif syntax == "ps1":
                        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]

                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    out, _ = proc.communicate()
                    write_output(_decode(out))

                    try:
                        os.remove(path)
                    except Exception:
                        pass

                write_output("\n(done)\n")
            except Exception as e:
                write_output("\nERROR: {0}\n".format(e))

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        self._styled_button(btn_row, "Run", run_code, side="left", color=OK_GREEN)
        self._styled_button(
            btn_row, "Clear Output",
            lambda: (output_box.configure(state="normal"), output_box.delete("1.0", "end"),
                      output_box.configure(state="disabled")),
            side="left",
        )
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- priority control ----------------------------------------------

    def open_priority_dialog(self):
        pids = self.get_selected_pids()
        if not pids:
            return

        win = tk.Toplevel(self.root)
        win.title("Set Priority")
        win.geometry("300x160")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(
            win, text="Set priority for {0} process(es):".format(len(pids)),
            bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, wraplength=260,
        ).pack(padx=10, pady=(14, 6))

        levels = ["Idle", "Below Normal", "Normal", "Above Normal", "High", "Realtime"]
        level_var = tk.StringVar(value="Normal")
        combo = ttk.Combobox(win, textvariable=level_var, state="readonly", values=levels)
        combo.pack(padx=10, pady=4, fill="x")

        def apply_priority():
            level_map = {
                "Idle": psutil.IDLE_PRIORITY_CLASS,
                "Below Normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                "Normal": psutil.NORMAL_PRIORITY_CLASS,
                "Above Normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
                "High": psutil.HIGH_PRIORITY_CLASS,
                "Realtime": psutil.REALTIME_PRIORITY_CLASS,
            }
            target = level_map[level_var.get()]
            ok_count = 0
            last_err = ""
            for pid in pids:
                try:
                    psutil.Process(pid).nice(target)
                    ok_count += 1
                except Exception as e:
                    last_err = str(e)
            self.set_status("Priority set on {0}/{1} process(es).{2}".format(
                ok_count, len(pids), (" Last error: " + last_err) if last_err else ""))
            win.destroy()

        btn_row2 = tk.Frame(win, bg=BG_DARK)
        btn_row2.pack(fill="x", padx=10, pady=(10, 10))
        self._styled_button(btn_row2, "Apply", apply_priority, side="left", color=OK_GREEN)
        self._styled_button(btn_row2, "Cancel", win.destroy, side="right")

    # -- CPU affinity -----------------------------------------------------

    def open_affinity_dialog(self):
        pids = self.get_selected_pids()
        if not pids:
            return

        try:
            core_count = psutil.cpu_count()
        except Exception:
            core_count = 1

        win = tk.Toplevel(self.root)
        win.title("Set CPU Affinity")
        win.geometry("320x{0}".format(120 + core_count * 26))
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(
            win, text="Cores to allow for {0} process(es):".format(len(pids)),
            bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, wraplength=280,
        ).pack(padx=10, pady=(12, 6))

        core_vars = []
        for i in range(core_count):
            var = tk.BooleanVar(value=True)
            core_vars.append(var)
            tk.Checkbutton(
                win, text="Core {0}".format(i), variable=var, bg=BG_DARK, fg=FG_TEXT,
                selectcolor=BG_PANEL, activebackground=BG_DARK, activeforeground=FG_TEXT,
                font=FONT_UI,
            ).pack(anchor="w", padx=20)

        def apply_affinity():
            selected_cores = [i for i, v in enumerate(core_vars) if v.get()]
            if not selected_cores:
                messagebox.showinfo("No cores selected", "Select at least one core.")
                return
            ok_count = 0
            last_err = ""
            for pid in pids:
                try:
                    psutil.Process(pid).cpu_affinity(selected_cores)
                    ok_count += 1
                except Exception as e:
                    last_err = str(e)
            self.set_status("Affinity set on {0}/{1} process(es).{2}".format(
                ok_count, len(pids), (" Last error: " + last_err) if last_err else ""))
            win.destroy()

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=10)
        self._styled_button(btn_row, "Apply", apply_affinity, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Cancel", win.destroy, side="right")

    # -- info panel (exe path / cmdline / create time) --------------------

    def open_info_panel(self):
        pid = self.get_selected_pid()
        if pid is None:
            return

        try:
            p = psutil.Process(pid)
            exe = p.exe()
        except Exception:
            exe = "(unavailable -- may need admin rights)"
        try:
            cmdline = " ".join(p.cmdline())
        except Exception:
            cmdline = "(unavailable)"
        try:
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.create_time()))
        except Exception:
            created = "(unavailable)"
        try:
            status = p.status()
        except Exception:
            status = "(unavailable)"
        try:
            nice_val = p.nice()
        except Exception:
            nice_val = "(unavailable)"
        try:
            num_threads = p.num_threads()
        except Exception:
            num_threads = "(unavailable)"

        win = tk.Toplevel(self.root)
        win.title("Process Info - PID {0}".format(pid))
        win.geometry("520x300")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        rows = [
            ("PID", str(pid)),
            ("Executable path", exe),
            ("Command line", cmdline),
            ("Created", created),
            ("Status", status),
            ("Priority class", str(nice_val)),
            ("Thread count", str(num_threads)),
        ]

        for label, value in rows:
            row = tk.Frame(win, bg=BG_DARK)
            row.pack(fill="x", padx=10, pady=3, anchor="w")
            tk.Label(row, text=label + ":", bg=BG_DARK, fg=FG_DIM, font=FONT_UI_BOLD,
                     width=16, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=BG_DARK, fg=FG_TEXT, font=FONT_UI,
                     wraplength=340, justify="left", anchor="w").pack(side="left", fill="x", expand=True)

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- digital signature + SHA-256 hash ---------------------------------

    def open_signature_hash(self):
        pid = self.get_selected_pid()
        if pid is None:
            return

        try:
            exe = psutil.Process(pid).exe()
        except Exception as e:
            messagebox.showerror("Error", "Could not get executable path: {0}".format(e))
            return

        win = tk.Toplevel(self.root)
        win.title("Signature / Hash - PID {0}".format(pid))
        win.geometry("560x220")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(win, text="Path: {0}".format(exe), bg=BG_DARK, fg=FG_TEXT,
                 font=FONT_UI, wraplength=520, justify="left").pack(padx=10, pady=(12, 8), anchor="w")

        status_var = tk.StringVar(value="Checking...")
        tk.Label(win, textvariable=status_var, bg=BG_DARK, fg=FG_DIM,
                 font=FONT_UI, wraplength=520, justify="left").pack(padx=10, anchor="w")

        def do_check():
            sig = check_digital_signature(exe)
            status_var.set("Signature: {0}\n\nComputing SHA-256 hash (may take a moment)...".format(sig))
            win.update_idletasks()
            digest = sha256_of_file(exe)
            status_var.set("Signature: {0}\n\nSHA-256: {1}".format(sig, digest))

        win.after(50, do_check)
        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- disk I/O per process -----------------------------------------

    def open_disk_io(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        try:
            p = psutil.Process(pid)
            io = p.io_counters()
        except Exception as e:
            messagebox.showerror("Unavailable", "Could not read I/O counters: {0}".format(e))
            return

        win = tk.Toplevel(self.root)
        win.title("Disk I/O - PID {0}".format(pid))
        win.geometry("380x220")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        rows = [
            ("Read bytes", "{0:.1f} MB".format(io.read_bytes / (1024.0 * 1024.0))),
            ("Write bytes", "{0:.1f} MB".format(io.write_bytes / (1024.0 * 1024.0))),
            ("Read operations", str(io.read_count)),
            ("Write operations", str(io.write_count)),
        ]
        for label, value in rows:
            row = tk.Frame(win, bg=BG_DARK)
            row.pack(fill="x", padx=10, pady=4, anchor="w")
            tk.Label(row, text=label + ":", bg=BG_DARK, fg=FG_DIM, font=FONT_UI_BOLD,
                     width=16, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=BG_DARK, fg=FG_TEXT, font=FONT_UI).pack(side="left")

        tk.Label(
            win, text="Values are cumulative totals since the process started.",
            bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8), wraplength=340,
        ).pack(padx=10, pady=(6, 0), anchor="w")

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- network activity (open connections) per process -------------------

    def open_network_info(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        try:
            p = psutil.Process(pid)
            try:
                conns = p.connections(kind="inet")
            except AttributeError:
                conns = p.net_connections(kind="inet")
        except Exception as e:
            messagebox.showerror("Unavailable", "Could not read connections: {0}".format(e))
            return

        win = tk.Toplevel(self.root)
        win.title("Network Connections - PID {0}".format(pid))
        win.geometry("560x360")
        win.configure(bg=BG_DARK)

        tk.Label(
            win,
            text="Note: this shows this process's open network connections, not live "
                 "bandwidth (Windows doesn't expose per-process bandwidth without extra "
                 "driver-level tooling).",
            bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8), wraplength=520, justify="left",
        ).pack(padx=10, pady=(10, 6), anchor="w")

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, relief="flat", font=("Consolas", 9), activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        if not conns:
            listbox.insert("end", "No active network connections for this process.")
        else:
            for c in conns:
                laddr = "{0}:{1}".format(*c.laddr) if c.laddr else "-"
                raddr = "{0}:{1}".format(*c.raddr) if c.raddr else "-"
                listbox.insert("end", "{0}  local={1}  remote={2}  status={3}".format(
                    c.type.name if hasattr(c.type, "name") else str(c.type), laddr, raddr, c.status))

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- loaded DLLs / modules ---------------------------------------------

    def open_dll_list(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        try:
            p = psutil.Process(pid)
            maps = p.memory_maps()
        except Exception as e:
            messagebox.showerror(
                "Unavailable",
                "Could not list loaded modules (often needs the process to be the "
                "same bitness as Python, or admin rights): {0}".format(e),
            )
            return

        win = tk.Toplevel(self.root)
        win.title("Loaded DLLs - PID {0}".format(pid))
        win.geometry("640x420")
        win.configure(bg=BG_DARK)

        search_var = tk.StringVar()
        search_row = tk.Frame(win, bg=BG_DARK)
        search_row.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(search_row, text="Filter:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI).pack(side="left")
        tk.Entry(search_row, textvariable=search_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=FONT_UI).pack(side="left", fill="x", expand=True, padx=(6, 0))

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, relief="flat", font=("Consolas", 8), activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        all_paths = [m.path for m in maps]

        def populate(filter_text=""):
            listbox.delete(0, "end")
            filter_text = filter_text.strip().lower()
            for path in all_paths:
                if filter_text and filter_text not in path.lower():
                    continue
                listbox.insert("end", path)

        populate()
        search_var.trace("w", lambda *args: populate(search_var.get()))

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- all windows for a process ------------------------------------

    def open_all_windows(self):
        pid = self.get_selected_pid()
        if pid is None:
            return

        windows = self.pid_to_all_windows.get(pid, [])
        if not windows:
            messagebox.showinfo("No windows", "This process has no visible windows.")
            return

        win = tk.Toplevel(self.root)
        win.title("All Windows - PID {0}".format(pid))
        win.geometry("520x360")
        win.configure(bg=BG_DARK)

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, selectbackground=ACCENT,
            relief="flat", font=FONT_UI, activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=10)

        for hwnd, title in windows:
            listbox.insert("end", "[{0}] {1}".format(hwnd, title))

        def get_selected_hwnd():
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("No selection", "Select a window first.")
                return None
            return windows[sel[0]][0]

        def act_focus():
            hwnd = get_selected_hwnd()
            if hwnd is None:
                return
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception as e:
                messagebox.showerror("Error", str(e))

        def act_minimize():
            hwnd = get_selected_hwnd()
            if hwnd is None:
                return
            minimize_hwnd(hwnd)

        def act_banner():
            hwnd = get_selected_hwnd()
            if hwnd is None:
                return
            sel = listbox.curselection()
            title = windows[sel[0]][1]
            show_gotcha_banner(hwnd, title, message=self.message_var.get().strip() or "GOTCHA!")

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        self._styled_button(btn_row, "Focus", act_focus, side="left")
        self._styled_button(btn_row, "Minimize", act_minimize, side="left")
        self._styled_button(btn_row, "Banner", act_banner, side="left", color=DANGER)
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- process tree ----------------------------------------------------

    def open_process_tree(self):
        win = tk.Toplevel(self.root)
        win.title("Process Tree")
        win.geometry("500x500")
        win.configure(bg=BG_DARK)

        tree = ttk.Treeview(win, columns=("pid", "name"), show="tree headings")
        tree.heading("#0", text="")
        tree.heading("pid", text="PID")
        tree.heading("name", text="Name")
        tree.column("#0", width=20)
        tree.column("pid", width=70, anchor="center")
        tree.column("name", width=300)
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        children_map = {}
        info_map = {}
        for p in psutil.process_iter(["pid", "ppid", "name"]):
            try:
                pid = p.info["pid"]
                ppid = p.info["ppid"]
                name = p.info["name"] or "?"
            except Exception:
                continue
            info_map[pid] = name
            children_map.setdefault(ppid, []).append(pid)

        inserted = set()

        def insert_node(pid, parent_iid):
            if pid in inserted:
                return
            inserted.add(pid)
            name = info_map.get(pid, "?")
            node = tree.insert(parent_iid, "end", values=(pid, name), text="")
            for child_pid in sorted(children_map.get(pid, [])):
                insert_node(child_pid, node)

        all_pids = set(info_map.keys())
        roots = []
        for p in psutil.process_iter(["pid", "ppid"]):
            try:
                pid = p.info["pid"]
                ppid = p.info["ppid"]
            except Exception:
                continue
            if ppid not in all_pids:
                roots.append(pid)

        for root_pid in sorted(set(roots)):
            insert_node(root_pid, "")

        for pid in sorted(all_pids):
            if pid not in inserted:
                insert_node(pid, "")

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- live CPU/RAM graph ------------------------------------------------

    def open_live_graph(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        try:
            target = psutil.Process(pid)
            target_name = target.name()
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        win = tk.Toplevel(self.root)
        win.title("Live Graph - {0} (pid {1})".format(target_name, pid))
        win.geometry("480x340")
        win.configure(bg=BG_DARK)

        tk.Label(
            win, text="CPU % (red) and Memory MB (blue) over time",
            bg=BG_DARK, fg=FG_TEXT, font=FONT_UI,
        ).pack(pady=(8, 2))

        canvas_w, canvas_h = 460, 260
        canvas = tk.Canvas(win, width=canvas_w, height=canvas_h, bg="#111218", highlightthickness=0)
        canvas.pack(padx=10, pady=6)

        cpu_history = []
        mem_history = []
        max_points = 60
        running = {"active": True}

        def on_close():
            running["active"] = False
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

        def tick():
            if not running["active"]:
                return
            try:
                cpu = target.cpu_percent(interval=None)
                mem = target.memory_info().rss / (1024.0 * 1024.0)
            except Exception:
                running["active"] = False
                canvas.create_text(
                    canvas_w // 2, canvas_h // 2, text="Process ended.",
                    fill=FG_DIM, font=FONT_UI,
                )
                return

            cpu_history.append(cpu)
            mem_history.append(mem)
            if len(cpu_history) > max_points:
                cpu_history.pop(0)
                mem_history.pop(0)

            canvas.delete("all")

            cpu_max = max(max(cpu_history), 100.0)
            mem_max = max(max(mem_history), 1.0)

            def plot(history, maxval, color):
                if len(history) < 2:
                    return
                step_x = canvas_w / float(max_points - 1)
                points = []
                for i, val in enumerate(history):
                    x = i * step_x
                    y = canvas_h - (val / maxval) * (canvas_h - 10) - 5
                    points.append(x)
                    points.append(y)
                canvas.create_line(*points, fill=color, width=2)

            plot(cpu_history, cpu_max, "#ff5c5c")
            plot(mem_history, mem_max, "#6c8cff")

            canvas.create_text(
                8, 10, anchor="w",
                text="CPU: {0:.1f}%   Mem: {1:.1f} MB".format(cpu, mem),
                fill=FG_TEXT, font=("Consolas", 9),
            )

            win.after(1000, tick)

        tick()
        self._styled_button(win, "Close", on_close, side="bottom")

    # -- GPU usage -------------------------------------------------------

    def open_gpu_usage(self):
        win = tk.Toplevel(self.root)
        win.title("GPU Usage")
        win.geometry("420x220")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        text_var = tk.StringVar(value="Loading...")
        tk.Label(win, textvariable=text_var, bg=BG_DARK, fg=FG_TEXT, font=("Consolas", 10),
                 wraplength=380, justify="left").pack(padx=10, pady=14, anchor="w")

        def do_update():
            text_var.set(get_gpu_usage_summary())

        do_update()
        self._styled_button(win, "Refresh", do_update, side="left")
        self._styled_button(win, "Close", win.destroy, side="right")

    # -- duplicate process detection --------------------------------------

    def open_duplicates(self):
        name_to_pids = {}
        for p in psutil.process_iter(["pid", "name"]):
            try:
                name = (p.info["name"] or "?").lower()
                name_to_pids.setdefault(name, []).append(p.info["pid"])
            except Exception:
                continue

        dupes = {name: pids for name, pids in name_to_pids.items() if len(pids) > 1}

        win = tk.Toplevel(self.root)
        win.title("Duplicate Processes")
        win.geometry("420x400")
        win.configure(bg=BG_DARK)

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, relief="flat", font=FONT_UI, activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=10)

        if not dupes:
            listbox.insert("end", "No duplicate process names found.")
        else:
            for name, pids in sorted(dupes.items(), key=lambda kv: -len(kv[1])):
                listbox.insert("end", "{0}  x{1}   pids: {2}".format(
                    name, len(pids), ", ".join(str(p) for p in pids)))

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- leak / threshold alert watcher ------------------------------------

    def open_leak_alert(self):
        pid = self.get_selected_pid()
        if pid is None:
            return
        try:
            target = psutil.Process(pid)
            target_name = target.name()
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        win = tk.Toplevel(self.root)
        win.title("Leak Alert - {0} (pid {1})".format(target_name, pid))
        win.geometry("340x220")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(win, text="Alert if this process exceeds:", bg=BG_DARK, fg=FG_TEXT,
                 font=FONT_UI).pack(padx=10, pady=(12, 6), anchor="w")

        row1 = tk.Frame(win, bg=BG_DARK)
        row1.pack(fill="x", padx=10, pady=2)
        tk.Label(row1, text="CPU %:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, width=10, anchor="w").pack(side="left")
        cpu_var = tk.StringVar(value="80")
        tk.Entry(row1, textvariable=cpu_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", width=10).pack(side="left")

        row2 = tk.Frame(win, bg=BG_DARK)
        row2.pack(fill="x", padx=10, pady=2)
        tk.Label(row2, text="Mem MB:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, width=10, anchor="w").pack(side="left")
        mem_var = tk.StringVar(value="1000")
        tk.Entry(row2, textvariable=mem_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", width=10).pack(side="left")

        status_var = tk.StringVar(value="Not started.")
        tk.Label(win, textvariable=status_var, bg=BG_DARK, fg=FG_DIM, font=FONT_UI,
                 wraplength=300, justify="left").pack(padx=10, pady=(8, 4), anchor="w")

        watching = {"active": False, "alerted": False}

        def tick():
            if not watching["active"]:
                return
            try:
                cpu = target.cpu_percent(interval=None)
                mem = target.memory_info().rss / (1024.0 * 1024.0)
            except Exception:
                status_var.set("Process ended.")
                watching["active"] = False
                return

            try:
                cpu_limit = float(cpu_var.get())
                mem_limit = float(mem_var.get())
            except ValueError:
                status_var.set("Enter valid numbers.")
                win.after(2000, tick)
                return

            status_var.set("CPU: {0:.1f}%  Mem: {1:.1f} MB  (watching...)".format(cpu, mem))

            if (cpu > cpu_limit or mem > mem_limit) and not watching["alerted"]:
                watching["alerted"] = True
                messagebox.showwarning(
                    "Threshold exceeded",
                    "{0} (pid {1}) exceeded threshold:\nCPU {2:.1f}%  Mem {3:.1f} MB".format(
                        target_name, pid, cpu, mem),
                )
            elif cpu <= cpu_limit and mem <= mem_limit:
                watching["alerted"] = False

            win.after(2000, tick)

        def start_watch():
            watching["active"] = True
            watching["alerted"] = False
            tick()

        def stop_watch():
            watching["active"] = False
            status_var.set("Stopped.")

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=10)
        self._styled_button(btn_row, "Start", start_watch, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Stop", stop_watch, side="left", color=WARN)
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- startup programs ----------------------------------------------

    def open_startup_programs(self):
        win = tk.Toplevel(self.root)
        win.title("Startup Programs")
        win.geometry("560x420")
        win.configure(bg=BG_DARK)

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, selectbackground=ACCENT,
            relief="flat", font=("Consolas", 9), activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=10)

        entries = []

        try:
            import winreg
        except ImportError:
            winreg = None

        if winreg is not None:
            reg_locations = [
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ]
            for hive, subkey in reg_locations:
                try:
                    key = winreg.OpenKey(hive, subkey)
                except Exception:
                    continue
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                        hive_name = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
                        entries.append("[{0}] {1}  ->  {2}".format(hive_name, name, value))
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)

        try:
            startup_dir = os.path.join(
                os.environ.get("APPDATA", ""),
                "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
            )
            if os.path.isdir(startup_dir):
                for fname in os.listdir(startup_dir):
                    entries.append("[Startup folder] {0}".format(fname))
        except Exception:
            pass

        if not entries:
            entries = ["(none found, or registry access was denied)"]

        for e in entries:
            listbox.insert("end", e)

        self._styled_button(win, "Close", win.destroy, side="bottom")

    # -- launch new app with custom args -----------------------------------

    def open_launch_new(self):
        win = tk.Toplevel(self.root)
        win.title("Launch New App")
        win.geometry("480x200")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        path_var = tk.StringVar()
        args_var = tk.StringVar()

        row1 = tk.Frame(win, bg=BG_DARK)
        row1.pack(fill="x", padx=10, pady=(14, 4))
        tk.Label(row1, text="Program:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, width=10, anchor="w").pack(side="left")
        tk.Entry(row1, textvariable=path_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=FONT_UI).pack(side="left", fill="x", expand=True)

        def browse():
            path = filedialog.askopenfilename(title="Choose executable", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
            if path:
                path_var.set(path)

        self._styled_button(row1, "Browse", browse, side="left")

        row2 = tk.Frame(win, bg=BG_DARK)
        row2.pack(fill="x", padx=10, pady=4)
        tk.Label(row2, text="Arguments:", bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, width=10, anchor="w").pack(side="left")
        tk.Entry(row2, textvariable=args_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=FONT_UI).pack(side="left", fill="x", expand=True)

        def do_launch():
            path = path_var.get().strip()
            if not path:
                messagebox.showinfo("No program", "Choose or type a program path first.")
                return
            args = args_var.get().strip()
            cmd = [path] + (args.split() if args else [])
            try:
                subprocess.Popen(cmd)
                self.set_status("Launched: {0} {1}".format(path, args))
                win.destroy()
            except Exception as e:
                messagebox.showerror("Launch failed", str(e))

        def save_as_profile():
            path = path_var.get().strip()
            if not path:
                messagebox.showinfo("No program", "Choose or type a program path first.")
                return
            name = os.path.basename(path)
            self.profiles.append({"name": name, "path": path, "args": args_var.get().strip()})
            self._save_profiles()
            self.set_status("Saved profile: {0}".format(name))

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=14)
        self._styled_button(btn_row, "Launch", do_launch, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Save as Profile", save_as_profile, side="left")
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- saved app profiles -------------------------------------------------

    def _load_profiles(self):
        try:
            with open(self.profiles_path, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_profiles(self):
        try:
            with open(self.profiles_path, "w") as f:
                json.dump(self.profiles, f, indent=2)
        except Exception as e:
            self.set_status("Could not save profiles: {0}".format(e))

    def open_profiles(self):
        win = tk.Toplevel(self.root)
        win.title("App Profiles")
        win.geometry("480x360")
        win.configure(bg=BG_DARK)

        listbox = tk.Listbox(
            win, bg=BG_PANEL, fg=FG_TEXT, selectbackground=ACCENT,
            relief="flat", font=FONT_UI, activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=10, pady=10)

        def repopulate():
            listbox.delete(0, "end")
            for prof in self.profiles:
                listbox.insert("end", "{0}   ({1} {2})".format(prof["name"], prof["path"], prof.get("args", "")))

        repopulate()

        def launch_selected():
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("No selection", "Select a profile first.")
                return
            prof = self.profiles[sel[0]]
            cmd = [prof["path"]] + (prof.get("args", "").split() if prof.get("args") else [])
            try:
                subprocess.Popen(cmd)
                self.set_status("Launched profile: {0}".format(prof["name"]))
            except Exception as e:
                messagebox.showerror("Launch failed", str(e))

        def delete_selected():
            sel = listbox.curselection()
            if not sel:
                return
            del self.profiles[sel[0]]
            self._save_profiles()
            repopulate()

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        self._styled_button(btn_row, "Launch", launch_selected, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Delete", delete_selected, side="left", color=DANGER)
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- auto-close rules ----------------------------------------------

    def open_autoclose_rules(self):
        win = tk.Toplevel(self.root)
        win.title("Auto-Close Rules")
        win.geometry("420x260")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        tk.Label(
            win, text="Process names to auto-kill whenever seen\n(comma-separated, e.g. notepad.exe, calc.exe):",
            bg=BG_DARK, fg=FG_TEXT, font=FONT_UI, justify="left",
        ).pack(padx=10, pady=(12, 6), anchor="w")

        names_var = tk.StringVar(value=", ".join(sorted(self.autoclose_names)))
        entry = tk.Entry(win, textvariable=names_var, bg=BG_PANEL, fg=FG_TEXT,
                          insertbackground=FG_TEXT, relief="flat", font=FONT_UI)
        entry.pack(fill="x", padx=10)

        enabled_chk = tk.Checkbutton(
            win, text="Enable auto-close", variable=self.autoclose_enabled,
            bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_PANEL,
            activebackground=BG_DARK, activeforeground=FG_TEXT, font=FONT_UI,
        )
        enabled_chk.pack(anchor="w", padx=10, pady=10)

        tk.Label(
            win, text="Warning: this force-kills matching processes on every refresh, no confirmation.",
            bg=BG_DARK, fg=WARN, font=("Segoe UI", 8), wraplength=380, justify="left",
        ).pack(padx=10, anchor="w")

        def apply_rules():
            names = [n.strip().lower() for n in names_var.get().split(",") if n.strip()]
            self.autoclose_names = set(names)
            self.set_status("Auto-close rules updated: {0} name(s).".format(len(self.autoclose_names)))
            win.destroy()

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=10, pady=10)
        self._styled_button(btn_row, "Apply", apply_rules, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Close", win.destroy, side="right")

    # -- watch for new processes -------------------------------------------

    def _toggle_watch_new(self):
        if self.watch_new_enabled.get():
            self.known_pids = set(p.pid for p in psutil.process_iter())
            self._watch_tick()
        else:
            if self.watch_job is not None:
                self.root.after_cancel(self.watch_job)
                self.watch_job = None

    def _watch_tick(self):
        current_pids = set(p.pid for p in psutil.process_iter())
        new_pids = current_pids - self.known_pids
        for pid in new_pids:
            try:
                name = psutil.Process(pid).name()
            except Exception:
                name = "?"
            self.set_status("New process detected: {0} (pid {1})".format(name, pid))
        self.known_pids = current_pids

        if self.watch_new_enabled.get():
            self.watch_job = self.root.after(3000, self._watch_tick)

    # -- export log --------------------------------------------------------

    def do_export_log(self):
        if not self.log_history:
            messagebox.showinfo("Nothing to export", "No log entries yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Export log", defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            initialfile="prank_gui_log.txt",
        )
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write("\n".join(self.log_history))
            self.set_status("Log exported to {0}".format(path))
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # -- settings page -------------------------------------------------

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.geometry("460x560")
        win.configure(bg=BG_DARK)
        win.attributes("-topmost", True)

        def section(text):
            tk.Label(
                win, text=text, bg=BG_DARK, fg=ACCENT, font=FONT_UI_BOLD,
            ).pack(anchor="w", padx=12, pady=(14, 2))

        def hint(text):
            tk.Label(
                win, text=text, bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 8),
                wraplength=420, justify="left",
            ).pack(anchor="w", padx=12, pady=(0, 2))

        # -- default banner message --
        section("Default banner message")
        banner_var = tk.StringVar(value=self.message_var.get())
        tk.Entry(win, textvariable=banner_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=FONT_UI).pack(fill="x", padx=12)

        # -- default chrome url --
        section("Default Chrome URL")
        url_var = tk.StringVar(value=self.url_var.get())
        tk.Entry(win, textvariable=url_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=FONT_UI).pack(fill="x", padx=12)

        # -- startup behavior --
        section("Startup behavior")
        auto_refresh_start_var = tk.BooleanVar(value=self.auto_refresh_var.get())
        tk.Checkbutton(
            win, text="Start with Auto-refresh already on", variable=auto_refresh_start_var,
            bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_PANEL, activebackground=BG_DARK,
            activeforeground=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=12)

        watch_start_var = tk.BooleanVar(value=self.watch_new_enabled.get())
        tk.Checkbutton(
            win, text="Start with Watch New Processes already on", variable=watch_start_var,
            bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_PANEL, activebackground=BG_DARK,
            activeforeground=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=12)

        remember_size_var = tk.BooleanVar(value=self.remember_window_size.get())
        tk.Checkbutton(
            win, text="Remember window size/position between sessions", variable=remember_size_var,
            bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_PANEL, activebackground=BG_DARK,
            activeforeground=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=12)

        # -- keyboard shortcuts --
        section("Keyboard shortcuts (while window is focused)")
        hint("F5 Refresh | Delete Kill selected | Ctrl+F Search | "
             "Ctrl+B Banner | Ctrl+S Suspend | Ctrl+R Resume | Ctrl+Q Close window")
        shortcuts_var = tk.BooleanVar(value=self.keyboard_shortcuts_enabled.get())
        tk.Checkbutton(
            win, text="Enable keyboard shortcuts", variable=shortcuts_var,
            bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_PANEL, activebackground=BG_DARK,
            activeforeground=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=12)

        # -- auto-close rules (mirror of the dedicated dialog, for convenience) --
        section("Auto-close rules")
        autoclose_var = tk.StringVar(value=", ".join(sorted(self.autoclose_names)))
        tk.Entry(win, textvariable=autoclose_var, bg=BG_PANEL, fg=FG_TEXT,
                 insertbackground=FG_TEXT, relief="flat", font=FONT_UI).pack(fill="x", padx=12)
        autoclose_enabled_var = tk.BooleanVar(value=self.autoclose_enabled.get())
        tk.Checkbutton(
            win, text="Enable auto-close", variable=autoclose_enabled_var,
            bg=BG_DARK, fg=FG_TEXT, selectcolor=BG_PANEL, activebackground=BG_DARK,
            activeforeground=FG_TEXT, font=FONT_UI,
        ).pack(anchor="w", padx=12)

        # -- data locations --
        section("Data files")
        hint("Settings: {0}\nProfiles: {1}".format(self.settings_path, self.profiles_path))

        status_var = tk.StringVar(value="")
        tk.Label(win, textvariable=status_var, bg=BG_DARK, fg=OK_GREEN, font=FONT_UI).pack(
            anchor="w", padx=12, pady=(10, 0))

        def apply_settings():
            self.message_var.set(banner_var.get())
            self.url_var.set(url_var.get())
            self.auto_refresh_var.set(auto_refresh_start_var.get())
            self.watch_new_enabled.set(watch_start_var.get())
            self.remember_window_size.set(remember_size_var.get())
            self.keyboard_shortcuts_enabled.set(shortcuts_var.get())
            self.autoclose_names = set(
                n.strip().lower() for n in autoclose_var.get().split(",") if n.strip()
            )
            self.autoclose_enabled.set(autoclose_enabled_var.get())

            if self._save_settings():
                status_var.set("Saved.")
            else:
                status_var.set("Save failed -- see status bar.")

        def reset_defaults():
            banner_var.set("GOTCHA!")
            url_var.set("https://")
            auto_refresh_start_var.set(False)
            watch_start_var.set(False)
            remember_size_var.set(True)
            shortcuts_var.set(True)
            autoclose_var.set("")
            autoclose_enabled_var.set(False)

        btn_row = tk.Frame(win, bg=BG_DARK)
        btn_row.pack(fill="x", padx=12, pady=14)
        self._styled_button(btn_row, "Save", apply_settings, side="left", color=OK_GREEN)
        self._styled_button(btn_row, "Reset to Defaults", reset_defaults, side="left", color=WARN)
        self._styled_button(btn_row, "Close", win.destroy, side="right")


# ---------------------------------------------------------------------------
# Hotkey listener + app bootstrap
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    app = ProcessPranker(root)
    root.withdraw()  # start hidden; INSERT toggles visibility

    def toggle_window():
        # winfo_viewable() returns 0 while withdrawn/hidden, 1 when shown
        if root.winfo_viewable():
            root.withdraw()
        else:
            root.deiconify()
            root.lift()
            root.focus_force()
            app.refresh()

    keyboard.add_hotkey("insert", toggle_window)

    root.mainloop()


if __name__ == "__main__":
    main()
