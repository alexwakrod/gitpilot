"""Terminal UI rendering using rich for status display and live preview."""

from datetime import datetime
from typing import Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from gitpilot.domain.settings import SettingsManager


class GitPilotTUI:
    """Interactive terminal UI for displaying daemon status and commit history."""

    def __init__(self, settings_manager: SettingsManager):
        self.settings = settings_manager
        self.console = Console()
        self.layout = Layout()
        self.live: Optional[Live] = None

    def start(self):
        """Start the live display."""
        theme = self.settings.get("theme") or "dark"
        self.console.clear()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        self.layout["body"].split_row(
            Layout(name="projects", ratio=2),
            Layout(name="commits", ratio=3),
        )
        self.live = Live(
            self._render(),
            console=self.console,
            screen=True,
            refresh_per_second=4,
            transient=False,
        )
        self.live.start()

    def stop(self):
        """Stop the live display."""
        if self.live:
            self.live.stop()
            self.live = None

    def update_projects(self, projects: list[dict]):
        """Update the project list in real time."""
        self._projects_data = projects
        self._refresh()

    def update_commits(self, commits: list[dict]):
        """Update the commit list in real time."""
        self._commits_data = commits
        self._refresh()

    def set_status(self, message: str):
        """Set the status bar message."""
        self._status_message = message
        self._refresh()

    def _refresh(self):
        if self.live:
            self.live.update(self._render())

    def _render(self) -> Group:
        """Build the full TUI layout."""
        header = self._build_header()
        body = self._build_body()
        footer = self._build_footer()
        return Group(header, body, footer)

    def _build_header(self) -> Panel:
        title = Text("GitPilot", style="bold bright_cyan")
        subtitle = Text("AI-powered Git auto-committer", style="italic")
        header_text = Text.assemble(title, " - ", subtitle)
        return Panel(header_text, box=box.HEAVY)

    def _build_body(self) -> Group:
        projects_panel = self._build_projects_panel()
        commits_panel = self._build_commits_panel()
        return Group(projects_panel, commits_panel)

    def _build_projects_panel(self) -> Panel:
        table = Table(title="Watched Projects", box=box.SIMPLE, expand=True)
        table.add_column("ID", justify="right", style="dim")
        table.add_column("Name")
        table.add_column("Path", style="bright_green")
        table.add_column("Status")

        projects = getattr(self, "_projects_data", [])
        for proj in projects:
            status = "active" if proj.get("deleted_at") is None else "inactive"
            table.add_row(
                str(proj["id"]),
                proj["name"],
                proj["path"],
                status,
            )
        return Panel(table, title="Projects", border_style="blue")

    def _build_commits_panel(self) -> Panel:
        table = Table(title="Recent Commits", box=box.SIMPLE, expand=True)
        table.add_column("Hash", style="dim")
        table.add_column("Message")
        table.add_column("Branch", style="cyan")
        table.add_column("Time", style="yellow")

        commits = getattr(self, "_commits_data", [])
        for commit in commits:
            ts = commit.get("committed_at", "")
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts)
                    ts = dt.strftime("%H:%M:%S")
                except ValueError:
                    pass
            table.add_row(
                commit["hash"][:8],
                commit["message"],
                commit.get("branch", "main"),
                ts,
            )
        return Panel(table, title="Commit History", border_style="magenta")

    def _build_footer(self) -> Panel:
        status = getattr(self, "_status_message", "Ready")
        footer_text = Text(f"Status: {status}", style="bold")
        return Panel(footer_text, box=box.MINIMAL)