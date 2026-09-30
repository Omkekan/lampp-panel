"""LAMPP Panel - control panel for a local Apache / MariaDB / PHP web development stack.

Standalone: it manages Fedora's own packaged services (httpd, mariadb, php-fpm, ...)
through systemd. XAMPP is not needed.

The GUI always runs as a normal user. Anything that needs root goes through
pkexec + /usr/libexec/lampp-panel-helper, which accepts only a fixed set of
actions. No shell strings are ever built from file names or user input.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from urllib.parse import quote

import psutil

APP_NAME = "LAMPP Panel"
HELPER = "/usr/libexec/lampp-panel-helper"
HTDOCS = "/var/www/html"

POLL_SECONDS = 2
CONSOLE_MAX_LINES = 1000
DEVNULL = subprocess.DEVNULL

# Log/config sources are tuples: ("journal", unit) | ("file", path) |
# ("rootfile", path, helper_log_id)  - the last kind falls back to the root helper
# when Fedora keeps the file root-only (e.g. /var/log/httpd).
# The helper keeps its own copy of the service/package table.
SERVICES = {
    "apache": {
        "label": "Apache", "unit": "httpd.service", "ports": ["80", "443"],
        "admin": "http://localhost/", "pkgs": ("httpd",),
        "logs": {
            "journal": ("journal", "httpd.service"),
            "error_log": ("rootfile", "/var/log/httpd/error_log", "apache-error"),
            "access_log": ("rootfile", "/var/log/httpd/access_log", "apache-access"),
        },
        "configs": {"httpd.conf": ("file", "/etc/httpd/conf/httpd.conf")},
    },
    "mariadb": {
        "label": "MariaDB", "unit": "mariadb.service", "ports": ["3306"],
        "admin": "http://localhost/phpmyadmin", "pkgs": ("mariadb-server",),
        "logs": {
            "journal": ("journal", "mariadb.service"),
            "mariadb.log": ("rootfile", "/var/log/mariadb/mariadb.log", "mariadb"),
        },
        "configs": {"my.cnf": ("file", "/etc/my.cnf"),
                    "mariadb-server.cnf": ("file", "/etc/my.cnf.d/mariadb-server.cnf")},
    },
    "php-fpm": {
        "label": "PHP-FPM", "unit": "php-fpm.service", "ports": [],
        "admin": None, "pkgs": ("php-fpm", "php-mysqlnd"),
        "logs": {"journal": ("journal", "php-fpm.service")},
        "configs": {"php.ini": ("file", "/etc/php.ini"),
                    "php-fpm.conf": ("file", "/etc/php-fpm.conf"),
                    "www.conf": ("file", "/etc/php-fpm.d/www.conf")},
    },
    "ftp": {
        "label": "FTP (vsftpd)", "unit": "vsftpd.service", "ports": ["21"],
        "admin": None, "pkgs": ("vsftpd",),
        "logs": {"journal": ("journal", "vsftpd.service")},
        "configs": {"vsftpd.conf": ("file", "/etc/vsftpd/vsftpd.conf")},
    },
    "postfix": {
        "label": "Postfix", "unit": "postfix.service", "ports": ["25"],
        "admin": None, "pkgs": ("postfix",),
        "logs": {"journal": ("journal", "postfix.service")},
        "configs": {"main.cf": ("file", "/etc/postfix/main.cf")},
    },
    "tomcat": {
        "label": "Tomcat", "unit": "tomcat.service", "ports": ["8080"],
        "admin": "http://localhost:8080", "pkgs": ("tomcat",),
        "logs": {"journal": ("journal", "tomcat.service")},
        "configs": {"server.xml": ("file", "/etc/tomcat/server.xml")},
    },
}


# --------------------------------------------------------------------------
# Status helpers (pure functions, safe to call from the worker thread)
# --------------------------------------------------------------------------
def query_units():
    """{unit: {property: value}} for all services, using a single systemctl call."""
    units = [c["unit"] for c in SERVICES.values()]
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "Id,LoadState,ActiveState,UnitFileState,MainPID", "--", *units],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    info = {}
    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in props:
            info[props["Id"]] = props
    return info


def proc_label(pid):
    """Readable program name for messages."""
    try:
        proc = psutil.Process(pid)
        cmd = proc.cmdline()
        if cmd and cmd[0]:
            return os.path.basename(cmd[0].split(" ")[0])
        return proc.name()
    except psutil.Error:
        return "unknown"


def prefs_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lampp-panel" / "prefs.json"


class LamppPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        w = min(1150, self.winfo_screenwidth() - 80)
        h = min(760, self.winfo_screenheight() - 120)
        self.geometry(f"{w}x{h}")
        self.minsize(820, 520)

        self.ui = {}                     # per-service widgets and state
        self.busy = set()                # service ids (or "_global") with an action running
        self.ui_queue = queue.Queue()    # worker threads -> main thread
        self.stop_event = threading.Event()
        self._proc_cache = {}            # only touched by the status thread
        self._reported_missing = False
        self._frames, self._labels, self._buttons = [], [], []

        self.prefs = self.load_prefs()
        self.is_dark = bool(self.prefs.get("dark_mode", False))
        self.update_theme_colors()

        self.create_widgets()
        self.apply_theme()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.log_msg(f"{APP_NAME} started.")
        if not os.path.exists(HELPER):
            self.log_msg(f"Privileged helper missing ({HELPER}); start/stop will not work. "
                         "Install the lampp-panel package.", error=True)

        self.after(100, self._drain_queue)
        threading.Thread(target=self.status_loop, daemon=True).start()

    # ---------------------------------------------------------------- theme
    def update_theme_colors(self):
        if self.is_dark:
            self.c_bg, self.c_fg, self.c_table = "#1e1e1e", "#d4d4d4", "#252526"
            self.c_run, self.c_run_fg = "#0e5a1e", "#ffffff"
            self.c_fail, self.c_fail_fg = "#6b1f1f", "#ffffff"
            self.c_btn, self.c_dim = "#333333", "#777777"
            self.c_console, self.c_console_fg, self.c_err = "#000000", "#00ff00", "#ff5555"
        else:
            self.c_bg, self.c_fg, self.c_table = "#f0f0f0", "#000000", "#ffffff"
            self.c_run, self.c_run_fg = "#ccffcc", "#000000"
            self.c_fail, self.c_fail_fg = "#ffd0d0", "#000000"
            self.c_btn, self.c_dim = "#e0e0e0", "#888888"
            self.c_console, self.c_console_fg, self.c_err = "#ffffff", "#000000", "#c00000"

    def apply_theme(self):
        self.configure(bg=self.c_bg)
        for w in self._frames:
            w.config(bg=self.c_bg)
        for w in self._labels:
            w.config(bg=self.c_bg, fg=self.c_fg)
        for w in self._buttons:
            w.config(bg=self.c_btn, fg=self.c_fg, activebackground=self.c_btn,
                     activeforeground=self.c_fg, disabledforeground=self.c_dim)
        self.title_lbl.config(bg=self.c_bg)
        self.console.config(bg=self.c_console, fg=self.c_console_fg)
        self.console.tag_config("err", foreground=self.c_err)
        self.theme_btn.config(text="Light theme" if self.is_dark else "Dark theme")
        for sid in self.ui:
            self.update_buttons(sid)

    def toggle_theme(self):
        self.is_dark = not self.is_dark
        self.prefs["dark_mode"] = self.is_dark
        self.save_prefs()
        self.update_theme_colors()
        self.apply_theme()
        self.log_msg(f"Theme switched to {'dark' if self.is_dark else 'light'}.")

    # ---------------------------------------------------------------- prefs
    def load_prefs(self):
        try:
            with open(prefs_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_prefs(self):
        try:
            path = prefs_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f)
        except OSError as exc:
            self.log_msg(f"Could not save preferences: {exc}", error=True)

    # -------------------------------------------------------------- widgets
    def _frame(self, parent, **kw):
        w = tk.Frame(parent, **kw)
        self._frames.append(w)
        return w

    def _label(self, parent, **kw):
        w = tk.Label(parent, **kw)
        self._labels.append(w)
        return w

    def _button(self, parent, **kw):
        w = tk.Button(parent, **kw)
        self._buttons.append(w)
        return w

    def create_widgets(self):
        header = self._frame(self)
        header.pack(fill="x", padx=10, pady=(10, 6))
        self.title_lbl = tk.Label(header, text=APP_NAME, font=("Arial", 20, "bold"), fg="#3c78d8")
        self.title_lbl.pack(side="left")
        self._label(header, text="local web development stack", font=("Arial", 12)).pack(side="left", padx=8)

        main = self._frame(self)
        main.pack(fill="x", padx=10)
        table = self._frame(main)
        table.pack(side="left", fill="both", expand=True)
        toolbar = self._frame(main)
        toolbar.pack(side="right", fill="y", padx=(10, 0))

        for col, text in enumerate(["Boot", "Module", "Status / PID(s)", "Port(s)", "CPU / RAM", "Actions"]):
            self._label(table, text=text, font=("Arial", 9, "bold")).grid(
                row=0, column=col, padx=6, pady=5, sticky="w")

        for row, (sid, cfg) in enumerate(SERVICES.items(), start=1):
            boot = tk.Button(table, width=3, font=("Arial", 10, "bold"),
                             command=lambda s=sid: self.toggle_boot(s))
            boot.grid(row=row, column=0, padx=6)
            mod = tk.Label(table, text=cfg["label"], font=("Arial", 10, "bold"),
                           width=14, anchor="w", padx=6)
            mod.grid(row=row, column=1, pady=4, sticky="w")
            pids = self._label(table, font=("Arial", 9), width=14, anchor="w")
            pids.grid(row=row, column=2, padx=6)
            ports = self._label(table, font=("Arial", 9), width=10, anchor="w")
            ports.grid(row=row, column=3, padx=6)
            cpu = self._label(table, font=("Arial", 9), width=16, anchor="w")
            cpu.grid(row=row, column=4, padx=6)

            act = self._frame(table)
            act.grid(row=row, column=5, sticky="w", padx=6)
            start = self._button(act, text="Start", width=7,
                                 command=lambda s=sid: self.toggle_service(s))
            start.pack(side="left", padx=1)
            restart = self._button(act, text="Restart", width=7,
                                   command=lambda s=sid: self.restart_service(s))
            restart.pack(side="left", padx=1)
            self._button(act, text="Admin", width=6, command=lambda s=sid: self.open_admin(s),
                         state="normal" if cfg["admin"] else "disabled").pack(side="left", padx=1)
            self._button(act, text="Config", width=6,
                         command=lambda s=sid: self.open_config(s)).pack(side="left", padx=1)
            self._button(act, text="Logs", width=6,
                         command=lambda s=sid: self.open_logs(s)).pack(side="left", padx=1)

            self.ui[sid] = {"boot": boot, "mod": mod, "pids": pids, "ports": ports, "cpu": cpu,
                            "start": start, "restart": restart, "running": False,
                            "installed": True, "failed": False, "boot_state": None, "pid_list": []}

        for text, cmd in (("Start LAMP", self.start_stack),
                          ("Stop LAMP", self.stop_stack),
                          ("Launch Pad", self.open_launchpad),
                          ("Grant Access", self.grant_access),
                          ("DB Backup", self.backup_db),
                          ("Clear Logs", self.clear_logs)):
            self._button(toolbar, text=text, width=12, command=cmd).pack(pady=3)
        self.theme_btn = self._button(toolbar, text="Dark theme", width=12, command=self.toggle_theme)
        self.theme_btn.pack(pady=3)
        self._button(toolbar, text="Quit", width=12, command=self.on_close).pack(side="bottom", pady=5)

        console_frame = self._frame(self)
        console_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.console = scrolledtext.ScrolledText(console_frame, height=8, font=("Courier", 9))
        self.console.pack(fill="both", expand=True)
        self.console.configure(state="disabled")

    # ---------------------------------------------- main-thread plumbing
    def post(self, fn, *args, **kwargs):
        """Schedule fn on the Tk main thread. The only safe way for workers to touch the UI."""
        self.ui_queue.put((fn, args, kwargs))

    def _drain_queue(self):
        try:
            while True:
                fn, args, kwargs = self.ui_queue.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(100, self._drain_queue)

    def log_msg(self, msg, error=False):
        """Main thread only - workers must use self.post(self.log_msg, ...)."""
        stamp = datetime.now().strftime("%H:%M:%S")
        c = self.console
        c.configure(state="normal")
        start = c.index("end-1c")
        c.insert(tk.END, f"[{stamp}] {msg}\n")
        if error:
            c.tag_add("err", start, "end-1c")
        lines = int(c.index("end-1c").split(".")[0])
        if lines > CONSOLE_MAX_LINES:
            c.delete("1.0", f"{lines - CONSOLE_MAX_LINES}.0")
        c.see(tk.END)
        c.configure(state="disabled")

    def on_close(self):
        self.stop_event.set()
        self.destroy()

    def spawn(self, cmd):
        """Launch a detached GUI program. cmd is always an argument list, never a shell string."""
        try:
            subprocess.Popen(cmd, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            self.log_msg(f"Could not run {cmd[0]}: {exc}", error=True)

    # ------------------------------------------------------- status thread
    def read_stats(self, pids):
        cpu, rss = 0.0, 0
        for pid in pids:
            try:
                proc = self._proc_cache.get(pid)
                if proc is None:
                    proc = self._proc_cache[pid] = psutil.Process(pid)
                cpu += proc.cpu_percent(None)   # since the previous call = real current usage
                rss += proc.memory_info().rss
            except psutil.Error:
                self._proc_cache.pop(pid, None)
        return cpu, rss / (1024 * 1024)

    def status_loop(self):
        last_err = None
        while not self.stop_event.is_set():
            try:
                info = query_units()
                if not info:
                    raise RuntimeError("systemctl is not available")
                snap, seen = {}, set()
                for sid, cfg in SERVICES.items():
                    p = info.get(cfg["unit"], {})
                    installed = p.get("LoadState", "not-found") != "not-found"
                    state = p.get("ActiveState", "")
                    pids = []
                    main = p.get("MainPID", "0")
                    if state == "active" and main.isdigit() and int(main) > 0:
                        pids = [int(main)]
                        try:
                            pids += [c.pid for c in psutil.Process(int(main)).children(recursive=True)]
                        except psutil.Error:
                            pass
                    seen.update(pids)
                    cpu, ram = self.read_stats(pids) if pids else (0.0, 0.0)
                    ufs = p.get("UnitFileState", "")
                    boot = True if ufs.startswith("enabled") else (False if ufs == "disabled" else None)
                    snap[sid] = {"installed": installed, "running": state == "active",
                                 "failed": state == "failed", "pids": sorted(pids),
                                 "cpu": cpu, "ram": ram, "boot": boot}
                for pid in [p for p in self._proc_cache if p not in seen]:
                    del self._proc_cache[pid]
                self.post(self.apply_status, snap)
                last_err = None
            except Exception as exc:          # keep the loop alive, but don't hide the problem
                if str(exc) != last_err:
                    last_err = str(exc)
                    self.post(self.log_msg, f"Status check failed: {exc}", True)
            self.stop_event.wait(POLL_SECONDS)

    def apply_status(self, snap):
        for sid, data in snap.items():
            ui, cfg = self.ui[sid], SERVICES[sid]
            ui.update(installed=data["installed"], running=data["running"],
                      failed=data["failed"], pid_list=data["pids"], boot_state=data["boot"])
            if not data["installed"]:
                shown = "not installed"
            elif data["failed"]:
                shown = "failed"
            elif data["running"]:
                shown = (",".join(map(str, data["pids"][:3])) + ("…" if len(data["pids"]) > 3 else "")) or "active"
            else:
                shown = ""
            ui["pids"].config(text=shown)
            ui["ports"].config(text=", ".join(cfg["ports"]) if data["running"] else "")
            ui["cpu"].config(text=f"{data['cpu']:.1f}% | {data['ram']:.0f} MB" if data["pids"] else "")
            self.update_buttons(sid)
        if not self._reported_missing:
            self._reported_missing = True
            missing = [SERVICES[s]["label"] for s, d in snap.items() if not d["installed"]]
            if missing:
                self.log_msg("Not installed: " + ", ".join(missing) +
                             ". Click 'Install' on a row to add it.")

    def update_buttons(self, sid):
        ui, busy = self.ui[sid], sid in self.busy
        installed, running = ui["installed"], ui["running"]
        if running:
            bg, fg = self.c_run, self.c_run_fg
        elif ui["failed"]:
            bg, fg = self.c_fail, self.c_fail_fg
        else:
            bg, fg = self.c_bg, (self.c_fg if installed else self.c_dim)
        ui["mod"].config(bg=bg, fg=fg)
        ui["start"].config(text="Install" if not installed else ("Stop" if running else "Start"),
                           state="disabled" if busy else "normal")
        ui["restart"].config(state="normal" if (installed and running and not busy) else "disabled")
        state = ui["boot_state"]
        if state is None or not installed:
            text, bfg = "–", self.c_dim
        else:
            text, bfg = ("✓", "#2e9e44") if state else ("✗", "#d13438")
        can_toggle = installed and state is not None and not busy
        ui["boot"].config(text=text, fg=bfg, bg=self.c_btn, activebackground=self.c_btn,
                          disabledforeground=bfg, state="normal" if can_toggle else "disabled")

    # ------------------------------------------------ privileged actions
    def run_helper_async(self, key, args, start_msg, ok_msg, timeout=120):
        """Run the root helper through pkexec on a worker thread."""
        if key in self.busy:
            return
        if not os.path.exists(HELPER):
            self.log_msg(f"Helper not found: {HELPER}. Install the lampp-panel package.", error=True)
            return
        if not shutil.which("pkexec"):
            self.log_msg("pkexec not found. Install polkit.", error=True)
            return
        self.busy.add(key)
        if key in self.ui:
            self.update_buttons(key)
        self.log_msg(start_msg)

        def worker():
            try:
                res = subprocess.run(["pkexec", HELPER, *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace",
                                     timeout=timeout)
                ok = res.returncode == 0
                if res.returncode in (126, 127):
                    detail = "Authorization was cancelled or denied."
                else:
                    detail = res.stdout.strip()
            except subprocess.TimeoutExpired:
                ok, detail = False, f"Timed out after {timeout} s."
            except OSError as exc:
                ok, detail = False, str(exc)
            self.post(self.action_done, key, ok, ok_msg, start_msg, detail)

        threading.Thread(target=worker, daemon=True).start()

    def action_done(self, key, ok, ok_msg, start_msg, detail):
        self.busy.discard(key)
        lines = detail.splitlines()
        for line in lines[-40:]:                   # dnf can be chatty
            self.log_msg(f"  {line}", error=not ok)
        self.log_msg(ok_msg if ok else f"Failed: {start_msg.rstrip('.')}", error=not ok)
        if key in self.ui:
            self.update_buttons(key)

    def find_port_conflicts(self, ports, ignore=()):
        wanted = {int(p) for p in ports}
        found = {}
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.Error, OSError):
            return found
        for c in conns:
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in wanted:
                if c.pid in ignore:          # the service's own processes are not a conflict
                    continue
                who = "another process (details need root)"
                if c.pid:
                    who = f"{proc_label(c.pid)} (PID {c.pid})"
                found.setdefault(c.laddr.port, set()).add(who)
        return found

    def toggle_service(self, sid):
        if sid in self.busy:
            return
        cfg, ui = SERVICES[sid], self.ui[sid]
        label = cfg["label"]
        if not ui["installed"]:
            pkgs = " ".join(cfg["pkgs"])
            if messagebox.askyesno("Install", f"{label} is not installed.\n\nInstall it now?\n\n"
                                   f"    sudo dnf install {pkgs}", parent=self):
                self.run_helper_async(sid, ["install", sid], f"Installing {pkgs} (this can take a few minutes)...",
                                      f"{label} installed.", timeout=1800)
            return
        if ui["running"]:
            self.run_helper_async(sid, ["stop", sid], f"Stopping {label}...", f"{label} stopped.")
            return
        args = ["start", sid]
        conflicts = self.find_port_conflicts(cfg["ports"], set(ui["pid_list"]))
        if conflicts:
            lines = [f"  port {port}: {', '.join(sorted(who))}" for port, who in sorted(conflicts.items())]
            self.log_msg(f"Ports needed by {label} are already in use:", error=True)
            for line in lines:
                self.log_msg(line, error=True)
            if not messagebox.askyesno(
                    "Port conflict",
                    f"{label} needs ports that are already in use:\n\n" + "\n".join(lines) +
                    f"\n\nAsk the process(es) to exit (SIGTERM, then SIGKILL after 5 s) and start {label}?",
                    parent=self):
                return
            args.append("--free-ports")
        self.run_helper_async(sid, args, f"Starting {label}...", f"{label} started.")

    def restart_service(self, sid):
        label = SERVICES[sid]["label"]
        self.run_helper_async(sid, ["restart", sid], f"Restarting {label}...", f"{label} restarted.")

    def toggle_boot(self, sid):
        state = self.ui[sid]["boot_state"]
        if state is None or sid in self.busy:
            return
        label, action = SERVICES[sid]["label"], "disable" if state else "enable"
        self.run_helper_async(sid, [action, sid], f"{action.capitalize()} {label} at boot...",
                              f"{label} {action}d at boot.")

    def start_stack(self):
        self.run_helper_async("_global", ["start-stack"], "Starting MariaDB, PHP-FPM and Apache...",
                              "LAMP stack started.")

    def stop_stack(self):
        self.run_helper_async("_global", ["stop-stack"], "Stopping Apache, PHP-FPM and MariaDB...",
                              "LAMP stack stopped.")

    def clear_logs(self):
        if messagebox.askyesno("Clear logs",
                               "Empty the Apache and MariaDB log files?\n"
                               "(The systemd journal is not touched.) This cannot be undone.", parent=self):
            self.run_helper_async("_global", ["clear-logs"], "Clearing log files...",
                                  "Log files cleared.")

    def grant_access(self):
        if messagebox.askyesno("Grant access",
                               f"{HTDOCS} is owned by root, so you cannot create projects there.\n\n"
                               "Give your user read/write access to it (using ACLs, no ownership "
                               "change)?", parent=self):
            self.run_helper_async("_global", ["grant-access"], f"Granting your user access to {HTDOCS}...",
                                  "Access granted.")

    # ------------------------------------------------- unprivileged tools
    def open_admin(self, sid):
        url = SERVICES[sid]["admin"]
        if url:
            self.spawn(["xdg-open", url])

    def open_launchpad(self):
        win = tk.Toplevel(self)
        win.title("Launch Pad")
        win.geometry("400x520")
        win.configure(bg=self.c_bg)
        tk.Label(win, text=f"Projects in {HTDOCS}", font=("Arial", 12, "bold"),
                 bg=self.c_bg, fg=self.c_fg).pack(pady=10)
        listbox = tk.Listbox(win, font=("Arial", 11), bg=self.c_table, fg=self.c_fg)
        listbox.pack(fill="both", expand=True, padx=10, pady=5)

        try:
            names = sorted(d for d in os.listdir(HTDOCS)
                           if not d.startswith(".") and os.path.isdir(os.path.join(HTDOCS, d)))
        except OSError as exc:
            names = []
            messagebox.showwarning("Launch Pad", f"Cannot read {HTDOCS}:\n{exc}\n\n"
                                   "Is Apache (httpd) installed?", parent=win)
        for name in names:
            listbox.insert(tk.END, name)
        if os.path.isdir(HTDOCS) and not os.access(HTDOCS, os.W_OK):
            tk.Label(win, text="This folder is read-only for you - use 'Grant Access'.",
                     bg=self.c_bg, fg=self.c_dim).pack()

        def selected():
            sel = listbox.curselection()
            return listbox.get(sel[0]) if sel else None

        def launch_code():
            name = selected()
            if not name:
                return
            path = os.path.join(HTDOCS, name)
            code = shutil.which("code")
            if code:
                self.spawn([code, path])
            else:
                self.log_msg("VS Code ('code') not found; opening the folder instead.")
                self.spawn(["xdg-open", path])

        def launch_web(_event=None):
            name = selected()
            if name:
                self.spawn(["xdg-open", f"http://localhost/{quote(name)}"])

        listbox.bind("<Double-Button-1>", launch_web)
        row = tk.Frame(win, bg=self.c_bg)
        row.pack(pady=10)
        tk.Button(row, text="Open in VS Code", command=launch_code).pack(side="left", padx=5)
        tk.Button(row, text="Open in Browser", command=launch_web).pack(side="left", padx=5)

    def backup_db(self):
        dump = shutil.which("mariadb-dump") or shutil.which("mysqldump")
        if not dump:
            messagebox.showerror("DB Backup", "mariadb-dump was not found.\n\n"
                                 "Install MariaDB first (sudo dnf install mariadb-server).", parent=self)
            return
        pw = simpledialog.askstring("DB Backup",
                                    "Password for database user 'root'\n(leave empty if none):",
                                    show="*", parent=self)
        if pw is None:
            return
        dest = Path.home() / "Documents" / f"lampp_db_backup_{datetime.now():%Y-%m-%d_%H%M%S}.sql"
        self.log_msg("Starting database backup (all databases)...")
        threading.Thread(target=self._backup_worker, args=(dump, dest, pw), daemon=True).start()

    def _backup_worker(self, dump, dest, pw):
        env = os.environ.copy()
        env.pop("MYSQL_PWD", None)
        if pw:
            env["MYSQL_PWD"] = pw            # not on the command line, so not visible in `ps`
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Runs as the current user. O_EXCL refuses existing files and symlinks; 0600 keeps
            # the dump (which contains password hashes) private from the very first byte.
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            self.post(self.log_msg, f"Backup failed: {exc}", True)
            return
        try:
            with os.fdopen(fd, "wb") as out:
                res = subprocess.run(
                    [dump, "-u", "root", "--all-databases", "--single-transaction",
                     "--routines", "--events"],
                    stdout=out, stderr=subprocess.PIPE, env=env, timeout=600)
            ok, err = res.returncode == 0, res.stderr.decode(errors="replace").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            ok, err = False, str(exc)
        if ok:
            self.post(self.log_msg, f"Backup saved to {dest}")
            self.post(messagebox.showinfo, "Backup complete",
                      f"All databases saved to:\n{dest}", parent=self)
        else:
            try:
                dest.unlink()                # never leave a partial dump behind
            except OSError:
                pass
            self.post(self.log_msg, f"Backup failed: {err or 'unknown error'}", True)

    # ------------------------------------------------- log / config viewer
    def open_logs(self, sid):
        cfg = SERVICES[sid]
        self.open_viewer(f"{cfg['label']} logs", cfg["logs"], tail=True)

    def open_config(self, sid):
        cfg = SERVICES[sid]
        self.open_viewer(f"{cfg['label']} configuration", cfg["configs"], tail=False,
                         note="Read-only. To edit: sudo -e <file>")

    def read_source(self, src, tail):
        """Blocking - always call from a worker thread."""
        kind, target, *rest = src
        if kind == "journal":
            try:
                r = subprocess.run(["journalctl", "-u", target, "-n", "500", "--no-pager"],
                                   capture_output=True, text=True, errors="replace", timeout=10)
                return r.stdout or r.stderr or "(no journal entries)"
            except (OSError, subprocess.SubprocessError) as exc:
                return f"(could not read the journal: {exc})"
        try:
            with open(target, "rb") as f:
                if tail:                     # only read the last 128 KB - logs can be huge
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 131072))
                    data = f.read()
                else:
                    data = f.read(512 * 1024)
            text = data.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return f"(file not found: {target} - is the service installed?)"
        except PermissionError:
            if kind == "rootfile" and rest:  # Fedora keeps e.g. /var/log/httpd root-only
                return self.read_via_helper(rest[0], tail)
            return f"(permission denied: {target})"
        except OSError as exc:
            return f"({exc})"
        return "\n".join(text.splitlines()[-500:]) if tail else text

    def read_via_helper(self, log_id, tail):
        if not os.path.exists(HELPER) or not shutil.which("pkexec"):
            return "(this file is root-only and the privileged helper is not available)"
        try:
            r = subprocess.run(["pkexec", HELPER, "read-log", log_id], capture_output=True,
                               text=True, errors="replace", timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"({exc})"
        if r.returncode in (126, 127):
            return "(authorization cancelled - this log is root-only)"
        if r.returncode != 0:
            return f"({r.stderr.strip() or 'could not read the log'})"
        return "\n".join(r.stdout.splitlines()[-500:]) if tail else r.stdout

    def open_viewer(self, title, sources, tail, note=""):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("900x600")
        win.configure(bg=self.c_bg)
        bar = tk.Frame(win, bg=self.c_bg)
        bar.pack(fill="x", padx=8, pady=6)
        choice = tk.StringVar(value=next(iter(sources)))
        combo = ttk.Combobox(bar, textvariable=choice, values=list(sources),
                             state="readonly", width=24)
        combo.pack(side="left")
        status = tk.Label(bar, text="", bg=self.c_bg, fg=self.c_dim)
        text = scrolledtext.ScrolledText(win, font=("Courier", 9), wrap="none",
                                         bg=self.c_console, fg=self.c_console_fg)

        def show(content):
            if not win.winfo_exists():       # window closed while loading
                return
            status.config(text="")
            text.configure(state="normal")
            text.delete("1.0", tk.END)
            text.insert("1.0", content)
            if tail:
                text.see(tk.END)
            text.configure(state="disabled")

        def refresh(_event=None):
            status.config(text="Loading...")
            src = sources[choice.get()]
            threading.Thread(target=lambda: self.post(show, self.read_source(src, tail)),
                             daemon=True).start()

        tk.Button(bar, text="Refresh", command=refresh).pack(side="left", padx=6)
        status.pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", refresh)
        if note:
            tk.Label(bar, text=note, bg=self.c_bg, fg=self.c_fg).pack(side="left", padx=10)
        text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        refresh()


def main():
    if os.geteuid() == 0:
        sys.exit("LAMPP Panel must not run as root. It asks for permission (pkexec) "
                 "only when an action needs it.")
    try:
        app = LamppPanel()
    except tk.TclError as exc:
        sys.exit(f"Cannot open a display: {exc}")
    app.mainloop()


if __name__ == "__main__":
    main()
