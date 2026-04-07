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
  • GitHub account display + quick-link
  • API usage tracking
  • Installation health checks with install buttons

Setup:
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  python app.py

Build .app:
  python setup.py py2app
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import rumps
from dotenv import load_dotenv

# ─── Bootstrap: resolve repo root + load env ──────────────────────────────────
APP_DIR  = Path(__file__).parent.resolve()
REPO_ROOT = APP_DIR.parent.parent.parent  # apps/forge/worker-app → repo root

# Load env: try dashboard .env first (has Supabase creds), then repo root .env
for env_path in [
    REPO_ROOT / "apps" / "dashboard" / ".env",
    REPO_ROOT / ".env",
    APP_DIR / ".env",
]:
    if env_path.exists():
        load_dotenv(env_path)
        break

# Config file for user preferences (Claude accounts, GitHub, etc.)
CONFIG_PATH = Path.home() / ".forge-worker" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

POLL_INTERVAL = 5   # seconds between Supabase polls
DASHBOARD_URL = "https://app.hawthornbloom.com"
FORGE_URL     = f"{DASHBOARD_URL}/forge"

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _machine_id() -> str:
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


def _supabase_client():
    """Return a Supabase client or None if not configured."""
    try:
        from supabase import create_client
        url = os.environ.get("PUBLIC_SUPABASE_URL") or os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if url and key:
            return create_client(url, key)
    except ImportError:
        pass
    return None


def _check_installation(name: str) -> tuple[bool, str]:
    """Check if a tool/package is installed. Returns (ok, version_or_error)."""
    checks = {
        "python3":  lambda: (True, subprocess.check_output(["python3", "--version"], text=True).strip()),
        "claude":   lambda: (True, subprocess.check_output(["claude", "--version"], text=True, stderr=subprocess.STDOUT).strip()),
        "gh":       lambda: (True, subprocess.check_output(["gh", "--version"], text=True).split("\n")[0].strip()),
        "git":      lambda: (True, subprocess.check_output(["git", "--version"], text=True).strip()),
        "psutil":   lambda: (__import__("psutil") and (True, f"psutil {__import__('psutil').__version__}")),
        "supabase": lambda: (__import__("supabase") and (True, f"supabase-py installed")),
    }
    if name not in checks:
        return False, "unknown"
    try:
        result = checks[name]()
        if isinstance(result, tuple):
            return result
        return True, "installed"
    except (subprocess.CalledProcessError, FileNotFoundError, ImportError, Exception) as e:
        return False, str(e)[:60]


def _install_command(name: str) -> str | None:
    """Return the shell command to install a missing tool."""
    commands = {
        "claude":   "npm install -g @anthropic-ai/claude-code",
        "gh":       "brew install gh",
        "psutil":   f"{sys.executable} -m pip install psutil",
        "supabase": f"{sys.executable} -m pip install supabase",
    }
    return commands.get(name)


# ─── Supabase poller ──────────────────────────────────────────────────────────

class ForgeState:
    """Thread-safe snapshot of the current forge state for this machine."""

    def __init__(self):
        self._lock = threading.Lock()
        self.machine_id   = _machine_id()
        self.connected    = False
        self.active_run   = None   # dict from forge_runs
        self.workers      = []     # list of forge_workers rows
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
    """Background thread that polls Supabase and updates ForgeState."""

    def __init__(self, state: ForgeState, stop_event: threading.Event):
        super().__init__(daemon=True, name="forge-poller")
        self.state      = state
        self.stop_event = stop_event
        self.db         = None

    def _connect(self):
        self.db = _supabase_client()
        return self.db is not None

    def _heartbeat(self):
        try:
            self.db.table("forge_machines").upsert({
                "machine_id":   self.state.machine_id,
                "display_name": self.state.machine_id,
                "last_seen":    "now()",
            }, on_conflict="machine_id").execute()
        except Exception:
            pass

    def _fetch(self):
        machine_id = self.state.machine_id

        # Find active run for this machine
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
            run_ids = list({r["run_id"] for r in team_rows})
            for run_id in run_ids:
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
                    # Fetch worker statuses
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
                self.db = None  # Force reconnect next iteration

            self.stop_event.wait(POLL_INTERVAL)


# ─── Menu Bar App ──────────────────────────────────────────────────────────────

class ForgeWorkerApp(rumps.App):

    def __init__(self):
        super().__init__(
            name="Forge Worker",
            title="⚒",
            quit_button=None,
        )
        self.machine_id = _machine_id()
        self.state      = ForgeState()
        self._stop      = threading.Event()
        self._poller    = ForgePoller(self.state, self._stop)
        self._poller.start()
        self._build_menu()
        # Update menu every 5 seconds
        rumps.Timer(self._refresh_menu, POLL_INTERVAL).start()

    # ── Menu construction ──────────────────────────────────────────────────────

    def _build_menu(self):
        self.menu.clear()
        snap = self.state.snapshot()

        # ── Status header ────────────────────────────────────────────────────
        if snap["error"]:
            self.title = "⚒ !"
            self.menu.add(rumps.MenuItem(f"⚠  {snap['error']}", callback=None))
        elif not snap["connected"]:
            self.title = "⚒ …"
            self.menu.add(rumps.MenuItem("Connecting to Supabase…", callback=None))
        elif snap["active_run"]:
            run = snap["active_run"]
            count = snap["worker_count"]
            self.title = f"⚒ {count}"
            name = run.get("project_name", "Unnamed Run")[:30]
            self.menu.add(rumps.MenuItem(
                f"🟢  {name}",
                callback=lambda _: webbrowser.open(f"{FORGE_URL}/{run['id']}")
            ))
            spend = snap["api_spend"]
            cap   = snap["budget_cap"]
            self.menu.add(rumps.MenuItem(f"💰  ${spend:.2f} / ${cap:.2f} budget", callback=None))
        else:
            self.title = "⚒"
            self.menu.add(rumps.MenuItem("⚪  Idle — watching for assignment", callback=None))

        self.menu.add(rumps.separator)

        # ── Machine ──────────────────────────────────────────────────────────
        self.menu.add(rumps.MenuItem(f"🖥  Machine: {self.machine_id}", callback=None))

        # ── Workers ──────────────────────────────────────────────────────────
        workers = snap["workers"]
        if workers:
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Workers", callback=None))
            for w in workers:
                status_icon = {"working": "🟢", "idle": "⚪", "stopped": "🔴"}.get(w.get("status", "idle"), "⚫")
                epic = w.get("current_epic") or "idle"
                label = f"  {status_icon}  {w.get('display_name', w.get('persona_key', '?'))} — {epic[:40]}"
                self.menu.add(rumps.MenuItem(label, callback=None))

        # ── Kill button ──────────────────────────────────────────────────────
        if snap["active_run"]:
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("🛑  Kill All Workers", callback=self.kill_workers))

        self.menu.add(rumps.separator)

        # ── Claude account ────────────────────────────────────────────────────
        cfg = _load_config()
        accounts = cfg.get("claude_accounts", [])
        active_account = cfg.get("active_account", "")
        if active_account:
            self.menu.add(rumps.MenuItem(f"🤖  Claude: {active_account}", callback=None))
        else:
            self.menu.add(rumps.MenuItem("🤖  Claude: not configured", callback=self._configure_claude))

        if accounts:
            account_menu = rumps.MenuItem("Switch Account")
            for acc in accounts:
                label = f"{'✓ ' if acc['name'] == active_account else '  '}{acc['name']}"
                account_menu[label] = rumps.MenuItem(
                    label,
                    callback=lambda _, a=acc: self._switch_account(a)
                )
            self.menu.add(account_menu)

        self.menu.add(rumps.MenuItem("+ Add Account…", callback=self._add_account))

        self.menu.add(rumps.separator)

        # ── GitHub ────────────────────────────────────────────────────────────
        github_user = cfg.get("github_username", "")
        if github_user:
            self.menu.add(rumps.MenuItem(
                f"🐙  GitHub: @{github_user}",
                callback=lambda _: webbrowser.open(f"https://github.com/{github_user}")
            ))
        else:
            self.menu.add(rumps.MenuItem("🐙  GitHub: not set", callback=self._configure_github))

        self.menu.add(rumps.separator)

        # ── Installations ─────────────────────────────────────────────────────
        install_menu = rumps.MenuItem("🔧 Installations")
        tools = ["python3", "claude", "gh", "git", "psutil", "supabase"]
        all_ok = True
        for tool in tools:
            ok, version = _check_installation(tool)
            if not ok:
                all_ok = False
            icon = "✅" if ok else "❌"
            short = version.split("\n")[0][:50] if ok else version
            label = f"  {icon}  {tool}: {short}"
            cmd = _install_command(tool)
            if not ok and cmd:
                install_menu[label] = rumps.MenuItem(
                    label,
                    callback=lambda _, t=tool, c=cmd: self._install_tool(t, c)
                )
            else:
                install_menu[label] = rumps.MenuItem(label, callback=None)

        self.menu.add(install_menu)

        # Warn icon if any missing
        if not all_ok:
            install_menu.title = "🔧 Installations ⚠"

        self.menu.add(rumps.separator)

        # ── Links ─────────────────────────────────────────────────────────────
        self.menu.add(rumps.MenuItem(
            "🌐  Forge Dashboard",
            callback=lambda _: webbrowser.open(FORGE_URL)
        ))
        self.menu.add(rumps.MenuItem(
            "📋  View Logs",
            callback=lambda _: subprocess.Popen(["open", str(Path.home() / "Library" / "Logs" / "forge-worker.log")])
        ))

        self.menu.add(rumps.separator)

        # ── About / Quit ──────────────────────────────────────────────────────
        self.menu.add(rumps.MenuItem("About Forge Worker", callback=self._show_about))
        self.menu.add(rumps.MenuItem("Quit", callback=self._quit))

    # ── Callbacks ──────────────────────────────────────────────────────────────

    @rumps.clicked("🛑  Kill All Workers")
    def kill_workers(self, _):
        if rumps.alert("Kill Workers", "Stop all running workers for this machine?", ok="Kill", cancel="Cancel"):
            snap = self.state.snapshot()
            if snap["active_run"]:
                try:
                    db = _supabase_client()
                    if db:
                        run_id = snap["active_run"]["id"]
                        db.table("forge_runs").update({"stop_requested": True}).eq("id", run_id).execute()
                        rumps.notification("Forge Worker", "Workers stopping", "Stop signal sent to all workers.")
                except Exception as e:
                    rumps.alert("Error", f"Could not send stop signal: {e}")

    def _switch_account(self, account: dict):
        cfg = _load_config()
        cfg["active_account"] = account["name"]
        api_key = account.get("api_key", "")
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        _save_config(cfg)
        rumps.notification("Forge Worker", "Account Switched", f"Now using: {account['name']}")
        self._refresh_menu(None)

    def _configure_claude(self, _):
        response = rumps.Window(
            message="Enter a name for this Claude account (e.g. 'HB Production'):",
            title="Add Claude Account",
            default_text="",
            ok="Next",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not response.clicked:
            return
        name = response.text.strip()
        if not name:
            return
        self._add_account_with_name(name)

    def _add_account(self, _):
        self._configure_claude(None)

    def _add_account_with_name(self, name: str):
        response = rumps.Window(
            message=f"Enter the Anthropic API key for '{name}':",
            title="Add Claude Account",
            default_text="sk-ant-...",
            ok="Save",
            cancel="Cancel",
            dimensions=(400, 24),
        ).run()
        if not response.clicked:
            return
        api_key = response.text.strip()
        if not api_key.startswith("sk-"):
            rumps.alert("Invalid Key", "API key should start with 'sk-ant-'")
            return
        cfg = _load_config()
        accounts = cfg.get("claude_accounts", [])
        accounts = [a for a in accounts if a["name"] != name]  # replace if exists
        accounts.append({"name": name, "api_key": api_key})
        cfg["claude_accounts"]  = accounts
        cfg["active_account"]   = name
        os.environ["ANTHROPIC_API_KEY"] = api_key
        _save_config(cfg)
        rumps.notification("Forge Worker", "Account Added", f"'{name}' saved and activated.")
        self._refresh_menu(None)

    def _configure_github(self, _):
        response = rumps.Window(
            message="Enter your GitHub username (for commit identity):",
            title="GitHub Account",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        ).run()
        if not response.clicked:
            return
        username = response.text.strip().lstrip("@")
        if not username:
            return
        cfg = _load_config()
        cfg["github_username"] = username
        _save_config(cfg)
        # Register in Supabase forge_machines
        try:
            db = _supabase_client()
            if db:
                db.table("forge_machines").upsert({
                    "machine_id":      self.machine_id,
                    "github_username": username,
                }, on_conflict="machine_id").execute()
        except Exception:
            pass
        rumps.notification("Forge Worker", "GitHub Set", f"Linked to @{username}")
        self._refresh_menu(None)

    def _install_tool(self, tool: str, command: str):
        if rumps.alert(
            f"Install {tool}",
            f"Run:\n{command}\n\nProceed?",
            ok="Install",
            cancel="Cancel",
        ):
            subprocess.Popen(
                ["osascript", "-e",
                 f'tell application "Terminal" to do script "{command}"']
            )

    def _show_about(self, _):
        rumps.alert(
            "Forge Worker",
            f"Machine: {self.machine_id}\n"
            f"Version: 1.0.0\n\n"
            f"The Hawthorn Bloom Forge Worker monitor.\n"
            f"Polls Supabase for epic assignments and runs\n"
            f"AI worker threads for the build pipeline.\n\n"
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
