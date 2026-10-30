"""Pane showing background ansible-playbook runs, one tab per run."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.markup import escape
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import RichLog, Static, TabbedContent, TabPane

from ..runner import Job

STATUS_STYLES = {
    "pending": ("○", "dim"),
    "starting": ("○", "cyan"),
    "running": ("●", "cyan"),
    "successful": ("✔", "green"),
    "failed": ("✖", "red"),
    "error": ("✖", "red"),
    "canceled": ("■", "yellow"),
    "timeout": ("■", "yellow"),
}
HOST_STYLES = {
    "ok": ("✔", "green"),
    "changed": ("●", "yellow"),
    "failed": ("✖", "red"),
    "unreachable": ("⚠", "magenta"),
    "skipped": ("-", "dim"),
}


class JobOutput(Message):
    """Posted from the runner thread with new output for a job."""

    def __init__(self, job: Job, text: str) -> None:
        super().__init__()
        self.job = job
        self.text = text


class JobUpdated(Message):
    """Posted from the runner thread when a job's status changes."""

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job


def _natural_key(name: str) -> list[int | str]:
    """Sort web2 before web10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def status_text(job: Job) -> Text:
    glyph, style = STATUS_STYLES.get(job.status, ("?", ""))
    return Text(f"{glyph} {job.status}", style=style)


class JobView(Vertical):
    def __init__(self, job: Job, **kwargs) -> None:
        super().__init__(**kwargs)
        self.job = job
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(classes="job-summary")
        yield RichLog(highlight=False, markup=False, wrap=True, max_lines=20_000)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write(Text(f"$ cd {self.job.workdir}", style="dim"))
        log.write(Text(f"$ {self.job.command}", style="bold"))
        self.refresh_summary()
        self._timer = self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self.refresh_summary()
        if self.job.done:
            self._timer.stop()

    def append(self, text: str) -> None:
        # ansible-runner output uses \r\n; a bare \r would blank the line when rendered.
        text = text.replace("\r\n", "\n").replace("\r", "")
        self.query_one(RichLog).write(Text.from_ansi(text.rstrip("\n")))

    def refresh_summary(self) -> None:
        # One line whatever the number of hosts, so the output below keeps its room. Host
        # names and the log path go into the output instead: see finish().
        job = self.job
        summary = Text.assemble((job.title, "bold"), "  ", status_text(job))
        summary.append(f"  {job.elapsed:.0f}s", style="dim")
        if job.rc is not None:
            summary.append(f"  rc={job.rc}", style="dim")
        counts = Counter(job.hosts.values())
        if counts:
            summary.append("  ")
            for state, (glyph, style) in HOST_STYLES.items():
                if counts[state]:
                    summary.append(f"  {glyph} {counts[state]} {state}", style=style)
        self.query_one(".job-summary", Static).update(summary)

    def finish(self) -> None:
        """Once the run is done, end the output with the hosts that need attention and the log path."""
        if self._finished:
            return
        self._finished = True
        log = self.query_one(RichLog)
        for state in ("failed", "unreachable"):
            hosts = sorted(
                (host for host, host_state in self.job.hosts.items() if host_state == state),
                key=_natural_key,
            )
            if hosts:
                glyph, style = HOST_STYLES[state]
                log.write(Text.assemble((f"{glyph} {state} ({len(hosts)}): ", f"bold {style}"), ", ".join(hosts)))
        if self.job.log_file:
            log_file = str(self.job.log_file).replace(str(Path.home()), "~", 1)
            log.write(Text(f"log: {log_file}  (F7 to open)", style="dim"))


class JobsPane(Vertical):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.jobs: dict[int, Job] = {}
        self.border_title = "Runs"

    def compose(self) -> ComposeResult:
        yield TabbedContent(id="job-tabs")

    @staticmethod
    def _pane_id(job: Job) -> str:
        return f"job-{job.id}"

    async def add_job(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.add_class("visible")
        tabs = self.query_one("#job-tabs", TabbedContent)
        pane_id = self._pane_id(job)
        await tabs.add_pane(TabPane(self._tab_label(job), JobView(job), id=pane_id))
        tabs.active = pane_id

    def view_for(self, job: Job) -> JobView | None:
        try:
            return self.query_one(f"#{self._pane_id(job)} JobView", JobView)
        except Exception:
            return None

    def current_job(self) -> Job | None:
        active = self.query_one("#job-tabs", TabbedContent).active
        if not active.startswith("job-"):
            return None
        return self.jobs.get(int(active.removeprefix("job-")))

    def current_view(self) -> JobView | None:
        job = self.current_job()
        return self.view_for(job) if job else None

    def running_jobs(self) -> list[Job]:
        return [job for job in self.jobs.values() if not job.done]

    def append(self, job: Job, text: str) -> None:
        view = self.view_for(job)
        if view is not None:
            view.append(text)
            view.refresh_summary()

    def update_job(self, job: Job) -> None:
        view = self.view_for(job)
        if view is not None:
            view.refresh_summary()
            if job.done:
                view.finish()
        tabs = self.query_one("#job-tabs", TabbedContent)
        try:
            tabs.get_tab(self._pane_id(job)).label = self._tab_label(job)
        except Exception:
            pass

    @staticmethod
    def _tab_label(job: Job) -> str:
        # Tab titles take content markup, not Rich Text objects.
        glyph, style = STATUS_STYLES.get(job.status, ("?", ""))
        return f"[{style}]{glyph}[/] {escape(job.title)}"
