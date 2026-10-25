"""Modal dialogs: run options and confirmations."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from ..playbooks import Entry
from ..project import ProjectInfo
from ..runner import RunOptions, display_command, looks_like_production

VERBOSITY = [("normal", 0), ("-v", 1), ("-vv", 2), ("-vvv", 3), ("-vvvv", 4)]
TEXT_FIELDS = (
    ("inventory", "Inventory", "-i  path or host list"),
    ("limit", "Limit", "--limit  hosts or groups"),
    ("tags", "Tags", "--tags  comma separated"),
    ("skip_tags", "Skip tags", "--skip-tags  comma separated"),
    ("extra_vars", "Extra vars", "-e  key=value …, JSON or @file"),
)


class RunDialog(ModalScreen[RunOptions | None]):
    """Collects ansible-playbook options and shows the resulting command."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+r", "run", "Run"),
    ]

    def __init__(self, entry: Entry, options: RunOptions, project: ProjectInfo) -> None:
        super().__init__()
        self.entry = entry
        self.initial = options
        self.project = project

    def compose(self) -> ComposeResult:
        # Scrolls when the terminal is too short to show the whole form. The buttons are
        # docked to the bottom (see app.tcss), so they stay visible while the form scrolls.
        with VerticalScroll(id="dialog"):
            yield Label(Text.assemble("Run ", (self.entry.name, "bold green")), classes="title")
            yield Static(self._context_text(), classes="context")
            for name, label, placeholder in TEXT_FIELDS:
                with Horizontal(classes="row"):
                    yield Label(label, classes="field-label")
                    yield Input(
                        value=getattr(self.initial, name),
                        placeholder=self._placeholder(name, placeholder),
                        id=name,
                    )
            with Horizontal(classes="row"):
                yield Label("Verbosity", classes="field-label")
                yield Select(VERBOSITY, value=self.initial.verbosity, allow_blank=False, id="verbosity")
            with Horizontal(classes="row checks"):
                yield Checkbox("Check mode (--check)", self.initial.check, id="check")
                yield Checkbox("Diff (--diff)", self.initial.diff, id="diff")
                yield Checkbox("Become (--become)", self.initial.become, id="become")
            with Horizontal(classes="row"):
                yield Checkbox(
                    "Ask become password (-K)", self.initial.ask_become_pass, id="ask_become_pass"
                )
                yield Checkbox(
                    "Ask vault password (-J)", self.initial.ask_vault_pass, id="ask_vault_pass"
                )
            yield Static(self._password_hint(), id="password-hint")
            yield Static(id="cmd-preview")
            yield Static(id="prod-warning")
            with Horizontal(id="buttons"):
                yield Button("Run", variant="success", id="run")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._refresh_preview()
        self.query_one("#limit", Input).focus()

    def _placeholder(self, name: str, default: str) -> str:
        if name == "inventory" and self.project.config_inventory:
            return f"from ansible.cfg: {self.project.config_inventory}"
        return default

    def _context_text(self) -> Text:
        home = str(Path.home())
        text = Text("runs in ", style="dim")
        text.append(str(self.project.workdir).replace(home, "~", 1))
        if self.project.config:
            text.append("  ·  ansible.cfg ✔", style="dim")
        if self.entry.playbook and (tags := self.entry.playbook.all_tags()):
            text.append("\ntags: ", style="dim")
            text.append(", ".join(tags), style="yellow")
        return text

    def _password_hint(self) -> Text:
        text = Text(style="dim")
        for kind, path in (
            ("become", self.project.become_password_file),
            ("vault", self.project.vault_password_file),
        ):
            if path:
                text.append(f"{kind} password: Ansible reads it from {path}\n")
        text.append("Passwords you type are kept in memory only, until you quit or press F9.")
        return text

    def options(self) -> RunOptions:
        values = {name: self.query_one(f"#{name}", Input).value.strip() for name, _, _ in TEXT_FIELDS}
        verbosity = self.query_one("#verbosity", Select).value
        return RunOptions(
            **values,
            check=self.query_one("#check", Checkbox).value,
            diff=self.query_one("#diff", Checkbox).value,
            become=self.query_one("#become", Checkbox).value,
            verbosity=verbosity if isinstance(verbosity, int) else 0,
            ask_become_pass=self.query_one("#ask_become_pass", Checkbox).value,
            ask_vault_pass=self.query_one("#ask_vault_pass", Checkbox).value,
        )

    def _refresh_preview(self) -> None:
        options = self.options()
        command = display_command(self.entry.path, options, self.project.workdir)
        self.query_one("#cmd-preview", Static).update(Text.assemble(("$ ", "dim"), (command, "bold")))
        warning = self.query_one("#prod-warning", Static)
        if looks_like_production(options):
            warning.update("⚠  This looks like a production target. You will be asked to confirm.")
            warning.display = True
        else:
            warning.display = False

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    def _changed(self) -> None:
        self._refresh_preview()

    @on(Checkbox.Changed, "#become")
    def _become_changed(self, event: Checkbox.Changed) -> None:
        # Become almost always needs a sudo password; ask for one unless Ansible reads it
        # from a file. Unticking Become leaves the ask alone (the playbook may still set it).
        if event.value and not self.project.become_password_file:
            self.query_one("#ask_become_pass", Checkbox).value = True

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#run")
    def action_run(self) -> None:
        self.dismiss(self.options())

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PasswordDialog(ModalScreen[str | None]):
    """Asks for a become or vault password. The value is returned, never stored here."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    TEXTS = {
        "become": (
            "Become password",
            "The sudo/become password for the managed hosts, as ansible-playbook -K asks for it.",
        ),
        "vault": ("Vault password", "Decrypts ansible-vault data, as ansible-playbook -J asks for it."),
    }

    def __init__(self, kind: str, workdir: Path) -> None:
        super().__init__()
        self.kind = kind
        self.workdir = workdir

    def compose(self) -> ComposeResult:
        title, explanation = self.TEXTS[self.kind]
        home = str(Path.home())
        with Vertical(id="dialog"):
            yield Label(Text(title, style="bold"), classes="title")
            yield Static(
                Text.assemble(
                    explanation,
                    "\n",
                    ("For runs in ", "dim"),
                    (str(self.workdir).replace(home, "~", 1), "dim"),
                    (". Kept in memory until you quit or press F9; never written to disk.", "dim"),
                ),
                classes="context",
            )
            yield Input(password=True, placeholder=title, id="password")
            with Horizontal(id="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#password", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        self.dismiss(self.query_one("#password", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape", "cancel", "No"),
    ]

    def __init__(self, message: str, confirm_label: str = "Yes", danger: bool = True) -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.message, classes="message")
            with Horizontal(id="buttons"):
                yield Button(self.confirm_label, variant="error" if self.danger else "primary", id="yes")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def action_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_cancel(self) -> None:
        self.dismiss(False)
