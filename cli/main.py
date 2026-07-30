"""CLI interface for GitPilot – fully interactive TUI with directory picker, settings, and cross-platform service installation."""

import asyncio
import json
import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
import httpx
import readchar
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text

from gitpilot.domain.policies import get_token_path, get_current_os_user, generate_api_token
from gitpilot.domain.settings import SettingsManager, get_gitpilot_dir

logger = logging.getLogger("gitpilot.cli")
console = Console()


# ============================================================================
# Daemon communication helpers
# ============================================================================
def _get_daemon_port() -> Optional[int]:
    port_file = get_token_path().with_name("daemon_port")
    if not port_file.exists():
        return None
    try:
        return int(port_file.read_text().strip())
    except (ValueError, OSError):
        return None

def _get_api_token() -> Optional[str]:
    token_file = get_token_path()
    if not token_file.exists():
        return None
    return token_file.read_text().strip()

def _get_client() -> Optional[httpx.Client]:
    port = _get_daemon_port()
    if port is None:
        console.print("[red]Daemon not running. Start with `gitpilotd` or `gitpilot install-service`.[/red]")
        return None
    token = _get_api_token()
    if token is None:
        console.print("[red]Token not found. Run `gitpilot setup`.[/red]")
        return None
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


# ============================================================================
# Non-blocking key input reader
# ============================================================================
class NonBlockingKeyReader:
    """Reads keys from stdin in a background thread and puts them into a queue."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._read_keys, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)

    def get_key(self, timeout: float = 0.0) -> Optional[str]:
        try:
            return self._queue.get(block=False)
        except queue.Empty:
            return None

    def _read_keys(self) -> None:
        while self._running:
            try:
                key = readchar.readkey()
                self._queue.put(key)
            except (KeyboardInterrupt, OSError):
                break


# ============================================================================
# Interactive directory picker (arrow-key navigation)
# ============================================================================
class DirectoryPicker:
    """Interactive directory navigator using readchar for arrow keys."""

    def __init__(self):
        self.current_path = Path.home().resolve()
        self.selected_index = 0
        self.running = True

    def pick(self) -> Optional[Path]:
        self.running = True
        self.current_path = Path.home().resolve()
        self.selected_index = 0

        console.clear()
        with Live(console=console, screen=False, auto_refresh=False) as live:
            while self.running:
                live.update(self._render(), refresh=True)
                key = readchar.readkey()
                self._handle_key(key, live)

        console.clear()
        return self.current_path if self.running else None

    def _handle_key(self, key: str, live: Live):
        dirs = self._get_subdirs()

        if key == readchar.key.UP:
            if self.selected_index > 0:
                self.selected_index -= 1
        elif key == readchar.key.DOWN:
            if self.selected_index < len(dirs) - 1:
                self.selected_index += 1
        elif key == readchar.key.ENTER:
            if self.selected_index < len(dirs):
                selected_name = dirs[self.selected_index]
                if selected_name == "..":
                    self.current_path = self.current_path.parent
                    self.selected_index = 0
                else:
                    new_path = self.current_path / selected_name
                    if new_path.is_dir():
                        self.current_path = new_path
                        self.selected_index = 0
        elif key == readchar.key.BACKSPACE:
            if self.current_path.parent != self.current_path:
                self.current_path = self.current_path.parent
                self.selected_index = 0
        elif key == 's':
            self.running = False
        elif key == 'q' or key == readchar.key.CTRL_C:
            self.running = False
            self.current_path = None

    def _get_subdirs(self) -> List[str]:
        try:
            entries = sorted(
                [e.name for e in self.current_path.iterdir() if e.is_dir()],
                key=str.lower,
            )
        except PermissionError:
            entries = []
        if self.current_path.parent != self.current_path:
            entries.insert(0, "..")
        return entries

    def _render(self) -> Panel:
        dirs = self._get_subdirs()
        lines = [Text(f"📁 {self.current_path}/", style="bold cyan")]
        for idx, name in enumerate(dirs):
            prefix = "  " if idx != self.selected_index else "> "
            style = "white" if idx != self.selected_index else "reverse"
            lines.append(Text(f"{prefix}{name}/", style=style))
        lines.append(Text(""))
        lines.append(Text("↑↓ navigate   ↵ enter   ← backspace up   s select current   q quit", style="dim"))
        return Panel(
            Text("\n").join(lines),
            title="Select Project Directory",
            border_style="green",
        )


# ============================================================================
# Main TUI after setup
# ============================================================================
class MainMenu:
    """Interactive main menu with options to add project, monitor, and settings."""

    def __init__(self):
        self.client = _get_client()
        if self.client is None:
            sys.exit(1)
        self.settings_mgr = SettingsManager()
        self.options = [
            "Add / Select Project",
            "Monitor (live view)",
            "Settings (API keys, webhooks, etc.)",
            "Exit",
        ]
        self.selected = 0
        self.running = True

    def run(self):
        console.clear()
        with Live(console=console, screen=False, auto_refresh=False) as live:
            while self.running:
                live.update(self._render_menu(), refresh=True)
                key = readchar.readkey()
                self._handle_menu_key(key, live)

    def _handle_menu_key(self, key: str, live: Live):
        if key == readchar.key.UP:
            self.selected = (self.selected - 1) % len(self.options)
        elif key == readchar.key.DOWN:
            self.selected = (self.selected + 1) % len(self.options)
        elif key == readchar.key.ENTER:
            live.stop()
            self._execute_option(self.selected)
            if self.running:
                live.start()
        elif key in ('0', '1', '2', '3'):
            opt = int(key)
            if opt < len(self.options):
                live.stop()
                self._execute_option(opt)
                if self.running:
                    live.start()
        elif key == 'q' or key == readchar.key.CTRL_C:
            self.running = False

    def _render_menu(self) -> Panel:
        lines = []
        for idx, option in enumerate(self.options):
            prefix = "  " if idx != self.selected else "> "
            style = "white" if idx != self.selected else "bold yellow on blue"
            lines.append(Text(f"{prefix}[{idx}] {option}", style=style))
        lines.append(Text("\nUse arrow keys or number to select, q to quit.", style="dim"))
        return Panel(
            Text("\n").join(lines),
            title="GitPilot Main Menu",
            border_style="magenta",
        )

    def _execute_option(self, idx: int):
        if idx == 0:
            self._add_project()
        elif idx == 1:
            self._monitor()
        elif idx == 2:
            self._settings()
        elif idx == 3:
            self.running = False

    def _add_project(self):
        picker = DirectoryPicker()
        path = picker.pick()
        if path is None:
            return
        console.clear()
        console.print(f"Selected: [bold]{path}[/bold]")
        name = Prompt.ask("Project name (default: directory name)", default=path.name)
        create_repo = Confirm.ask("Create GitHub private repo?", default=False)
        github_repo = None
        if create_repo:
            github_repo = Prompt.ask("Repository name")
        payload = {"name": name, "path": str(path)}
        if github_repo:
            payload["github_repo_name"] = github_repo
        resp = self.client.post("/api/v1/projects", json=payload)
        if resp.status_code == 201:
            console.print(f"[green]Project '{name}' added (ID: {resp.json()['id']})[/green]")
        else:
            console.print(f"[red]Error: {resp.status_code} {resp.text}[/red]")
        console.print("\nPress any key to continue...", end="")
        readchar.readkey()

    def _monitor(self):
        console.clear()
        try:
            resp = self.client.get("/api/v1/projects")
            projects = resp.json()["items"] if resp.status_code == 200 else []
        except Exception:
            projects = []

        event_queue = queue.Queue()
        running_flag = threading.Event()
        running_flag.set()

        async def sse_stream():
            port = _get_daemon_port()
            token = _get_api_token()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=None,
            ) as client:
                try:
                    async with client.stream("GET", "/api/v1/events") as resp:
                        async for line in resp.aiter_lines():
                            if not running_flag.is_set():
                                break
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                try:
                                    data = json.loads(data_str)
                                    event_queue.put(("event", data))
                                except json.JSONDecodeError:
                                    pass
                except Exception as e:
                    event_queue.put(("error", str(e)))

        def run_async_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(sse_stream())

        sse_thread = threading.Thread(target=run_async_loop, daemon=True)
        sse_thread.start()

        key_reader = NonBlockingKeyReader()
        key_reader.start()

        events_log = []

        def render_monitor() -> Group:
            table = Table(title="Watched Projects", expand=True)
            table.add_column("ID", style="cyan")
            table.add_column("Name")
            table.add_column("Path")
            table.add_column("Status")
            for p in projects:
                status = "active" if p.get("deleted_at") is None else "inactive"
                table.add_row(str(p["id"]), p["name"], p["path"], status)

            event_lines = []
            for e in events_log[-15:]:
                if isinstance(e, dict):
                    if "commit_hash" in e:
                        event_lines.append(
                            f"[{e.get('timestamp','')[:19]}] Commit: {e['commit_hash'][:8]} - {e['message']} "
                            f"({e.get('branch','?')})"
                        )
                    elif "error" in e:
                        event_lines.append(f"[{e.get('timestamp','')[:19]}] Error: {e['error']}")
                    elif "status" in e:
                        event_lines.append(f"[{e.get('timestamp','')[:19]}] Status: {e['status']}")
                elif isinstance(e, str):
                    event_lines.append(f"Error: {e}")
            events_panel = Panel(
                "\n".join(event_lines) if event_lines else "Waiting for events...",
                title="Live Events (last 15)",
                border_style="yellow",
            )
            return Group(table, events_panel)

        with Live(console=console, refresh_per_second=4, screen=False) as live:
            try:
                while running_flag.is_set():
                    while not event_queue.empty():
                        try:
                            kind, payload = event_queue.get_nowait()
                            if kind == "event":
                                events_log.append(payload)
                            elif kind == "error":
                                events_log.append({"error": payload, "timestamp": ""})
                        except queue.Empty:
                            break

                    live.update(render_monitor())

                    key = key_reader.get_key()
                    if key is not None and key in ('q', readchar.key.CTRL_C, readchar.key.ESC):
                        running_flag.clear()
                        break

                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
            finally:
                running_flag.clear()
                key_reader.stop()
                sse_thread.join(timeout=2)

        console.clear()
        console.print("[green]Monitor stopped.[/green]")
        console.print("Press any key to return to menu...", end="")
        readchar.readkey()

    def _settings(self):
        sub_menu = {
            "1": "Change AI Provider / API Key",
            "2": "Change GitHub Token",
            "3": "Configure Discord Webhook for a project",
            "4": "Toggle branch-aware messages / smart grouping",
            "5": "Back to main menu",
        }
        while True:
            console.clear()
            for key, desc in sub_menu.items():
                console.print(f"[{key}] {desc}")
            choice = Prompt.ask("Select option", default="5")
            if choice == "1":
                self._change_ai_provider()
            elif choice == "2":
                self._change_github_token()
            elif choice == "3":
                self._configure_discord_webhook()
            elif choice == "4":
                self._toggle_features()
            elif choice == "5":
                break
            else:
                console.print("[red]Invalid option[/red]")
                time.sleep(1)
        console.clear()

    def _change_ai_provider(self):
        config = self.settings_mgr.load()
        current = config.get("ai_provider", "grok")
        console.print(f"Current provider: [bold]{current}[/bold]")
        provider = Prompt.ask("New provider", choices=["grok", "openai", "anthropic", "ollama"], default=current)
        self.settings_mgr.set("ai_provider", provider)
        if provider == "grok":
            key = Prompt.ask("Grok API Key", password=True, default=config.get("grok_api_key", ""))
            self.settings_mgr.set("grok_api_key", key)
            model = Prompt.ask("Model", default=config.get("ai_model", "grok-2"))
            self.settings_mgr.set("ai_model", model)
        elif provider == "openai":
            key = Prompt.ask("OpenAI API Key", password=True, default=config.get("openai_api_key", ""))
            self.settings_mgr.set("openai_api_key", key)
            model = Prompt.ask("Model", default=config.get("ai_model", "gpt-4o"))
            self.settings_mgr.set("ai_model", model)
        elif provider == "anthropic":
            key = Prompt.ask("Anthropic API Key", password=True, default=config.get("anthropic_api_key", ""))
            self.settings_mgr.set("anthropic_api_key", key)
            model = Prompt.ask("Model", default=config.get("ai_model", "claude-3-5-sonnet-20241022"))
            self.settings_mgr.set("ai_model", model)
        elif provider == "ollama":
            url = Prompt.ask("Ollama base URL", default=config.get("ollama_base_url", "http://localhost:11434"))
            self.settings_mgr.set("ollama_base_url", url)
            model = Prompt.ask("Ollama model", default=config.get("ollama_model", "llama3"))
            self.settings_mgr.set("ollama_model", model)
        console.print("[green]AI provider updated.[/green]")
        time.sleep(1)

    def _change_github_token(self):
        config = self.settings_mgr.load()
        token = Prompt.ask("GitHub Personal Access Token (repo scope)", password=True, default=config.get("github_token", ""))
        self.settings_mgr.set("github_token", token)
        console.print("[green]GitHub token updated.[/green]")
        time.sleep(1)

    def _configure_discord_webhook(self):
        resp = self.client.get("/api/v1/projects")
        if resp.status_code != 200:
            console.print("[red]Could not fetch projects.[/red]")
            return
        projects = resp.json()["items"]
        if not projects:
            console.print("[yellow]No projects registered. Add a project first.[/yellow]")
            return
        table = Table(title="Select Project for Webhook")
        table.add_column("ID")
        table.add_column("Name")
        for p in projects:
            table.add_row(str(p["id"]), p["name"])
        console.print(table)
        pid = Prompt.ask("Project ID")
        webhook_url = Prompt.ask("Discord Webhook URL")
        resp = self.client.post(
            "/api/v1/discord-webhooks",
            json={"project_id": int(pid), "url": webhook_url},
        )
        if resp.status_code == 201:
            console.print("[green]Discord webhook added.[/green]")
        else:
            console.print(f"[red]Error: {resp.text}[/red]")
        time.sleep(1)

    def _toggle_features(self):
        config = self.settings_mgr.load()
        smart = Confirm.ask("Smart grouping?", default=config.get("smart_grouping", True))
        self.settings_mgr.set("smart_grouping", smart)
        branch = Confirm.ask("Branch-aware messages?", default=config.get("branch_aware_messages", True))
        self.settings_mgr.set("branch_aware_messages", branch)
        debounce = Prompt.ask("Debounce interval (seconds)", default=str(config.get("debounce_interval", 3)))
        self.settings_mgr.set("debounce_interval", int(debounce))
        console.print("[green]Settings updated.[/green]")
        time.sleep(1)


# ============================================================================
# Cross-platform service installation
# ============================================================================
def _install_linux_service() -> None:
    """Install systemd user unit."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / "gitpilotd.service"

    exec_path = shutil.which("gitpilotd")
    if not exec_path:
        exec_path = f"{sys.executable} -m gitpilot.daemon.server"

    service_content = f"""[Unit]
Description=GitPilot Auto-Committer Daemon
After=network.target

[Service]
ExecStart={exec_path}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    service_path.write_text(service_content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "gitpilotd"], check=True)
    subprocess.run(["systemctl", "--user", "start", "gitpilotd"], check=True)
    console.print("[green]Systemd user service installed and started.[/green]")


def _install_macos_service() -> None:
    """Install launchd user agent."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.gitpilot.daemon.plist"

    exec_path = shutil.which("gitpilotd")
    if not exec_path:
        exec_path = f"{sys.executable} -m gitpilot.daemon.server"

    parts = shlex.split(exec_path)
    program_args_xml = "".join(f"<string>{arg}</string>" for arg in parts)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gitpilot.daemon</string>
    <key>ProgramArguments</key>
    <array>
        {program_args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home() / '.gitpilot' / 'logs' / 'launchd_stdout.log'}</string>
    <key>StandardErrorPath</key>
    <string>{Path.home() / '.gitpilot' / 'logs' / 'launchd_stderr.log'}</string>
</dict>
</plist>"""
    plist_path.write_text(plist_content)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    console.print("[green]Launchd agent installed and started.[/green]")


def _install_windows_service() -> None:
    """Install Windows service using `sc` (requires administrator privileges)."""
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        console.print("[red]Administrator privileges required. Run as Administrator.[/red]")
        raise SystemExit(1)

    service_name = "GitPilotDaemon"
    exec_path = shutil.which("gitpilotd")
    if not exec_path:
        exec_path = f'"{sys.executable}" -m gitpilot.daemon.server'
    else:
        exec_path = f'"{exec_path}"'

    # Stop and delete if already exists
    subprocess.run(["sc", "stop", service_name], capture_output=True)
    subprocess.run(["sc", "delete", service_name], capture_output=True)

    create_cmd = [
        "sc", "create", service_name,
        "binPath=", exec_path,
        "start=", "auto",
        "DisplayName=", "GitPilot Daemon",
    ]
    result = subprocess.run(create_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]Service creation failed: {result.stderr}[/red]")
        return

    start_result = subprocess.run(["sc", "start", service_name], capture_output=True, text=True)
    if start_result.returncode != 0:
        console.print(f"[yellow]Service created but start failed: {start_result.stderr}[/yellow]")
    else:
        console.print("[green]Windows service installed and started.[/green]")


@cli.command()
def install_service() -> None:
    """Install the daemon as a persistent background service for the current OS."""
    if sys.platform == "linux":
        _install_linux_service()
    elif sys.platform == "darwin":
        _install_macos_service()
    elif sys.platform == "win32":
        _install_windows_service()
    else:
        console.print(f"[red]Unsupported platform: {sys.platform}[/red]")
        raise SystemExit(1)


# ============================================================================
# Click CLI group
# ============================================================================
@click.group(invoke_without_command=True)
@click.version_option(version="0.1.0")
@click.pass_context
def cli(ctx):
    """GitPilot – AI-powered Git auto-committer."""
    if ctx.invoked_subcommand is None:
        _run_setup_if_needed()
        port = _get_daemon_port()
        if port is None:
            console.print("[red]Daemon is not running. Starting it in background...[/red]")
            try:
                daemon_cmd = [sys.executable, "-m", "gitpilot.daemon.server"]
                subprocess.Popen(daemon_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(20):
                    time.sleep(0.5)
                    if _get_daemon_port() is not None:
                        break
                else:
                    console.print("[red]Daemon did not start within 10 seconds.[/red]")
                    sys.exit(1)
            except Exception as e:
                console.print(f"[red]Could not start daemon: {e}[/red]")
                sys.exit(1)
        menu = MainMenu()
        menu.run()


def _run_setup_if_needed():
    token_file = get_token_path()
    settings_mgr = SettingsManager()
    config = settings_mgr.load()
    if not token_file.exists() or not config.get("ai_provider"):
        console.print("[yellow]First-run setup required.[/yellow]")
        setup.callback()


@cli.command()
def setup():
    """Interactive first-time setup (API keys, project path, etc.)."""
    console.print(Panel.fit("[bold cyan]GitPilot Setup[/bold cyan]"))
    settings_mgr = SettingsManager()
    config = settings_mgr.load()

    gh_token = Prompt.ask(
        "GitHub Personal Access Token (repo scope) – leave blank to skip",
        password=True,
        default=config.get("github_token", ""),
    )
    if gh_token:
        settings_mgr.set("github_token", gh_token)

    ai_provider = Prompt.ask(
        "Choose AI provider",
        choices=["grok", "openai", "anthropic", "ollama"],
        default=config.get("ai_provider", "grok"),
    )
    settings_mgr.set("ai_provider", ai_provider)
    if ai_provider == "grok":
        key = Prompt.ask("Grok API Key", password=True, default=config.get("grok_api_key", ""))
        settings_mgr.set("grok_api_key", key)
        model = Prompt.ask("Model", default=config.get("ai_model", "grok-2"))
        settings_mgr.set("ai_model", model)
    elif ai_provider == "openai":
        key = Prompt.ask("OpenAI API Key", password=True, default=config.get("openai_api_key", ""))
        settings_mgr.set("openai_api_key", key)
        model = Prompt.ask("Model", default=config.get("ai_model", "gpt-4o"))
        settings_mgr.set("ai_model", model)
    elif ai_provider == "anthropic":
        key = Prompt.ask("Anthropic API Key", password=True, default=config.get("anthropic_api_key", ""))
        settings_mgr.set("anthropic_api_key", key)
        model = Prompt.ask("Model", default=config.get("ai_model", "claude-3-5-sonnet-20241022"))
        settings_mgr.set("ai_model", model)
    elif ai_provider == "ollama":
        url = Prompt.ask("Ollama base URL", default=config.get("ollama_base_url", "http://localhost:11434"))
        settings_mgr.set("ollama_base_url", url)
        model = Prompt.ask("Ollama model", default=config.get("ollama_model", "llama3"))
        settings_mgr.set("ollama_model", model)

    debounce = Prompt.ask("Debounce interval (seconds)", default=str(config.get("debounce_interval", 3)))
    settings_mgr.set("debounce_interval", int(debounce))
    smart = Confirm.ask("Enable smart grouping?", default=config.get("smart_grouping", True))
    settings_mgr.set("smart_grouping", smart)
    branch_aware = Confirm.ask("Enable branch-aware messages?", default=config.get("branch_aware_messages", True))
    settings_mgr.set("branch_aware_messages", branch_aware)

    token_file = get_token_path()
    if not token_file.exists():
        token = generate_api_token()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
        token_file.chmod(0o600)

    console.print("[green]Setup complete![/green]")
    console.print("You can now run [bold]gitpilot[/bold] to launch the dashboard.")


if __name__ == "__main__":
    cli()