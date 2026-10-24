"""Building ansible-playbook commands and running them in the background via ansible-runner."""

from __future__ import annotations

import itertools
import os
import re
import shlex
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import state

PRODUCTION_PATTERN = re.compile(r"prod", re.IGNORECASE)

HOST_EVENTS = {
    "runner_on_ok": "ok",
    "runner_on_failed": "failed",
    "runner_on_unreachable": "unreachable",
    "runner_on_skipped": "skipped",
}
# Higher wins when a host reports several results during a run.
HOST_SEVERITY = {"skipped": 0, "ok": 1, "changed": 2, "failed": 3, "unreachable": 4}
FINAL_STATUSES = frozenset({"successful", "failed", "canceled", "timeout", "error"})

# Ansible's interactive prompts, answered by ansible-runner so passwords never touch disk.
# The become prompt is "BECOME password: ", or e.g. "SUDO password: " when
# agnostic_become_prompt is turned off.
PASSWORD_PROMPTS = {
    "become": r"^[A-Z_]+ password(\[defaults to SSH password\])?:\s*?$",
    "vault": r"^Vault password:\s*?$",
}
# Output that means a password we supplied was wrong (matched case-insensitively).
PASSWORD_ERRORS = {
    "become": ("incorrect sudo password", "incorrect su password", "incorrect become password"),
    "vault": ("decryption failed",),
}


@dataclass
class RunOptions:
    inventory: str = ""
    limit: str = ""
    tags: str = ""
    skip_tags: str = ""
    extra_vars: str = ""
    check: bool = False
    diff: bool = False
    become: bool = False
    verbosity: int = 0
    syntax_check: bool = False
    ask_become_pass: bool = False
    ask_vault_pass: bool = False

    @property
    def password_kinds(self) -> list[str]:
        """Which passwords ansible-playbook will prompt for with these options."""
        return [
            kind
            for kind, wanted in (("become", self.ask_become_pass), ("vault", self.ask_vault_pass))
            if wanted
        ]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunOptions:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_args(options: RunOptions) -> list[str]:
    """ansible-playbook arguments for `options`, excluding the playbook itself."""
    args: list[str] = []
    for flag, value in (
        ("-i", options.inventory),
        ("--limit", options.limit),
        ("--tags", options.tags),
        ("--skip-tags", options.skip_tags),
        ("-e", options.extra_vars),
    ):
        if value.strip():
            args += [flag, value.strip()]
    if options.check:
        args.append("--check")
    if options.diff:
        args.append("--diff")
    if options.become:
        args.append("--become")
    if options.ask_become_pass:
        args.append("--ask-become-pass")
    if options.ask_vault_pass:
        args.append("--ask-vault-pass")
    if options.verbosity > 0:
        args.append("-" + "v" * min(options.verbosity, 6))
    if options.syntax_check:
        args.append("--syntax-check")
    return args


def display_command(playbook: Path, options: RunOptions, workdir: Path) -> str:
    """The equivalent shell command, as a user would type it from `workdir`."""
    return shlex.join(["ansible-playbook", *build_args(options), os.path.relpath(playbook, workdir)])


def looks_like_production(options: RunOptions) -> bool:
    return bool(PRODUCTION_PATTERN.search(f"{options.inventory} {options.limit}"))


def find_ansible_playbook() -> str | None:
    """ansible-playbook on PATH, else the one installed next to this Python."""
    found = shutil.which("ansible-playbook")
    if found:
        return found
    sibling = Path(sys.executable).parent / "ansible-playbook"
    return str(sibling) if sibling.exists() else None


OutputCallback = Callable[["Job", str], None]
StatusCallback = Callable[["Job"], None]


class Job:
    """One ansible-playbook run, executed on a background thread by ansible-runner."""

    _ids = itertools.count(1)

    def __init__(
        self,
        playbook: Path,
        options: RunOptions,
        workdir: Path,
        passwords: dict[str, str] | None = None,
    ) -> None:
        self.id = next(self._ids)
        self.playbook = playbook
        self.options = options
        self.workdir = workdir
        self.command = display_command(playbook, options, workdir)
        self.status = "pending"
        self.rc: int | None = None
        self.hosts: dict[str, str] = {}
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.artifact_dir: Path | None = None
        self.password_errors: set[str] = set()
        """Password kinds ("become", "vault") that Ansible rejected during this run."""
        # Kept off the public attributes so passwords don't end up in reprs or logs.
        self._passwords = dict(passwords or {})
        self._cancel = threading.Event()

    @property
    def title(self) -> str:
        kind = "check" if self.options.syntax_check else "run"
        return f"#{self.id} {kind} {self.playbook.name}"

    @property
    def done(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def cancel(self) -> None:
        self._cancel.set()

    def handle_event(self, event: dict[str, Any]) -> str:
        """Update host states from an ansible-runner event; return its stdout."""
        name = event.get("event")
        data = event.get("event_data") or {}

        state_name = HOST_EVENTS.get(name or "")
        host = data.get("host")
        if host and state_name:
            if state_name == "ok" and (data.get("res") or {}).get("changed"):
                state_name = "changed"
            if state_name == "failed" and data.get("ignore_errors"):
                state_name = "ok"
            self._set_host(host, state_name)
        elif name == "playbook_on_stats":
            # The recap is authoritative: a failure that was rescued or ignored is not a failure.
            stats = {key: data.get(key) or {} for key in ("ok", "changed", "failures", "dark", "skipped")}
            for stat_host in set().union(*stats.values()):
                if stats["dark"].get(stat_host):
                    self.hosts[stat_host] = "unreachable"
                elif stats["failures"].get(stat_host):
                    self.hosts[stat_host] = "failed"
                elif stats["changed"].get(stat_host):
                    self.hosts[stat_host] = "changed"
                elif stats["ok"].get(stat_host):
                    self.hosts[stat_host] = "ok"
                else:
                    self.hosts[stat_host] = "skipped"

        stdout = event.get("stdout") or ""
        if self._passwords and stdout:
            lowered = stdout.lower()
            for kind in self._passwords:
                if any(marker in lowered for marker in PASSWORD_ERRORS[kind]):
                    self.password_errors.add(kind)
        return stdout

    def _set_host(self, host: str, state_name: str) -> None:
        current = self.hosts.get(host)
        if current is None or HOST_SEVERITY[state_name] > HOST_SEVERITY[current]:
            self.hosts[host] = state_name

    def _finish(self, status: str, rc: int | None = None) -> None:
        self.status = status
        self.rc = rc
        self.finished_at = datetime.now()

    def start(self, on_output: OutputCallback, on_status: StatusCallback) -> None:
        """Start the run on a background thread. Callbacks are invoked from that thread."""
        self.started_at = datetime.now()
        binary = find_ansible_playbook()
        if binary is None:
            self._finish("error")
            on_output(self, "ansible-playbook not found. Install Ansible or `pip install ansicurse[ansible]`.\n")
            on_status(self)
            return
        try:
            import ansible_runner
        except ImportError as exc:
            self._finish("error")
            on_output(self, f"Could not import ansible_runner: {exc}\n")
            on_status(self)
            return

        stamp = self.started_at.strftime("%Y%m%d-%H%M%S")
        private_dir = state.runs_dir() / f"{stamp}-{self.id}-{self.playbook.stem}"
        # ansible-runner records the full process environment in artifacts/<ident>/command,
        # which can include tokens, so keep run folders private to the user.
        state.runs_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        private_dir.mkdir(mode=0o700, exist_ok=True)
        self.artifact_dir = private_dir / "artifacts"

        def event_handler(event: dict[str, Any]) -> bool:
            text = self.handle_event(event)
            if text:
                on_output(self, text + "\n")
            return True

        def status_handler(data: dict[str, Any], runner_config: Any = None) -> None:
            status = data.get("status")
            if status and not self.done:
                if status in FINAL_STATUSES:
                    self._finish(status)
                else:
                    self.status = status
                on_status(self)

        def finished_callback(runner: Any) -> None:
            self._finish(getattr(runner, "status", None) or self.status, getattr(runner, "rc", None))
            on_status(self)

        try:
            thread, runner = ansible_runner.run_async(
                private_data_dir=str(private_dir),
                project_dir=str(self.workdir),
                playbook=str(self.playbook),
                cmdline=shlex.join(build_args(self.options)),
                # Passing binary= would switch ansible-runner to raw mode and drop the playbook
                # argument, so put the ansible-playbook we found first on PATH instead.
                envvars={"PATH": os.pathsep.join([str(Path(binary).parent), os.environ.get("PATH", "")])},
                suppress_env_files=True,
                # Held in memory only: ansible-runner types these into Ansible's prompts.
                passwords={PASSWORD_PROMPTS[kind]: value for kind, value in self._passwords.items()},
                ident="run",
                quiet=True,
                event_handler=event_handler,
                status_handler=status_handler,
                cancel_callback=self._cancel.is_set,
                finished_callback=finished_callback,
            )
        except Exception as exc:  # ansible-runner raises ConfigurationError and friends
            self._finish("error")
            on_output(self, f"Failed to start ansible-runner: {exc}\n")
            on_status(self)
            return

        def watch() -> None:
            # If the runner thread dies without reporting a final status, don't leave the job hanging.
            thread.join()
            if not self.done:
                status = getattr(runner, "status", None)
                self._finish(status if status in FINAL_STATUSES else "error", getattr(runner, "rc", None))
                on_status(self)

        threading.Thread(target=watch, name=f"ansicurse-watch-{self.id}", daemon=True).start()
