"""The Ansicurse Textual application."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, RichLog, Static

from . import state
from .playbooks import Entry, EntryKind, inspect_file
from .project import ProjectInfo, project_info, uses_vault
from .runner import Job, RunOptions, find_ansible_playbook, looks_like_production
from .widgets.browser import Browser
from .widgets.detail import DetailPane
from .widgets.dialogs import ConfirmDialog, PasswordDialog, RunDialog
from .widgets.jobs import JobOutput, JobsPane, JobUpdated

# Actions that open dialogs or start work; disabled while a dialog is showing.
MAIN_SCREEN_ACTIONS = frozenset(
    {
        "run", "check", "external_edit", "next_view", "stop",
        "toggle_runs", "maximize_runs", "maximize_view", "run_log",
    }
)


class AnsicurseApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "Ansicurse"

    BINDINGS = [
        Binding("f2", "check", "Syntax check"),
        Binding("f3", "next_view", "View"),
        Binding("f4", "external_edit", "$EDITOR"),
        Binding("f5", "run", "Run"),
        Binding("f6", "toggle_runs", "Runs"),
        Binding("f7", "run_log", "Run log"),
        Binding("f8", "stop", "Stop run"),
        Binding("f10", "maximize_runs", "Fullscreen runs"),
        Binding("f12", "maximize_view", "Fullscreen view"),
        Binding("f9", "forget_passwords", "Forget passwords", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, start_dir: Path) -> None:
        super().__init__()
        self.launch_dir = start_dir
        self.current_entry: Entry | None = None
        self.saved_options = state.load_options()
        self.ansible_playbook = find_ansible_playbook()
        self._all_jobs: list[Job] = []
        # (kind, workdir) -> password. In memory only; never saved with the run options.
        self._passwords: dict[tuple[str, str], str] = {}

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="main"):
            yield Browser(self.launch_dir, id="browser")
            yield DetailPane(id="detail")
        yield JobsPane(id="jobs")
        yield Footer()

    def on_mount(self) -> None:
        self._delete_old_runs()
        if self.ansible_playbook is None:
            self.notify(
                "ansible-playbook was not found on PATH. Install Ansible to run playbooks.",
                severity="warning",
                timeout=8,
            )

    @work(thread=True, group="cleanup")
    def _delete_old_runs(self) -> None:
        # In a thread, so a big runs/ folder doesn't hold up the first screen.
        state.delete_old_runs()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in MAIN_SCREEN_ACTIONS and len(self.screen_stack) > 1:
            return False
        return True

    @property
    def browser(self) -> Browser:
        return self.query_one("#browser", Browser)

    @property
    def detail(self) -> DetailPane:
        return self.query_one("#detail", DetailPane)

    @property
    def jobs(self) -> JobsPane:
        return self.query_one("#jobs", JobsPane)

    # Browser events

    @on(Browser.DirectoryChanged)
    def _directory_changed(self, message: Browser.DirectoryChanged) -> None:
        info = project_info(message.directory, self.launch_dir)
        home = str(Path.home())

        def short(path: Path) -> str:
            return str(path).replace(home, "~", 1)

        bar = Text()
        bar.append("Ansicurse", style="bold")
        bar.append("   workdir ", style="dim")
        bar.append(short(info.workdir))
        bar.append("   cfg ", style="dim")
        bar.append(short(info.config) if info.config else "—")
        bar.append("   inventory ", style="dim")
        bar.append(info.config_inventory or info.inventory or "—")
        if self.ansible_playbook is None:
            bar.append("   ansible-playbook not found", style="bold red")
        self.query_one("#topbar", Static).update(bar)

    @on(Browser.Highlighted)
    def _highlighted(self, message: Browser.Highlighted) -> None:
        self.current_entry = message.entry
        self.detail.show(message.entry)

    @on(Browser.Activated)
    def _activated(self, message: Browser.Activated) -> None:
        if message.entry.kind is EntryKind.PLAYBOOK:
            self.action_run()
        elif message.entry.kind is not EntryKind.VAULT:
            self.detail.focus_editor()

    # Running playbooks

    def _options_for(self, entry: Entry) -> RunOptions:
        info = project_info(entry.path.parent, self.launch_dir)
        saved = self.saved_options.get(str(entry.path), {})
        uses_become = bool(entry.playbook and entry.playbook.uses_become) or saved.get("become", False)
        detected = RunOptions(
            inventory=info.inventory or "",
            # Only ask when Ansible won't already read the password from a file.
            ask_become_pass=uses_become and not info.become_password_file,
            ask_vault_pass=not info.vault_password_file and uses_vault(info.workdir, entry.path),
        )
        # Saved options win, but ones saved by an older version lack the newer keys:
        # those keep the detected values instead of falling back to off.
        return RunOptions.from_dict({**detected.to_dict(), **saved})

    def _selected_playbook(self) -> Entry | None:
        entry = self.current_entry
        if entry is None or entry.kind is not EntryKind.PLAYBOOK:
            self.notify("Select a playbook first.", severity="warning", timeout=3)
            return None
        return entry

    def action_run(self) -> None:
        entry = self._selected_playbook()
        if entry is None:
            return
        info = project_info(entry.path.parent, self.launch_dir)

        def dialog_closed(options: RunOptions | None) -> None:
            if options is None:
                return
            self.saved_options[str(entry.path)] = options.to_dict()
            state.save_options(self.saved_options)
            self._prepare_run(entry, info, options)

        self.push_screen(RunDialog(entry, self._options_for(entry), info), dialog_closed)

    @work(group="prepare-run")
    async def _prepare_run(self, entry: Entry, info: ProjectInfo, options: RunOptions) -> None:
        """Confirm production runs and collect passwords, then start the job."""
        if looks_like_production(options):
            command = Job(entry.path, options, info.workdir).command
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    f"Run {entry.name} against what looks like PRODUCTION?\n\n$ {command}",
                    confirm_label="Run it",
                )
            )
            if not confirmed:
                return

        passwords: dict[str, str] = {}
        for kind in options.password_kinds:
            key = (kind, str(info.workdir))
            if key not in self._passwords:
                password = await self.push_screen_wait(PasswordDialog(kind, info.workdir))
                if password is None:
                    self.notify("Run cancelled.", timeout=3)
                    return
                self._passwords[key] = password
            passwords[kind] = self._passwords[key]

        self._launch(Job(entry.path, options, info.workdir, passwords=passwords))

    def action_check(self) -> None:
        entry = self._selected_playbook()
        if entry is None:
            return
        info = project_info(entry.path.parent, self.launch_dir)
        base = self._options_for(entry)
        options = RunOptions(inventory=base.inventory, syntax_check=True)
        self._launch(Job(entry.path, options, info.workdir))

    def _launch(self, job: Job) -> None:
        self._all_jobs.append(job)

        async def start() -> None:
            await self.jobs.add_job(job)
            job.start(
                on_output=lambda j, text: self.post_message(JobOutput(j, text)),
                on_status=lambda j: self.post_message(JobUpdated(j)),
            )
            self.jobs.update_job(job)

        self.run_worker(start(), group="jobs")

    @on(JobOutput)
    def _job_output(self, message: JobOutput) -> None:
        self.jobs.append(message.job, message.text)

    @on(JobUpdated)
    def _job_updated(self, message: JobUpdated) -> None:
        job = message.job
        self.jobs.update_job(job)
        if job.done:
            severity = "information" if job.status == "successful" else "error"
            self.notify(f"{job.title}: {job.status}", severity=severity, timeout=5)
            for kind in job.password_errors:
                # Forget a rejected password so the next run asks again.
                if self._passwords.pop((kind, str(job.workdir)), None) is not None:
                    self.notify(
                        f"Ansible rejected the {kind} password; you'll be asked again next run.",
                        severity="warning",
                        timeout=8,
                    )
            if "become" in job.missing_passwords:
                # Tick "Ask become password" for next time, so the retry just works.
                saved = self.saved_options.setdefault(str(job.playbook), {})
                saved["ask_become_pass"] = True
                state.save_options(self.saved_options)
                self.notify(
                    "The hosts wanted a sudo password, but none was given. "
                    '"Ask become password (-K)" is now ticked for this playbook; run it again.',
                    severity="warning",
                    timeout=12,
                )

    def action_forget_passwords(self) -> None:
        count = len(self._passwords)
        self._passwords.clear()
        self.notify(f"Forgot {count} password{'s' if count != 1 else ''}.", timeout=3)

    def action_stop(self) -> None:
        job = self.jobs.current_job()
        if job is None or job.done:
            self.notify("No running job selected.", timeout=3)
            return
        job.cancel()
        self.notify(f"Stopping {job.title}…", timeout=3)

    def action_toggle_runs(self) -> None:
        self.screen.remove_class("view-maximized")
        had_focus = self.jobs.has_focus_within
        self.jobs.toggle_class("visible")
        if not self.jobs.has_class("visible"):
            self.screen.remove_class("runs-maximized")
            if had_focus:
                self.browser.query_one("#files").focus()

    def action_maximize_runs(self) -> None:
        """Switch the runs pane between its normal size and the full height of the screen."""
        self.screen.remove_class("view-maximized")
        maximized = self.screen.toggle_class("runs-maximized").has_class("runs-maximized")
        if maximized:
            self.jobs.add_class("visible")
            # So the arrow keys and PgUp scroll the output straight away.
            if (view := self.jobs.current_view()) is not None:
                view.query_one(RichLog).focus()
        elif self.jobs.has_focus_within:
            self.browser.query_one("#files").focus()

    def action_maximize_view(self) -> None:
        """Switch the view/edit pane between its normal size and the whole screen."""
        self.screen.remove_class("runs-maximized")
        if self.screen.toggle_class("view-maximized").has_class("view-maximized"):
            self.detail.focus_active_tab()

    def action_run_log(self) -> None:
        """Open the selected run's full output (ansible-runner's stdout file) in $PAGER."""
        job = self.jobs.current_job()
        log_file = job.log_file if job else None
        if log_file is None or not log_file.exists():
            self.notify("No run log to show yet.", timeout=3)
            return
        pager = os.environ.get("PAGER") or "less -R"
        try:
            with self.suspend():
                subprocess.call([*shlex.split(pager), str(log_file)])
        except Exception as exc:  # SuspendNotSupported, missing pager binary, ...
            self.notify(f"Could not start pager: {exc}", severity="error")

    # Viewing and editing

    def action_next_view(self) -> None:
        # Focus follows the tab when it was in the pane, else the old tab's widget hides with focus.
        follow = self.detail.has_focus_within
        self.detail.cycle_tab()
        if follow:
            self.call_after_refresh(self.detail.focus_active_tab)

    def action_save(self) -> None:
        saved = self.detail.save()
        if saved is not None:
            self.notify(f"Saved {saved.name}", timeout=2)
            if saved.parent == self.browser.directory:
                self.browser.reload()

    def action_external_edit(self) -> None:
        entry = self.current_entry
        if entry is None or entry.is_dir:
            return
        if self.detail.dirty:
            self.notify("Save or discard the changes in the Edit tab first.", severity="warning")
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            with self.suspend():
                subprocess.call([*shlex.split(editor), str(entry.path)])
        except Exception as exc:  # SuspendNotSupported, missing editor binary, ...
            self.notify(f"Could not start editor: {exc}", severity="error")
            return
        self.browser.reload()
        refreshed = inspect_file(entry.path)
        self.current_entry = refreshed
        self.detail.show(refreshed, force_editor=True)

    # Quitting

    async def action_quit(self) -> None:
        reasons = []
        if self.detail.dirty:
            reasons.append("unsaved changes in the editor")
        running = self.jobs.running_jobs()
        if running:
            reasons.append(f"{len(running)} running job(s) that will be stopped")
        if not reasons:
            self.exit()
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.exit()

        self.push_screen(ConfirmDialog("Quit with " + " and ".join(reasons) + "?", "Quit"), confirmed)

    def on_unmount(self) -> None:
        # Widgets are already gone here, so use the app's own job list.
        for job in self._all_jobs:
            job.cancel()
