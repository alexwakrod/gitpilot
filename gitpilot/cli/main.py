"""CLI interface for GitPilot – fully interactive TUI with intelligent project setup,
   API key validation, cross‑platform service, Qwen support, and all intelligence commands."""

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
from collections import Counter
from datetime import datetime, timezone
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
from gitpilot.core.project_setup import (
    is_git_repo,
    ensure_initial_commit,
    create_github_repo,
)
from gitpilot.core.executor import GitExecutor
from gitpilot.core.intelligence import DomainClassifier, CommitSplitter, OptimizationScanner
from gitpilot.core import git_utils  # native Git porcelain
from gitpilot.infrastructure.db import managed_connection, get_db_path
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.patterns import PatternsRepository

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
# API key validation (live test with detailed feedback)
# ============================================================================
async def _test_grok_api_key(key: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            )
            if resp.status_code == 200:
                return True
            else:
                console.print(f"[yellow]Grok API returned {resp.status_code}: {resp.text[:200]}[/yellow]")
                return False
    except httpx.HTTPError as e:
        console.print(f"[yellow]Network error testing Grok key: {e}[/yellow]")
        return False
    except Exception as e:
        console.print(f"[yellow]Error testing key: {e}[/yellow]")
        return False

async def _test_groq_api_key(key: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            )
            if resp.status_code == 200:
                return True
            else:
                console.print(f"[yellow]Groq API returned {resp.status_code}: {resp.text[:200]}[/yellow]")
                return False
    except httpx.HTTPError as e:
        console.print(f"[yellow]Network error testing Groq key: {e}[/yellow]")
        return False
    except Exception as e:
        console.print(f"[yellow]Error testing key: {e}[/yellow]")
        return False

async def _test_qwen_api_key(key: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            )
            if resp.status_code == 200:
                return True
            else:
                console.print(f"[yellow]Qwen API returned {resp.status_code}: {resp.text[:200]}[/yellow]")
                return False
    except Exception as e:
        console.print(f"[yellow]Error testing Qwen key: {e}[/yellow]")
        return False

async def _test_openai_api_key(key: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5},
            )
            if resp.status_code == 200:
                return True
            else:
                console.print(f"[yellow]OpenAI API returned {resp.status_code}: {resp.text[:200]}[/yellow]")
                return False
    except httpx.HTTPError as e:
        console.print(f"[yellow]Network error testing OpenAI key: {e}[/yellow]")
        return False
    except Exception as e:
        console.print(f"[yellow]Error testing key: {e}[/yellow]")
        return False

async def _test_anthropic_api_key(key: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "Hi"}]},
            )
            if resp.status_code == 200:
                return True
            else:
                console.print(f"[yellow]Anthropic API returned {resp.status_code}: {resp.text[:200]}[/yellow]")
                return False
    except httpx.HTTPError as e:
        console.print(f"[yellow]Network error testing Anthropic key: {e}[/yellow]")
        return False
    except Exception as e:
        console.print(f"[yellow]Error testing key: {e}[/yellow]")
        return False

async def _test_ollama_connection(base_url: str, model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": "Hi", "stream": False},
            )
            if resp.status_code == 200:
                return True
            else:
                console.print(f"[yellow]Ollama returned {resp.status_code}[/yellow]")
                return False
    except Exception as e:
        console.print(f"[yellow]Could not connect to Ollama: {e}[/yellow]")
        return False

def _validate_api_key_format(key: str, provider: str) -> bool:
    if not key or not key.strip():
        return False
    if provider == "grok" and not key.startswith("xai-"):
        return False
    if provider == "groq" and not key.startswith("gsk_"):
        return False
    if provider == "openai" and not key.startswith("sk-"):
        return False
    if provider == "anthropic" and not key.startswith("sk-ant-"):
        return False
    if provider == "qwen" and not key.startswith("sk-ws-"):
        return False
    return True

def _prompt_api_key_with_test(provider: str, default_key: str, model: str) -> str:
    while True:
        key = Prompt.ask(f"{provider.capitalize()} API Key", password=True, default=default_key)
        if provider == "ollama":
            return key
        if not _validate_api_key_format(key, provider):
            console.print(f"[red]Invalid key format for {provider}. Expected proper prefix.[/red]")
            retry = Confirm.ask("Retry?", default=True)
            if not retry:
                return ""
            continue
        console.print("[cyan]Testing API key...[/cyan]")
        if provider == "grok":
            ok = asyncio.run(_test_grok_api_key(key, model))
        elif provider == "groq":
            ok = asyncio.run(_test_groq_api_key(key, model))
        elif provider == "qwen":
            ok = asyncio.run(_test_qwen_api_key(key, model))
        elif provider == "openai":
            ok = asyncio.run(_test_openai_api_key(key, model))
        elif provider == "anthropic":
            ok = asyncio.run(_test_anthropic_api_key(key, model))
        if ok:
            console.print("[green]API key is valid.[/green]")
            return key
        else:
            console.print("[red]API key test failed. Check key, model, or network.[/red]")
            retry = Confirm.ask("Retry?", default=True)
            if not retry:
                return key


# ============================================================================
# Non‑blocking key input reader
# ============================================================================
class NonBlockingKeyReader:
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
# Interactive directory picker (terminal fallback)
# ============================================================================
class DirectoryPicker:
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
# Native GUI folder picker
# ============================================================================
def _pick_directory_gui() -> Optional[Path]:
    try:
        import tkinter
        import tkinter.filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = tkinter.filedialog.askdirectory(
            parent=root,
            title="Select Project Directory",
            initialdir=str(Path.home()),
        )
        root.destroy()
        if folder:
            return Path(folder)
        return None
    except Exception:
        return None


# ============================================================================
# Auto‑launch in new terminal if not in a TTY
# ============================================================================
def _spawn_in_new_terminal() -> None:
    script = f'"{sys.executable}" -m gitpilot.cli.main'
    if sys.platform == "linux":
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or \
               shutil.which("konsole") or shutil.which("xfce4-terminal") or \
               shutil.which("lxterminal") or shutil.which("xterm")
        if term:
            subprocess.Popen([term, "-e", script])
        else:
            console.print("[red]No terminal emulator found. Please install one.[/red]")
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Terminal", script])
    elif sys.platform == "win32":
        subprocess.Popen(["start", "cmd", "/k", script], shell=True)
    sys.exit(0)


# ============================================================================
# Comprehensive Git readiness check (interactive)
# ============================================================================
def _prepare_project_directory(path: Path, settings_mgr: SettingsManager) -> bool:
    """
    Check that the given path is a valid, ready Git repository.
    If not, interactively guide the user to fix missing pieces
    (git init, initial commit, remote setup, .gitignore, etc.).

    Returns True if ready (or user fixed everything), False if user cancels.
    """
    if not path.exists() or not path.is_dir():
        console.print("[red]The selected directory does not exist or is not a directory.[/red]")
        return False

    # 1. Git repository?
    if not is_git_repo(path):
        console.print("[yellow]This directory is not a Git repository.[/yellow]")
        if not Confirm.ask("Would you like to initialize a Git repository here?", default=True):
            return False
        executor = GitExecutor()
        if not executor.init_repo(path):
            console.print("[red]Failed to initialize Git repository.[/red]")
            return False
        console.print("[green]Git repository initialized.[/green]")
    else:
        console.print("[green]✓ Git repository found.[/green]")

    # 2. At least one commit?
    if not git_utils.has_commits(path):
        console.print("[yellow]No commits found in this repository.[/yellow]")
        if Confirm.ask("Create an initial commit?", default=True):
            if not ensure_initial_commit(path):
                console.print("[red]Could not create initial commit. Proceeding anyway, but commits may fail.[/red]")
            else:
                console.print("[green]Initial commit created.[/green]")
        # Allow user to skip, but warn
    else:
        console.print("[green]✓ Repository has commits.[/green]")

    # 3. .gitignore existence (optional but recommended)
    if not (path / ".gitignore").exists():
        console.print("[dim]No .gitignore file found. Consider adding one to avoid committing artifacts.[/dim]")

    # 4. Remote origin?
    if not git_utils.has_remote_origin(path):
        console.print("[yellow]No remote 'origin' configured.[/yellow]")
        config = settings_mgr.load()
        if config.get("github_token"):
            if Confirm.ask("Would you like to create a GitHub repository and set it as origin?", default=False):
                repo_name = Prompt.ask("Repository name", default=path.name)
                private = Confirm.ask("Private repository?", default=True)
                clone_url = create_github_repo(
                    name=repo_name,
                    private=private,
                    github_token=config["github_token"],
                )
                if clone_url:
                    executor = GitExecutor()
                    if executor.set_remote_origin(path, clone_url):
                        console.print("[green]Remote origin added successfully.[/green]")
                    else:
                        console.print("[red]Failed to set remote origin. You can add it later manually.[/red]")
                else:
                    console.print("[yellow]Could not create GitHub repository. Proceeding locally.[/yellow]")
            else:
                console.print("[dim]Skipping remote setup. You can add one later with `git remote add origin <url>`.[/dim]")
        else:
            console.print("[dim]No GitHub token configured. Run `gitpilot setup` to add one for automatic remote creation.[/dim]")
    else:
        console.print("[green]✓ Remote 'origin' is configured.[/green]")

    return True


# ============================================================================
# Main TUI
# ============================================================================
class MainMenu:
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
        path = _pick_directory_gui()
        if path is None:
            picker = DirectoryPicker()
            path = picker.pick()
        if path is None:
            return
        console.clear()
        console.print(f"Selected: [bold]{path}[/bold]")

        # Run comprehensive Git readiness check
        if not _prepare_project_directory(path, self.settings_mgr):
            console.print("[red]Project setup cancelled.[/red]")
            console.print("\nPress any key to continue...", end="")
            readchar.readkey()
            return

        name = Prompt.ask("Project name (default: directory name)", default=path.name)
        payload = {"name": name, "path": str(path)}
        resp = self.client.post("/api/v1/projects", json=payload)
        if resp.status_code == 201:
            console.print(f"[green]Project '{name}' added (ID: {resp.json()['id']})[/green]")
        else:
            console.print(f"[red]Error: {resp.status_code} {resp.text}[/red]")
        console.print("\nPress any key to continue...", end="")
        readchar.readkey()

    def _monitor(self):
        # unchanged
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
            "4": "Toggle branch‑aware messages / smart grouping / domain splitting",
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
        provider = Prompt.ask("New provider",
                              choices=["grok", "groq", "qwen", "openai", "anthropic", "ollama"],
                              default=current)
        self.settings_mgr.set("ai_provider", provider)

        if provider == "grok":
            model = Prompt.ask("Model", default=config.get("ai_model", "grok-2"))
            self.settings_mgr.set("ai_model", model)
            key = _prompt_api_key_with_test("grok", config.get("grok_api_key", ""), model)
            self.settings_mgr.set("grok_api_key", key)
        elif provider == "groq":
            model = Prompt.ask("Model", default=config.get("groq_model", "llama3-70b-8192"))
            self.settings_mgr.set("groq_model", model)
            key = _prompt_api_key_with_test("groq", config.get("groq_api_key", ""), model)
            self.settings_mgr.set("groq_api_key", key)
        elif provider == "qwen":
            model = Prompt.ask("Model", default=config.get("qwen_model", "qwen-plus"))
            self.settings_mgr.set("qwen_model", model)
            key = _prompt_api_key_with_test("qwen", config.get("qwen_api_key", ""), model)
            self.settings_mgr.set("qwen_api_key", key)
        elif provider == "openai":
            model = Prompt.ask("Model", default=config.get("ai_model", "gpt-4o"))
            self.settings_mgr.set("ai_model", model)
            key = _prompt_api_key_with_test("openai", config.get("openai_api_key", ""), model)
            self.settings_mgr.set("openai_api_key", key)
        elif provider == "anthropic":
            model = Prompt.ask("Model", default=config.get("ai_model", "claude-3-5-sonnet-20241022"))
            self.settings_mgr.set("ai_model", model)
            key = _prompt_api_key_with_test("anthropic", config.get("anthropic_api_key", ""), model)
            self.settings_mgr.set("anthropic_api_key", key)
        elif provider == "ollama":
            url = Prompt.ask("Ollama base URL", default=config.get("ollama_base_url", "http://localhost:11434"))
            self.settings_mgr.set("ollama_base_url", url)
            model = Prompt.ask("Ollama model", default=config.get("ollama_model", "llama3"))
            self.settings_mgr.set("ollama_model", model)
            console.print("[cyan]Testing connection to Ollama...[/cyan]")
            ok = asyncio.run(_test_ollama_connection(url, model))
            if ok:
                console.print("[green]Ollama connection successful.[/green]")
            else:
                console.print("[red]Could not connect to Ollama. Check URL and model.[/red]")
        console.print("[green]AI provider updated.[/green]")
        time.sleep(1)

    def _change_github_token(self):
        config = self.settings_mgr.load()
        token = Prompt.ask("GitHub Personal Access Token (repo scope)",
                           password=True, default=config.get("github_token", ""))
        if token and not token.startswith("ghp_") and len(token) < 40:
            console.print("[yellow]Token format may be invalid. Ensure it has 'repo' scope.[/yellow]")
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
        if not webhook_url.startswith("https://discord.com/api/webhooks/"):
            console.print("[yellow]Invalid Discord webhook URL.[/yellow]")
            return
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
        branch = Confirm.ask("Branch‑aware messages?", default=config.get("branch_aware_messages", True))
        self.settings_mgr.set("branch_aware_messages", branch)
        split = Confirm.ask("Domain‑aware commit splitting?", default=config.get("enable_splitting", True))
        self.settings_mgr.set("enable_splitting", split)
        optim = Confirm.ask("Enable optimization hints in commit messages?", default=config.get("enable_optimizations", False))
        self.settings_mgr.set("enable_optimizations", optim)
        debounce = Prompt.ask("Debounce interval (seconds)", default=str(config.get("debounce_interval", 3)))
        self.settings_mgr.set("debounce_interval", int(debounce))
        console.print("[green]Settings updated.[/green]")
        time.sleep(1)


# ============================================================================
# Cross‑platform service installation
# ============================================================================
def _install_linux_service() -> None:
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


# ============================================================================
# Click CLI group
# ============================================================================
@click.group(invoke_without_command=True)
@click.version_option(version="0.2.0")
@click.pass_context
def cli(ctx):
    """GitPilot – AI-powered Git auto‑committer."""
    if ctx.invoked_subcommand is None:
        _run_setup_if_needed()
        if not sys.stdin.isatty():
            _spawn_in_new_terminal()
            return
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
    """Interactive first-time setup."""
    console.print(Panel.fit("[bold cyan]GitPilot Setup[/bold cyan]"))
    settings_mgr = SettingsManager()
    config = settings_mgr.load()

    gh_token = Prompt.ask(
        "GitHub Personal Access Token (repo scope) – leave blank to skip",
        password=True,
        default=config.get("github_token", ""),
    )
    if gh_token and not gh_token.startswith("ghp_") and len(gh_token) < 40:
        console.print("[yellow]Token format may be invalid. Ensure it has 'repo' scope.[/yellow]")
    settings_mgr.set("github_token", gh_token)

    ai_provider = Prompt.ask(
        "Choose AI provider",
        choices=["grok", "groq", "qwen", "openai", "anthropic", "ollama"],
        default=config.get("ai_provider", "grok"),
    )
    settings_mgr.set("ai_provider", ai_provider)
    if ai_provider == "grok":
        model = Prompt.ask("Model", default=config.get("ai_model", "grok-2"))
        settings_mgr.set("ai_model", model)
        key = _prompt_api_key_with_test("grok", config.get("grok_api_key", ""), model)
        settings_mgr.set("grok_api_key", key)
    elif ai_provider == "groq":
        model = Prompt.ask("Model", default=config.get("groq_model", "llama3-70b-8192"))
        settings_mgr.set("groq_model", model)
        key = _prompt_api_key_with_test("groq", config.get("groq_api_key", ""), model)
        settings_mgr.set("groq_api_key", key)
    elif ai_provider == "qwen":
        model = Prompt.ask("Model", default=config.get("qwen_model", "qwen-plus"))
        settings_mgr.set("qwen_model", model)
        key = _prompt_api_key_with_test("qwen", config.get("qwen_api_key", ""), model)
        settings_mgr.set("qwen_api_key", key)
    elif ai_provider == "openai":
        model = Prompt.ask("Model", default=config.get("ai_model", "gpt-4o"))
        settings_mgr.set("ai_model", model)
        key = _prompt_api_key_with_test("openai", config.get("openai_api_key", ""), model)
        settings_mgr.set("openai_api_key", key)
    elif ai_provider == "anthropic":
        model = Prompt.ask("Model", default=config.get("ai_model", "claude-3-5-sonnet-20241022"))
        settings_mgr.set("ai_model", model)
        key = _prompt_api_key_with_test("anthropic", config.get("anthropic_api_key", ""), model)
        settings_mgr.set("anthropic_api_key", key)
    elif ai_provider == "ollama":
        url = Prompt.ask("Ollama base URL", default=config.get("ollama_base_url", "http://localhost:11434"))
        settings_mgr.set("ollama_base_url", url)
        model = Prompt.ask("Ollama model", default=config.get("ollama_model", "llama3"))
        settings_mgr.set("ollama_model", model)
        console.print("[cyan]Testing connection to Ollama...[/cyan]")
        ok = asyncio.run(_test_ollama_connection(url, model))
        if ok:
            console.print("[green]Ollama connection successful.[/green]")
        else:
            console.print("[red]Could not connect to Ollama. Check URL and model.[/red]")

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


@cli.command()
def install_service() -> None:
    """Install the daemon as a persistent background service."""
    if sys.platform == "linux":
        _install_linux_service()
    elif sys.platform == "darwin":
        _install_macos_service()
    elif sys.platform == "win32":
        _install_windows_service()
    else:
        console.print(f"[red]Unsupported platform: {sys.platform}[/red]")
        raise SystemExit(1)


@cli.command()
def daemon_status() -> None:
    """Check if the daemon is running."""
    port = _get_daemon_port()
    if port is None:
        console.print("[red]Daemon is not running.[/red]")
        return
    token = _get_api_token()
    if token is None:
        console.print("[red]Token not found.[/red]")
        return
    try:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            console.print(f"[green]Daemon is running on port {port}.[/green]")
        else:
            console.print(f"[yellow]Daemon responded but unexpected status {resp.status_code}.[/yellow]")
    except httpx.RequestError:
        console.print("[red]Daemon is not reachable. It may have crashed.[/red]")


@cli.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--name", "-n", help="Project display name")
@click.option("--github-repo", help="Create a private GitHub repository with this name")
def add(path: str, name: Optional[str], github_repo: Optional[str]) -> None:
    """Register a project directory for auto‑commit watching."""
    dir_path = Path(path)
    # Run readiness check locally before calling the daemon
    settings_mgr = SettingsManager()
    if not _prepare_project_directory(dir_path, settings_mgr):
        console.print("[red]Project setup cancelled due to readiness issues.[/red]")
        return

    client = _get_client()
    if client is None:
        return
    project_name = name or dir_path.name
    payload = {"name": project_name, "path": str(dir_path)}
    if github_repo:
        payload["github_repo_name"] = github_repo
    resp = client.post("/api/v1/projects", json=payload)
    if resp.status_code == 201:
        data = resp.json()
        console.print(f"[green]Project '{data['name']}' added (ID: {data['id']})[/green]")
    elif resp.status_code == 409:
        console.print("[yellow]Project path already registered.[/yellow]")
    else:
        console.print(f"[red]Failed to add project: {resp.status_code} {resp.text}[/red]")


@cli.command()
def status() -> None:
    """Display daemon status and watched projects."""
    client = _get_client()
    if client is None:
        return
    try:
        resp = client.get("/api/v1/projects")
        if resp.status_code == 200:
            data = resp.json()
            projects = data.get("items", [])
            if not projects:
                console.print("[yellow]No projects registered.[/yellow]")
                return
            table = Table(title="Watched Projects")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="green")
            table.add_column("Path")
            table.add_column("Status")
            for proj in projects:
                status_text = "active" if proj.get("deleted_at") is None else "inactive"
                table.add_row(str(proj["id"]), proj["name"], proj["path"], status_text)
            console.print(table)
        else:
            console.print(f"[red]Failed to fetch projects: {resp.status_code}[/red]")
    except httpx.RequestError as e:
        console.print(f"[red]Cannot connect to daemon: {e}[/red]")


@cli.command()
@click.argument("project_id", type=int)
@click.option("--limit", default=10, help="Number of commits")
@click.option("--domain", default=None, help="Filter by domain (ui, backend, database, test, config, docs)")
def log(project_id: int, limit: int, domain: Optional[str]) -> None:
    """Show recent commit history for a project."""
    client = _get_client()
    if client is None:
        return
    params = {"project_id": project_id, "limit": limit}
    if domain:
        params["domain"] = domain
    resp = client.get("/api/v1/commits", params=params)
    if resp.status_code == 200:
        data = resp.json()
        commits = data.get("items", [])
        if not commits:
            console.print("[yellow]No commits recorded.[/yellow]")
            return
        table = Table(title=f"Commit History (Project {project_id})")
        table.add_column("ID", style="cyan")
        table.add_column("Hash", style="magenta")
        table.add_column("Domain", style="blue")
        table.add_column("Message")
        table.add_column("Branch")
        table.add_column("Committed At")
        for c in commits:
            table.add_row(
                str(c["id"]),
                c["hash"][:8],
                c.get("domain", "general"),
                c["message"],
                c.get("branch", "main"),
                c["committed_at"][:19] if c.get("committed_at") else "N/A",
            )
        console.print(table)
    else:
        console.print(f"[red]Failed to fetch commits: {resp.status_code} {resp.text}[/red]")


@cli.command()
@click.argument("project_id", type=int)
def remove(project_id: int) -> None:
    """Stop watching and soft‑delete a project."""
    client = _get_client()
    if client is None:
        return
    resp = client.delete(f"/api/v1/projects/{project_id}")
    if resp.status_code == 200:
        console.print(f"[green]Project {project_id} removed from watching.[/green]")
    else:
        console.print(f"[red]Failed to remove project: {resp.status_code}[/red]")


@cli.command()
def config_list() -> None:
    """Display current configuration."""
    client = _get_client()
    if client is None:
        return
    resp = client.get("/api/v1/config")
    if resp.status_code == 200:
        data = resp.json()
        items = data.get("items", [])
        if not items:
            console.print("[yellow]No configuration found.[/yellow]")
            return
        table = Table(title="Configuration")
        table.add_column("Key")
        table.add_column("Value")
        table.add_column("Type")
        for item in items:
            table.add_row(item["key"], item["value"], item["type"])
        console.print(table)
    else:
        console.print(f"[red]Failed to fetch config: {resp.status_code}[/red]")


@cli.command()
@click.argument("key")
@click.argument("value")
@click.option("--type", "value_type", default="string", help="string, integer, boolean, json")
def config_set(key: str, value: str, value_type: str) -> None:
    """Set a configuration value."""
    client = _get_client()
    if client is None:
        return
    resp = client.put(
        f"/api/v1/config/{key}",
        json={"value": value, "type": value_type},
    )
    if resp.status_code == 200:
        console.print(f"[green]Setting '{key}' updated.[/green]")
    else:
        console.print(f"[red]Failed to set config: {resp.status_code} {resp.text}[/red]")


@cli.command()
@click.argument("key")
def config_delete(key: str) -> None:
    """Delete a configuration setting."""
    client = _get_client()
    if client is None:
        return
    resp = client.delete(f"/api/v1/config/{key}")
    if resp.status_code == 200:
        console.print(f"[green]Setting '{key}' deleted.[/green]")
    else:
        console.print(f"[red]Failed to delete setting: {resp.status_code} {resp.text}[/red]")


@cli.command()
def watch() -> None:
    """Connect to daemon and display real‑time events (SSE)."""
    port = _get_daemon_port()
    if port is None:
        console.print("[red]Daemon not running.[/red]")
        return
    token = _get_api_token()
    if token is None:
        console.print("[red]Token not found.[/red]")
        return
    console.print(f"[cyan]Connecting to daemon on port {port}...[/cyan]")

    async def event_listener():
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=None,
        ) as client:
            try:
                async with client.stream("GET", "/api/v1/events") as response:
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                            console.print(f"[bold yellow]Event:[/bold yellow] {event_type}")
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()
                            try:
                                data = json.loads(data_str)
                                console.print(f"  {json.dumps(data, indent=2)}")
                            except json.JSONDecodeError:
                                console.print(f"  {data_str}")
                        elif line.startswith(":"):
                            pass
            except httpx.RequestError as e:
                console.print(f"[red]Connection lost: {e}[/red]")

    asyncio.run(event_listener())


# ------------------------------------------------------------------
# Intelligence commands (using git_utils)
# ------------------------------------------------------------------

@cli.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False, resolve_path=True), default=".")
def split_status(project_path: str) -> None:
    """Show how uncommitted changes would be grouped by domain (native Git porcelain)."""
    repo_path = Path(project_path)
    if not is_git_repo(repo_path):
        console.print("[red]Not a Git repository.[/red]")
        return

    try:
        plan = git_utils.get_domain_split_plan(repo_path)
        if not plan:
            console.print("No uncommitted changes detected (including untracked files).")
            return

        table = Table(title="Domain Split Plan")
        table.add_column("Domain", style="bold cyan")
        table.add_column("Files")
        for domain, files in plan.items():
            file_list = ", ".join(files)
            table.add_row(domain, file_list)
        console.print(table)
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


@cli.command()
def suggest() -> None:
    """Show pending intelligent suggestions (squash candidates, patterns)."""
    try:
        with managed_connection(get_db_path()) as conn:
            commits_repo = CommitsRepository(conn)
            projects_repo = ProjectsRepository(conn)
            projects, _ = projects_repo.list_all(get_current_os_user(), limit=100)
            suggestions = []
            for proj in projects:
                commits, _ = commits_repo.list_by_project(proj["id"], limit=50)
                for commit in commits:
                    if commit.get("squash_candidate"):
                        suggestions.append(
                            f"  Squash candidate: {commit['hash'][:8]} on {commit['branch']} ({commit['domain']})"
                        )
            if suggestions:
                console.print("[bold]Squash Suggestions:[/bold]")
                for s in suggestions:
                    console.print(s)
            else:
                console.print("No squash suggestions at this time.")
            console.print("[yellow]Branch/PR suggestions require more context; coming soon.[/yellow]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


@cli.command()
@click.argument("branch", default="")
def squash(branch: str) -> None:
    """Squash the most recent related commits on a branch (placeholder)."""
    console.print("[yellow]Squash command is not yet implemented. Use `git rebase -i` manually.[/yellow]")


@cli.command()
@click.argument("branch", default="")
def pr(branch: str) -> None:
    """Create a GitHub Pull Request with an AI‑generated description (placeholder)."""
    console.print("[yellow]PR creation requires GitHub CLI or API integration. Coming soon.[/yellow]")


@cli.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False, resolve_path=True), default=".")
def optimize(project_path: str) -> None:
    """Run AI‑powered code review on current diff (staged and unstaged)."""
    repo_path = Path(project_path)
    if not is_git_repo(repo_path):
        console.print("[red]Not a Git repository.[/red]")
        return

    diff = git_utils.get_staged_diff(repo_path)
    if not diff:
        try:
            result = subprocess.run(
                ["git", "diff"],
                cwd=str(repo_path), capture_output=True, text=True, timeout=10,
            )
            diff = result.stdout if result.returncode == 0 else ""
        except Exception:
            pass
    if not diff:
        console.print("No changes to analyze.")
        return

    warnings = OptimizationScanner.scan_diff(diff)
    if warnings:
        console.print("[bold]Optimization Suggestions:[/bold]")
        for w in warnings:
            console.print(f"  • {w}")
    else:
        console.print("[green]No optimization issues found.[/green]")


@cli.command()
def stats() -> None:
    """Show learned patterns and usage statistics."""
    try:
        with managed_connection(get_db_path()) as conn:
            patterns_repo = PatternsRepository(conn)
            owner = get_current_os_user()
            patterns = patterns_repo.list_by_owner(owner)
            if not patterns:
                console.print("[yellow]No learned patterns yet. Keep committing to train GitPilot.[/yellow]")
                return
            table = Table(title="Learned Patterns")
            table.add_column("Type")
            table.add_column("Value")
            table.add_column("Confidence")
            for p in patterns:
                table.add_row(p["pattern_type"], str(p["value"]), f"{p['confidence']:.0%}")
            console.print(table)

            commits_repo = CommitsRepository(conn)
            projects_repo = ProjectsRepository(conn)
            projects, _ = projects_repo.list_all(owner, limit=100)
            total_commits = 0
            domain_counts = Counter()
            for proj in projects:
                commits, _ = commits_repo.list_by_project(proj["id"], limit=1000)
                total_commits += len(commits)
                for c in commits:
                    domain_counts[c.get("domain", "general")] += 1
            console.print(f"\nTotal commits recorded: {total_commits}")
            if domain_counts:
                console.print("Commits by domain:")
                for domain, count in domain_counts.most_common():
                    console.print(f"  {domain}: {count}")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")


@cli.command()
@click.argument("action", type=click.Choice(["on", "off"]))
def config_review(action: str) -> None:
    """Enable or disable code review gating before commits."""
    settings_mgr = SettingsManager()
    settings_mgr.set("enable_optimizations", action == "on")
    console.print(f"[green]Code review gating turned {action}.[/green]")


if __name__ == "__main__":
    cli()