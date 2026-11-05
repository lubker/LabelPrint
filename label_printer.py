#!/usr/bin/env python3
"""4Power factory label printer.

Desktop tool (Windows, tkinter - no third-party packages) that shows the
labels waiting in ``printlabels_que`` and prints them one at a time through
Brady Workstation:

1. Lists queued labels from the 4Power web API (``listprintlabels``), joined
   with ``device_types`` so every row carries its label template file,
   material number and LED texts.
2. On "Print" it writes exactly one row to the CSV data file that the Brady
   label templates (*.BWS) link to, then opens the template with the
   operating system's default application (Brady Workstation Custom Designer).
3. Optionally sends Ctrl+P / Enter to Brady Workstation so the label prints
   without touching the mouse ("auto print"), and confirms the print back to
   the API (``confirmprintlabel``) so the row disappears from the queue.

Configuration lives in ``config.json`` next to this file (copy
``config.example.json``). See README.md for the details.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import platform
import queue
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

APP_TITLE = "4Power Label Printer"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
SESSION_FILE = BASE_DIR / "session.json"
IS_WINDOWS = platform.system() == "Windows"

DEFAULT_CONFIG = {
    "api_url": "https://api.4power.dk/web_api",
    "ca_bundle": "",
    "verify_ssl": True,
    "csv_file": "data_file.csv",
    "labels_folder": ".",
    "url_template": "https://portal.4power.dk/welcome.php?DeviceID={device}",
    "url_templates_by_model": {},
    "labelfile_by_model": {},
    "csv_columns": [
        "Device", "URL", "Materialnr", "Model", "Date",
        "Led1", "Led2", "Led3", "Label", "Emballage", "Version",
    ],
    "csv_encoding": "utf-8",
    "date_format": "%Y-%m-%d %H:%M:%S",
    "refresh_seconds": 15,
    "confirm_mode": "ask",
    "close_brady_before_open": False,
    "brady_process_name": "",
    "auto_print": {
        "enabled": False,
        "window_title": "Brady Workstation",
        "window_wait_seconds": 8,
        "print_dialog_wait_seconds": 3,
        "close_document_after_print": False,
        "confirm_after_auto_print": True,
    },
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """Load config.json and fill missing keys with defaults."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def resolve_path(config: dict, value: str) -> Path:
    """Resolve a path from config relative to the tool folder."""
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


def build_ssl_context(config: dict, log=print) -> ssl.SSLContext:
    """TLS settings for the API connection.

    Python on Windows only trusts the root certificates that Windows has
    already downloaded, and a fresh PC often has not fetched the Amazon root
    that api.4power.dk chains to - the symptom is CERTIFICATE_VERIFY_FAILED
    "unable to get local issuer certificate". In order of preference:
    a CA bundle given in config, the `truststore` package (asks Windows and
    triggers the root download), the `certifi` package, then the default.
    """
    if not config.get("verify_ssl", True):
        log("WARNING: verify_ssl is false - the API certificate is not checked.")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    bundle = config.get("ca_bundle") or ""
    if bundle:
        log(f"TLS trust: CA bundle {bundle}")
        return ssl.create_default_context(cafile=str(resolve_path(config, bundle)))
    try:
        import truststore  # type: ignore
        log("TLS trust: Windows certificate store via truststore")
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi  # type: ignore
        log("TLS trust: certifi bundle")
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    log(f"TLS trust: Python default store ({sys.executable})")
    return ssl.create_default_context()


SSL_HELP = (
    "The API certificate could not be verified. Fix one of these ways:\n"
    "  1. Open https://api.4power.dk in Edge once (Windows then downloads the missing root certificate) and retry.\n"
    "  2. Run:  py -3 -m pip install truststore   (or certifi) and restart the tool.\n"
    "  3. Set \"ca_bundle\" in config.json to a PEM file with the root certificates."
)


class ApiClient:
    """Minimal client for the 4Power web API (POST JSON, `function` field)."""

    def __init__(self, api_url: str, ssl_context: ssl.SSLContext | None = None):
        self.api_url = api_url
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.user_id = None
        self.token = None
        self.email = None
        self.load_session()

    # -- session persistence -------------------------------------------------
    def load_session(self) -> None:
        if SESSION_FILE.exists():
            try:
                with open(SESSION_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.user_id = data.get("userID")
                self.token = data.get("token")
                self.email = data.get("email")
            except (OSError, ValueError):
                pass

    def save_session(self) -> None:
        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"userID": self.user_id, "token": self.token, "email": self.email}, fh)

    def clear_session(self) -> None:
        self.user_id = self.token = self.email = None
        try:
            SESSION_FILE.unlink()
        except FileNotFoundError:
            pass

    @property
    def logged_in(self) -> bool:
        return bool(self.user_id and self.token)

    # -- low level ---------------------------------------------------------------
    def call(self, function: str, timeout: float = 20, **params) -> dict:
        payload = {"function": function}
        if self.logged_in:
            payload["userID"] = self.user_id
            payload["token"] = self.token
        payload.update(params)
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ssl_context) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            status = err.code
        except urllib.error.URLError as err:
            reason = err.reason
            if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                raise ApiError(0, f"{SSL_HELP}\n\n({reason})") from err
            raise ApiError(0, f"Could not reach {self.api_url}: {reason}") from err
        except (TimeoutError, OSError) as err:
            raise ApiError(0, f"Could not reach {self.api_url}: {err}") from err
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"message": raw}
        if status >= 400:
            message = data.get("message", raw)
            if status == 400 and str(message).startswith("Invalid function"):
                message = (f"{message}\n\nThe API at {self.api_url} does not have the label-printing "
                           "functions yet. Deploy the Backend API Lambda (Backend API/deploy.sh) "
                           "with modules/printlabels.mjs.")
            raise ApiError(status, message)
        return data

    # -- functions ---------------------------------------------------------------
    def login(self, email: str, password: str) -> None:
        # app=True stores the token in users.app_token_key so the operator's
        # browser session (web_token_key) is not logged out. Use a dedicated
        # factory user account anyway - see README.
        # The API compares against a SHA-256 hex digest of the password, the
        # same way the web app's login form (js-sha256) sends it.
        hashed = hashlib.sha256(password.encode("utf-8")).hexdigest()
        data = self.call("login", email=email, password=hashed, app=True)
        user = data.get("user") or {}
        if not user.get("id") or not user.get("token"):
            raise ApiError(500, "Login response did not contain a user token")
        self.user_id = user["id"]
        self.token = user["token"]
        self.email = user.get("email") or email
        self.save_session()

    def list_labels(self, include_printed: bool = False, limit: int = 300) -> dict:
        return self.call("listprintlabels", includePrinted=include_printed, limit=limit)

    def confirm(self, label_id: int) -> dict:
        return self.call("confirmprintlabel", id=label_id)

    def requeue(self, label_id: int) -> dict:
        return self.call("requeueprintlabel", id=label_id)

    def add(self, model: str, device_id: str, version: str | None) -> dict:
        return self.call("addprintlabel", model=model, device_id=device_id, version=version or None)


# ---------------------------------------------------------------------------
# Label data -> CSV
# ---------------------------------------------------------------------------
def format_timestamp(value: str | None, date_format: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()  # local time on the factory PC
        return parsed.strftime(date_format)
    except ValueError:
        return value


def build_url(config: dict, label: dict) -> str:
    template = config.get("url_templates_by_model", {}).get(label.get("model")) or config["url_template"]
    return template.format(device=label.get("device_id", ""), model=label.get("model", ""))


def label_file_name(config: dict, label: dict) -> str:
    """Template file for a label: config override first, then device_types.labelfile."""
    override = config.get("labelfile_by_model", {}).get(label.get("model"))
    return (override or label.get("labelfile") or "").strip()


def csv_row_values(config: dict, label: dict) -> dict:
    """All values a CSV column can reference; keys are the column names."""
    return {
        "Id": label.get("id", ""),
        "Device": label.get("device_id", ""),
        "URL": build_url(config, label),
        "Materialnr": label.get("materialnumber") or "",
        "Model": label.get("model", ""),
        "Version": label.get("version") or "",
        "Date": format_timestamp(label.get("timestamp"), config["date_format"]),
        "Led1": label.get("led1") or "",
        "Led2": label.get("led2") or "",
        "Led3": label.get("led3") or "",
        "Label": label_file_name(config, label),
        "Emballage": label.get("emballagefile") or "",
        "Configuration": label.get("configuration") or "",
        "Description": label.get("description") or "",
    }


def write_csv(config: dict, label: dict) -> Path:
    """Write header + exactly one row (Brady accepts one label at a time)."""
    columns = config["csv_columns"]
    values = csv_row_values(config, label)
    unknown = [c for c in columns if c not in values]
    if unknown:
        raise ValueError(f"Unknown csv_columns in config: {', '.join(unknown)}. "
                         f"Available: {', '.join(values)}")
    path = resolve_path(config, config["csv_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding=config["csv_encoding"]) as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerow([values[c] for c in columns])
    return path


def open_file(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def kill_process(name: str) -> None:
    if not name:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", name], capture_output=True)


# ---------------------------------------------------------------------------
# Windows automation (Ctrl+P / Enter into Brady Workstation)
# ---------------------------------------------------------------------------
VK_MENU = 0x12
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_P = 0x50
VK_W = 0x57
VK_F4 = 0x73
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9


def find_window(title_fragment: str):
    """Return the handle of the first visible top-level window whose title contains the fragment."""
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    fragment = title_fragment.lower()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined]
    def enum_proc(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if fragment in buf.value.lower():
                    found.append(hwnd)
                    return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else None


def press_keys(*vks: int) -> None:
    """Press the given virtual keys together, then release in reverse order."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def focus_window(hwnd) -> None:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    # Windows refuses SetForegroundWindow from a background process unless the
    # process recently received input; tapping Alt is the classic workaround.
    press_keys(VK_MENU)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)


def auto_print(settings: dict, log) -> bool:
    """Wait for the Brady window, then send Ctrl+P and Enter. Returns True when keys were sent."""
    if not IS_WINDOWS:
        log("Auto print is only available on Windows.")
        return False
    title = settings.get("window_title") or "Brady Workstation"
    deadline = time.time() + float(settings.get("window_wait_seconds", 8))
    hwnd = None
    while time.time() < deadline:
        hwnd = find_window(title)
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        log(f"Auto print: no window with '{title}' in the title appeared - print manually.")
        return False
    # Give the document a moment to finish loading after the window shows up.
    time.sleep(1.5)
    focus_window(hwnd)
    log("Auto print: sending Ctrl+P")
    press_keys(VK_CONTROL, VK_P)
    time.sleep(float(settings.get("print_dialog_wait_seconds", 3)))
    log("Auto print: sending Enter")
    press_keys(VK_RETURN)
    if settings.get("close_document_after_print"):
        time.sleep(2)
        hwnd = find_window(title)
        if hwnd:
            focus_window(hwnd)
            press_keys(VK_CONTROL, VK_W)
    return True


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui(config: dict) -> None:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    events: "queue.Queue[tuple]" = queue.Queue()
    startup_messages: list[str] = []
    api = ApiClient(config["api_url"], build_ssl_context(config, startup_messages.append))

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1100x640")
    root.minsize(820, 480)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=(18, 10))
    style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
    style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    show_printed = tk.BooleanVar(value=False)
    auto_refresh = tk.BooleanVar(value=True)
    auto_print_var = tk.BooleanVar(value=bool(config["auto_print"].get("enabled")))
    status_var = tk.StringVar(value="Not connected")
    pending_var = tk.StringVar(value="")
    labels_by_iid: dict[str, dict] = {}
    busy = {"printing": False}

    # -- layout ----------------------------------------------------------------
    top = ttk.Frame(root, padding=(10, 8))
    top.pack(fill="x")
    ttk.Label(top, textvariable=status_var).pack(side="left")
    ttk.Label(top, textvariable=pending_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(16, 0))
    ttk.Button(top, text="Log out", command=lambda: do_logout()).pack(side="right")
    ttk.Button(top, text="Queue label…", command=lambda: add_label_dialog()).pack(side="right", padx=6)
    ttk.Button(top, text="Refresh", command=lambda: refresh()).pack(side="right", padx=6)
    ttk.Checkbutton(top, text="Auto refresh", variable=auto_refresh).pack(side="right", padx=6)
    ttk.Checkbutton(top, text="Show printed", variable=show_printed, command=lambda: refresh()).pack(side="right", padx=6)

    middle = ttk.Frame(root, padding=(10, 0))
    middle.pack(fill="both", expand=True)

    columns = ("id", "device_id", "model", "version", "timestamp", "labelfile", "materialnumber", "printed")
    headings = {
        "id": ("ID", 60), "device_id": ("Device ID", 150), "model": ("Model", 150),
        "version": ("Version", 90), "timestamp": ("Queued", 165), "labelfile": ("Label file", 220),
        "materialnumber": ("Material nr", 120), "printed": ("Printed", 70),
    }
    tree = ttk.Treeview(middle, columns=columns, show="headings", selectmode="browse")
    for col in columns:
        text, width = headings[col]
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w", stretch=(col in ("labelfile", "model")))
    tree.tag_configure("printed", foreground="#888888")
    tree.tag_configure("nolabel", foreground="#b00020")
    scrollbar = ttk.Scrollbar(middle, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    tree.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    actions = ttk.Frame(root, padding=(10, 8))
    actions.pack(fill="x")
    print_btn = ttk.Button(actions, text="Print selected", style="Big.TButton", command=lambda: print_selected())
    print_btn.pack(side="left")
    next_btn = ttk.Button(actions, text="Print next in queue", style="Big.TButton", command=lambda: print_next())
    next_btn.pack(side="left", padx=8)
    ttk.Checkbutton(actions, text="Auto print (Ctrl+P / Enter in Brady)", variable=auto_print_var).pack(side="left", padx=12)
    ttk.Button(actions, text="Re-queue", command=lambda: requeue_selected()).pack(side="right")
    ttk.Button(actions, text="Mark as printed", command=lambda: confirm_selected()).pack(side="right", padx=6)
    ttk.Button(actions, text="Open CSV folder", command=lambda: open_file(resolve_path(config, config["csv_file"]).parent)).pack(side="right", padx=6)

    log_frame = ttk.LabelFrame(root, text="Log", padding=(6, 4))
    log_frame.pack(fill="both", padx=10, pady=(0, 10))
    log_text = tk.Text(log_frame, height=7, state="disabled", font=("Consolas", 9), wrap="word")
    log_text.pack(fill="both", expand=True)

    def log(message: str) -> None:
        events.put(("log", message))

    def _append_log(message: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    # -- background work -------------------------------------------------------
    def run_async(func, on_done=None):
        def worker():
            try:
                result = func()
            except Exception as err:  # noqa: BLE001 - surfaced in the UI
                events.put(("error", err))
                return
            if on_done:
                events.put(("done", on_done, result))
        threading.Thread(target=worker, daemon=True).start()

    def process_events() -> None:
        try:
            while True:
                event = events.get_nowait()
                kind = event[0]
                if kind == "log":
                    _append_log(event[1])
                elif kind == "done":
                    event[1](event[2])
                elif kind == "error":
                    handle_error(event[1])
        except queue.Empty:
            pass
        root.after(100, process_events)

    def handle_error(err: Exception) -> None:
        busy["printing"] = False
        if isinstance(err, ApiError) and err.status == 401:
            _append_log("Session expired - please log in again.")
            api.clear_session()
            status_var.set("Not connected")
            login_dialog()
            return
        _append_log(f"ERROR: {err}")
        messagebox.showerror(APP_TITLE, str(err))

    # -- queue listing -----------------------------------------------------------
    def refresh() -> None:
        if not api.logged_in:
            return
        run_async(lambda: api.list_labels(include_printed=show_printed.get()), populate)

    def populate(data: dict) -> None:
        selected = tree.selection()
        selected_id = labels_by_iid[selected[0]]["id"] if selected else None
        tree.delete(*tree.get_children())
        labels_by_iid.clear()
        for label in data.get("labels", []):
            iid = str(label["id"])
            labels_by_iid[iid] = label
            tags = []
            if label.get("printed"):
                tags.append("printed")
            if not label_file_name(config, label):
                tags.append("nolabel")
            tree.insert("", "end", iid=iid, tags=tags, values=(
                label["id"], label.get("device_id", ""), label.get("model", ""),
                label.get("version") or "", format_timestamp(label.get("timestamp"), config["date_format"]),
                label_file_name(config, label) or "(no label file - check device_types)",
                label.get("materialnumber") or "", "yes" if label.get("printed") else "",
            ))
        if selected_id is not None and str(selected_id) in labels_by_iid:
            tree.selection_set(str(selected_id))
        pending_var.set(f"{data.get('pending', 0)} label(s) waiting")
        status_var.set(f"Connected as {api.email or api.user_id}")

    def auto_refresh_tick() -> None:
        if auto_refresh.get() and not busy["printing"]:
            refresh()
        root.after(max(3, int(config["refresh_seconds"])) * 1000, auto_refresh_tick)

    # -- printing ----------------------------------------------------------------
    def selected_label() -> dict | None:
        selection = tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Select a label in the list first.")
            return None
        return labels_by_iid[selection[0]]

    def print_selected() -> None:
        label = selected_label()
        if label:
            print_label(label)

    def print_next() -> None:
        pending = [l for l in labels_by_iid.values() if not l.get("printed")]
        if not pending:
            messagebox.showinfo(APP_TITLE, "The queue is empty.")
            return
        label = pending[0]
        tree.selection_set(str(label["id"]))
        tree.see(str(label["id"]))
        print_label(label)

    def print_label(label: dict) -> None:
        if busy["printing"]:
            messagebox.showinfo(APP_TITLE, "A label is already being printed. Confirm it first.")
            return
        file_name = label_file_name(config, label)
        if not file_name:
            messagebox.showerror(APP_TITLE, f"No label file is defined for model '{label.get('model')}'.\n\n"
                                            "Set device_types.labelfile for the model, or add it under "
                                            "labelfile_by_model in config.json.")
            return
        label_path = Path(file_name)
        if not label_path.is_absolute():
            label_path = resolve_path(config, config["labels_folder"]) / file_name
        if not label_path.exists():
            messagebox.showerror(APP_TITLE, f"Label file not found:\n{label_path}")
            return
        busy["printing"] = True

        def work():
            if config.get("close_brady_before_open") and config.get("brady_process_name"):
                log(f"Closing {config['brady_process_name']} so the data file is re-read")
                kill_process(config["brady_process_name"])
                time.sleep(1.5)
            try:
                csv_path = write_csv(config, label)
            except PermissionError as err:
                raise RuntimeError(f"Could not write {config['csv_file']} - it is probably still open "
                                   f"in Brady Workstation. Close the document and try again.\n\n{err}") from err
            log(f"Wrote {csv_path.name}: {label.get('device_id')} ({label.get('model')})")
            log(f"Opening {label_path}")
            open_file(label_path)
            printed = False
            if auto_print_var.get():
                printed = auto_print(config["auto_print"], log)
            return printed

        run_async(work, lambda printed: after_open(label, printed))

    def after_open(label: dict, auto_printed: bool) -> None:
        mode = config.get("confirm_mode", "ask")
        if mode == "auto" or (mode == "ask" and auto_printed and config["auto_print"].get("confirm_after_auto_print", True)):
            confirm_label(label)
        elif mode == "ask":
            root.after(300, lambda: ask_confirm(label))
        else:
            busy["printing"] = False
            _append_log("Print the label in Brady, then click 'Mark as printed'.")

    def ask_confirm(label: dict) -> None:
        root.lift()
        ok = messagebox.askyesno(
            APP_TITLE,
            f"Device {label.get('device_id')} ({label.get('model')})\n\n"
            "Was the label printed OK?\n\nYes = mark as printed and continue\nNo = keep it in the queue",
        )
        if ok:
            confirm_label(label)
        else:
            busy["printing"] = False
            _append_log(f"Kept {label.get('device_id')} in the queue.")

    def confirm_label(label: dict) -> None:
        def done(_result):
            busy["printing"] = False
            _append_log(f"Confirmed printed: {label.get('device_id')}")
            refresh()
        run_async(lambda: api.confirm(label["id"]), done)

    def confirm_selected() -> None:
        label = selected_label()
        if label:
            confirm_label(label)

    def requeue_selected() -> None:
        label = selected_label()
        if not label:
            return
        def done(_result):
            _append_log(f"Re-queued {label.get('device_id')} for printing")
            refresh()
        run_async(lambda: api.requeue(label["id"]), done)

    # -- dialogs -----------------------------------------------------------------
    def add_label_dialog() -> None:
        model = simpledialog.askstring(APP_TITLE, "Model (must match device_types.model):", parent=root)
        if not model:
            return
        device_id = simpledialog.askstring(APP_TITLE, "Device ID:", parent=root)
        if not device_id:
            return
        version = simpledialog.askstring(APP_TITLE, "Version (optional):", parent=root) or ""
        def done(result):
            _append_log(f"Queued {result.get('label', {}).get('device_id')}")
            refresh()
        run_async(lambda: api.add(model.strip(), device_id.strip(), version.strip()), done)

    def login_dialog() -> None:
        dialog = tk.Toplevel(root)
        dialog.title("Log in to 4Power")
        dialog.transient(root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack()
        ttk.Label(frame, text="Log in with a 4Power admin/operator account.").grid(column=0, row=0, columnspan=2, pady=(0, 10))
        ttk.Label(frame, text="E-mail").grid(column=0, row=1, sticky="e", padx=(0, 8), pady=3)
        email_entry = ttk.Entry(frame, width=32)
        email_entry.grid(column=1, row=1, pady=3)
        ttk.Label(frame, text="Password").grid(column=0, row=2, sticky="e", padx=(0, 8), pady=3)
        password_entry = ttk.Entry(frame, width=32, show="•")
        password_entry.grid(column=1, row=2, pady=3)
        error_var = tk.StringVar()
        ttk.Label(frame, textvariable=error_var, foreground="#b00020", wraplength=420, justify="left").grid(column=0, row=3, columnspan=2, sticky="w")

        def submit(_event=None):
            email = email_entry.get().strip()
            password = password_entry.get()
            if not email or not password:
                error_var.set("Enter e-mail and password")
                return
            error_var.set("Logging in…")
            def done(error):
                if error:
                    error_var.set(str(error))
                    return
                dialog.destroy()
                _append_log(f"Logged in as {api.email}")
                refresh()
            def work():
                try:
                    api.login(email, password)
                    return None
                except Exception as err:  # noqa: BLE001 - shown inside the dialog
                    return err
            run_async(work, done)

        ttk.Button(frame, text="Log in", command=submit).grid(column=0, row=4, columnspan=2, pady=(10, 0))
        password_entry.bind("<Return>", submit)
        email_entry.bind("<Return>", submit)
        email_entry.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", lambda: (dialog.destroy(), root.destroy()))

    def do_logout() -> None:
        api.clear_session()
        tree.delete(*tree.get_children())
        labels_by_iid.clear()
        status_var.set("Not connected")
        pending_var.set("")
        login_dialog()

    # -- wiring ------------------------------------------------------------------
    tree.bind("<Double-1>", lambda _e: print_selected())
    tree.bind("<Return>", lambda _e: print_selected())
    root.bind("<F5>", lambda _e: refresh())

    _append_log(f"API: {config['api_url']}")
    for message in startup_messages:
        _append_log(message)
    _append_log(f"CSV data file: {resolve_path(config, config['csv_file'])}")
    _append_log(f"Label templates folder: {resolve_path(config, config['labels_folder'])}")
    if not IS_WINDOWS:
        _append_log("Not running on Windows: labels open with the OS default app, auto print is disabled.")
    root.after(100, process_events)
    root.after(1500, auto_refresh_tick)
    if api.logged_in:
        refresh()
    else:
        root.after(200, login_dialog)
    root.mainloop()


def main() -> None:
    config = load_config()
    if "--print-config" in sys.argv:
        print(json.dumps(config, indent=2))
        return
    run_gui(config)


if __name__ == "__main__":
    main()
