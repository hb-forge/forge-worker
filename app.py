"""
Forge Worker — macOS Menu Bar App

The Hawthorn Bloom Forge Worker monitor and controller.
Runs as a menu bar item (no Dock icon, no terminal).

Features:
  • Monitors active forge sessions + worker threads
  • Polls Supabase for machine assignments (watcher logic built in)
  • Shows current run name + opens forge URL in browser
  • Kill all workers button
  • Switch between Claude API accounts
  • Multiple GitHub accounts with active-account switcher
  • Rename this machine (distinct display name for two Mac Minis)
  • Installation health checks with install buttons

Setup:
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  python app.py

Build .app:
  python setup.py py2app
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import rumps
from dotenv import load_dotenv

# ─── Bootstrap: resolve repo root + load env ──────────────────────────────────
APP_DIR   = Path(__file__).parent.resolve()
REPO_ROOT = APP_DIR.parent.parent.parent  # apps/forge/worker-app → repo root

# Load env: dashboard .env has Supabase creds
for env_path in [
    REPO_ROOT / "apps" / "dashboard" / ".env",
    REPO_ROOT / ".env",
    APP_DIR / ".env",
]:
    if env_path.exists():
        load_dotenv(env_path)
        break

# Config file for user preferences
CONFIG_PATH = Path.home() / ".forge-worker" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

POLL_INTERVAL = 5
DASHBOARD_URL = "https://app.hawthornbloom.com"
FORGE_URL     = f"{DASHBOARD_URL}/forge"

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _raw_machine_id() -> str:
    """Hostname-derived machine ID (immutable, used as PK in forge_machines)."""
    raw = socket.gethostname()
    for suffix in ('.local', '.lan', '.home'):
        if raw.lower().endswith(suffix):
            raw = raw[:-len(suffix)]
    return raw.lower().replace("'", "").replace(" ", "-")


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _display_name(cfg: Optional[dict] = None) -> str:
    """Human-friendly name for this machine (editable, stored in config)."""
    cfg = cfg or _load_config()
    return cfg.get("display_name") or _raw_machine_id()


def _supabase_client():
    try:
        from supabase import create_client
        url = os.environ.get("PUBLIC_SUPABASE_URL") or os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if url and key:
            return create_client(url, key)
    except ImportError:
        pass
    return None


def _check_installation(name: str) -> tuple:
    checks = {
        "python3":  lambda: (True, subprocess.check_output(["python3", "--version"], text=True).strip()),
        "claude":   lambda: (True, subprocess.check_output(["claude", "--version"], text=True, stderr=subprocess.STDOUT).strip()),
        "gh":       lambda: (True, subprocess.check_output(["gh", "--version"], text=True).split("\n")[0].strip()),
        "git":      lambda: (True, subprocess.check_output(["git", "--version"], text=True).strip()),
        "psutil":   lambda: (__import__("psutil") and (True, f"psutil {__import__('psutil').__version__}")),
        "supabase": lambda: (__import__("supabase") and (True, "supabase-py installed")),
    }
    if name not in checks:
        return False, "unknown"
    try:
        result = checks[name]()
        if isinstance(result, tuple):
            return result
        return True, "installed"
    except Exception as e:
        return False, str(e)[:60]


def _install_command(name: str) -> Optional[str]:
    commands = {
        "claude":   "npm install -g @anthropic-ai/claude-code",
        "gh":       "brew install gh",
        "psutil":   f"{sys.executable} -m pip install psutil",
        "supabase": f"{sys.executable} -m pip install supabase",
    }
    return commands.get(name)


# ─── Supabase poller ──────────────────────────────────────────────────────────

class ForgeState:
    def __init__(self, machine_id: str):
        self._lock        = threading.Lock()
        self.machine_id   = machine_id
        self.connected    = False
        self.active_run   = None
        self.workers      = []
        self.worker_count = 0
        self.api_spend    = 0.0
        self.budget_cap   = 20.0
        self.error        = None

    def update(self, connected, active_run, workers):
        with self._lock:
            self.connected    = connected
            self.active_run   = active_run
            self.workers      = workers or []
            self.worker_count = len([w for w in self.workers if w.get("status") == "working"])
            if active_run:
                self.api_spend  = float(active_run.get("api_spend_usd", 0))
                self.budget_cap = float(active_run.get("budget_cap_usd", 20))
            self.error = None

    def set_error(self, msg):
        with self._lock:
            self.connected = False
            self.error     = msg

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "machine_id":   self.machine_id,
                "connected":    self.connected,
                "active_run":   self.active_run,
                "workers":      list(self.workers),
                "worker_count": self.worker_count,
                "api_spend":    self.api_spend,
                "budget_cap":   self.budget_cap,
                "error":        self.error,
            }


class ForgePoller(threading.Thread):
    def __init__(self, state: ForgeState, stop_event: threading.Event):
        super().__init__(daemon=True, name="forge-poller")
        self.state      = state
        self.stop_event = stop_event
        self.db         = None

    def _connect(self):
        self.db = _supabase_client()
        return self.db is not None

    def _heartbeat(self):
        cfg = _load_config()
        try:
            self.db.table("forge_machines").upsert({
                "machine_id":      self.state.machine_id,
                "display_name":    _display_name(cfg),
                "github_username": cfg.get("github_username", ""),
                "last_seen":       "now()",
            }, on_conflict="machine_id").execute()
        except Exception:
            pass

    def _fetch(self):
        machine_id = self.state.machine_id
        team_rows = (
            self.db.table("forge_run_team")
            .select("run_id, persona, display_name")
            .eq("machine_id", machine_id)
            .execute()
            .data or []
        )
        active_run = None
        workers    = []
        if team_rows:
            for run_id in {r["run_id"] for r in team_rows}:
                run = (
                    self.db.table("forge_runs")
                    .select("id, project_name, status, build_gate_status, stop_requested, api_spend_usd, budget_cap_usd")
                    .eq("id", run_id)
                    .single()
                    .execute()
                    .data
                )
                if run and run.get("build_gate_status") in ("running", "pending") and not run.get("stop_requested"):
                    active_run = run
                    workers = (
                        self.db.table("forge_workers")
                        .select("worker_key, persona_key, display_name, status, current_epic, last_heartbeat_at")
                        .eq("run_id", run_id)
                        .execute()
                        .data or []
                    )
                    break
        self.state.update(connected=True, active_run=active_run, workers=workers)

    def run(self):
        while not self.stop_event.is_set():
            try:
                if self.db is None:
                    if not self._connect():
                        self.state.set_error("Supabase not configured — check .env")
                        self.stop_event.wait(POLL_INTERVAL)
                        continue
                self._heartbeat()
                self._fetch()
            except Exception as e:
                self.state.set_error(str(e)[:80])
                self.db = None
            self.stop_event.wait(POLL_INTERVAL)


# ─── Menu Bar App ──────────────────────────────────────────────────────────────

class ForgeWorkerApp(rumps.App):

    def __init__(self):
        super().__init__(name="Forge Worker", title="⚒", quit_button=None)
        self.machine_id = _raw_machine_id()
        self.state      = ForgeState(self.machine_id)
        self._stop      = threading.Event()
        self._poller    = ForgePoller(self.state, self._stop)
        self._poller.start()
        self._build_menu()
        rumps.Timer(self._refresh_menu, POLL_INTERVAL).start()

    # ── Menu construction ──────────────────────────────────────────────────────

    def _build_menu(self):
        self.menu.clear()
        snap = self.state.snapshot()
        cfg  = _load_config()

        # ── Status ───────────────────────────────────────────────────────────
        if snap["error"]:
            self.title = "⚒ !"
            self.menu.add(rumps.MenuItem(f"⚠  {snap['error']}", callback=None))
        elif not snap["connected"]:
            self.title = "⚒ …"
            self.menu.add(rumps.MenuItem("Connecting to Supabase…", callback=None))
        elif snap["active_run"]:
            run   = snap["active_run"]
            count = snap["worker_count"]
            self.title = f"⚒ {count}"
            name = run.get("project_name", "Unnamed Run")[:30]
            self.menu.add(rumps.MenuItem(
                f"🟢  {name}",
                callback=lambda _: webbrowser.open(f"{FORGE_URL}/{run['id']}")
            ))
            self.menu.add(rumps.MenuItem(
                f"💰  ${snap['api_spend']:.2f} / ${snap['budget_cap']:.2f} budget",
                callback=None
            ))
        else:
            self.title = "⚒"
            self.menu.add(rumps.MenuItem("⚪  Idle — watching for assignment", callback=None))

        self.menu.add(rumps.separator)

        # ── Machine ──────────────────────────────────────────────────────────
        display = _display_name(cfg)
        self.menu.add(rumps.MenuItem(
            f"🖥  {display}",
            callback=self._rename_machine
        ))

        # ── Workers ──────────────────────────────────────────────────────────
        workers = snap["workers"]
        if workers:
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Workers", callback=None))
            for w in workers:
                icon  = {"working": "🟢", "idle": "⚪", "stopped": "🔴"}.get(w.get("status", "idle"), "⚫")
                epic  = w.get("current_epic") or "idle"
                label = f"  {icon}  {w.get('display_name', w.get('persona_key', '?'))} — {epic[:40]}"
                self.menu.add(rumps.MenuItem(label, callback=None))

        # ── Kill button ──────────────────────────────────────────────────────
        if snap["active_run"]:
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("🛑  Kill All Workers", callback=self.kill_workers))

        self.menu.add(rumps.separator)

        # ── Claude accounts ───────────────────────────────────────────────────
        accounts       = cfg.get("claude_accounts", [])
        active_account = cfg.get("active_account", "")

        if active_account:
            self.menu.add(rumps.MenuItem(f"🤖  Claude: {active_account}", callback=None))
        else:
            self.menu.add(rumps.MenuItem("🤖  Claude: not configured", callback=self._add_account))

        if len(accounts) > 1:
            switch_menu = rumps.MenuItem("Switch Account")
            for acc in accounts:
                label = f"{'✓ ' if acc['name'] == active_account else '   '}{acc['name']}"
                switch_menu[label] = rumps.MenuItem(
                    label, callback=lambda _, a=acc: self._switch_account(a)
                )
            self.menu.add(switch_menu)

        self.menu.add(rumps.MenuItem("+ Add Account…", callback=self._add_account))

        self.menu.add(rumps.separator)

        # ── GitHub accounts ───────────────────────────────────────────────────
        gh_accounts    = cfg.get("github_accounts", [])
        active_gh      = cfg.get("github_username", "")

        if active_gh:
            self.menu.add(rumps.MenuItem(
                f"🐙  GitHub: @{active_gh}",
                callback=lambda _: webbrowser.open(f"https://github.com/{active_gh}")
            ))
        else:
            self.menu.add(rumps.MenuItem("🐙  GitHub: not set", callback=self._add_github))

        if len(gh_accounts) > 1:
            gh_menu = rumps.MenuItem("Switch GitHub")
            for gh in gh_accounts:
                label = f"{'✓ ' if gh == active_gh else '   '}@{gh}"
                gh_menu[label] = rumps.MenuItem(
                    label, callback=lambda _, u=gh: self._switch_github(u)
                )
            self.menu.add(gh_menu)

        self.menu.add(rumps.MenuItem("+ Add GitHub Account…", callback=self._add_github))

        self.menu.add(rumps.separator)

        # ── Installations ─────────────────────────────────────────────────────
        install_menu = rumps.MenuItem("🔧 Installations")
        all_ok = True
        for tool in ["python3", "claude", "gh", "git", "psutil", "supabase"]:
            ok, version = _check_installation(tool)
            if not ok:
                all_ok = False
            icon  = "✅" if ok else "❌"
            short = version.split("\n")[0][:50] if ok else version
            label = f"  {icon}  {tool}: {short}"
            cmd   = _install_command(tool)
            install_menu[label] = rumps.MenuItem(
                label,
                callback=(lambda _, t=tool, c=cmd: self._install_tool(t, c)) if not ok and cmd else None
            )
        if not all_ok:
            install_menu.title = "🔧 Installations ⚠"
        self.menu.add(install_menu)

        self.menu.add(rumps.separator)

        # ── Links ─────────────────────────────────────────────────────────────
        self.menu.add(rumps.MenuItem(
            "🌐  Forge Dashboard",
            callback=lambda _: webbrowser.open(FORGE_URL)
        ))
        self.menu.add(rumps.MenuItem(
            "📋  View Logs",
            callback=lambda _: subprocess.Popen(["open", str(CONFIG_PATH.parent / "app.log")])
        ))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("About Forge Worker", callback=self._show_about))
        self.menu.add(rumps.MenuItem("Quit", callback=self._quit))

    # ── Callbacks ─────────────────────────────────────────────────────────────

    @rumps.clicked("🛑  Kill All Workers")
    def kill_workers(self, _):
        if rumps.alert("Kill Workers", "Stop all running workers for this machine?", ok="Kill", cancel="Cancel"):
            snap = self.state.snapshot()
            if snap["active_run"]:
                try:
                    db = _supabase_client()
                    if db:
                        db.table("forge_runs").update({"stop_requested": True}).eq("id", snap["active_run"]["id"]).execute()
                        rumps.notification("Forge Worker", "Workers stopping", "Stop signal sent.")
                except Exception as e:
                    rumps.alert("Error", f"Could not send stop signal: {e}")

    def _rename_machine(self, _):
        cfg     = _load_config()
        current = _display_name(cfg)
        r = rumps.Window(
            message="Enter a display name for this machine\n(shown in the Assembly screen):",
            title="Rename Machine",
            default_text=current,
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not r.clicked or not r.text.strip():
            return
        cfg["display_name"] = r.text.strip()
        _save_config(cfg)
        # Update Supabase immediately
        try:
            db = _supabase_client()
            if db:
                db.table("forge_machines").upsert({
                    "machine_id":   self.machine_id,
                    "display_name": r.text.strip(),
                }, on_conflict="machine_id").execute()
        except Exception:
            pass
        rumps.notification("Forge Worker", "Machine renamed", f"Now showing as: {r.text.strip()}")
        self._refresh_menu(None)

    def _switch_account(self, account: dict):
        cfg = _load_config()
        cfg["active_account"] = account["name"]
        if account.get("api_key"):
            os.environ["ANTHROPIC_API_KEY"] = account["api_key"]
        _save_config(cfg)
        rumps.notification("Forge Worker", "Account Switched", f"Now using: {account['name']}")
        self._refresh_menu(None)

    def _add_account(self, _):
        r = rumps.Window(
            message="Account name (e.g. 'HB Production'):",
            title="Add Claude Account",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not r.clicked or not r.text.strip():
            return
        name = r.text.strip()

        r2 = rumps.Window(
            message=f"Anthropic API key for '{name}':",
            title="Add Claude Account",
            default_text="sk-ant-...",
            ok="Save",
            cancel="Cancel",
            dimensions=(400, 24),
        ).run()
        if not r2.clicked:
            return
        api_key = r2.text.strip()
        if not api_key.startswith("sk-"):
            rumps.alert("Invalid Key", "API key should start with 'sk-ant-'")
            return

        cfg      = _load_config()
        accounts = [a for a in cfg.get("claude_accounts", []) if a["name"] != name]
        accounts.append({"name": name, "api_key": api_key})
        cfg["claude_accounts"] = accounts
        cfg["active_account"]  = name
        os.environ["ANTHROPIC_API_KEY"] = api_key
        _save_config(cfg)
        rumps.notification("Forge Worker", "Account Added", f"'{name}' saved and activated.")
        self._refresh_menu(None)

    def _switch_github(self, username: str):
        cfg = _load_config()
        cfg["github_username"] = username
        _save_config(cfg)
        try:
            db = _supabase_client()
            if db:
                db.table("forge_machines").upsert({
                    "machine_id":      self.machine_id,
                    "github_username": username,
                }, on_conflict="machine_id").execute()
        except Exception:
            pass
        rumps.notification("Forge Worker", "GitHub Switched", f"Now using: @{username}")
        self._refresh_menu(None)

    def _add_github(self, _):
        r = rumps.Window(
            message="GitHub username:",
            title="Add GitHub Account",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not r.clicked or not r.text.strip():
            return
        username = r.text.strip().lstrip("@")
        cfg      = _load_config()
        accounts = cfg.get("github_accounts", [])
        if username not in accounts:
            accounts.append(username)
        cfg["github_accounts"]  = accounts
        cfg["github_username"]  = username
        _save_config(cfg)
        try:
            db = _supabase_client()
            if db:
                db.table("forge_machines").upsert({
                    "machine_id":      self.machine_id,
                    "github_username": username,
                }, on_conflict="machine_id").execute()
        except Exception:
            pass
        rumps.notification("Forge Worker", "GitHub Added", f"@{username} saved.")
        self._refresh_menu(None)

    def _install_tool(self, tool: str, command: str):
        if rumps.alert(f"Install {tool}", f"Run:\n{command}\n\nProceed?", ok="Install", cancel="Cancel"):
            subprocess.Popen(
                ["osascript", "-e", f'tell application "Terminal" to do script "{command}"']
            )

    def _show_about(self, _):
        cfg = _load_config()
        rumps.alert(
            "Forge Worker",
            f"Machine ID:   {self.machine_id}\n"
            f"Display name: {_display_name(cfg)}\n"
            f"Version:      1.0.0\n\n"
            f"Config: {CONFIG_PATH}"
        )

    def _quit(self, _):
        self._stop.set()
        rumps.quit_application()

    def _refresh_menu(self, _):
        try:
            self._build_menu()
        except Exception as e:
            log.error(f"Menu refresh error: {e}")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ForgeWorkerApp()
    app.run()
