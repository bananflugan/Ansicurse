"""Directory browser that highlights Ansible playbooks."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from ..playbooks import Entry, EntryKind, scan_directory


def entry_label(entry: Entry) -> Text:
    if entry.kind is EntryKind.PARENT:
        return Text("↰ ..", style="bold")
    if entry.kind is EntryKind.DIR:
        return Text.assemble(("▸ ", "cyan"), (f"{entry.name}/", "bold"))
    if entry.kind is EntryKind.PLAYBOOK:
        assert entry.playbook is not None
        plays = entry.playbook.play_count
        label = Text.assemble(("◆ ", "bold green"), (entry.name, "bold"))
        label.append(f"  {plays} play{'s' if plays != 1 else ''}", style="dim")
        return label
    if entry.kind is EntryKind.VAULT:
        return Text.assemble(f"  {entry.name}", ("  vault", "magenta"))
    if entry.kind is EntryKind.YAML:
        label = Text(f"  {entry.name}")
        if entry.error:
            label.append("  !", style="bold red")
        return label
    return Text(f"  {entry.name}", style="dim")


class Browser(Vertical):
    """Folder listing. Playbooks sort to the top and are marked with ◆."""

    BINDINGS = [
        Binding("right,l", "open", "Open", show=False),
        Binding("left,h,backspace", "parent", "Up", show=False),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("full_stop", "toggle_hidden", "Hidden", show=False),
        Binding("p", "toggle_playbooks_only", "Playbooks only"),
        Binding("ctrl+r", "reload", "Reload", show=False),
        Binding("r", "app.run", "Run", show=False),
        Binding("c", "app.check", "Check", show=False),
        Binding("e", "app.external_edit", "Edit", show=False),
        Binding("q", "app.quit", "Quit", show=False),
    ]

    class DirectoryChanged(Message):
        def __init__(self, directory: Path) -> None:
            super().__init__()
            self.directory = directory

    class Highlighted(Message):
        def __init__(self, entry: Entry) -> None:
            super().__init__()
            self.entry = entry

    class Activated(Message):
        """Enter pressed on a file."""

        def __init__(self, entry: Entry) -> None:
            super().__init__()
            self.entry = entry

    def __init__(self, directory: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.directory = directory
        self.show_hidden = False
        self.playbooks_only = False
        self._entries: list[Entry] = []
        self._visible: list[Entry] = []

    def compose(self) -> ComposeResult:
        yield OptionList(id="files")
        yield Input(placeholder="filter…", id="filter")

    def on_mount(self) -> None:
        self.load(self.directory)
        self.query_one("#files", OptionList).focus()

    @property
    def files(self) -> OptionList:
        return self.query_one("#files", OptionList)

    @property
    def current(self) -> Entry | None:
        index = self.files.highlighted
        if index is None or not 0 <= index < len(self._visible):
            return None
        return self._visible[index]

    def load(self, directory: Path, select: Path | None = None) -> None:
        """Show `directory`, highlighting `select` (or the first real entry)."""
        changed = directory != self.directory
        self.directory = directory
        self._entries = scan_directory(
            directory, show_hidden=self.show_hidden, playbooks_only=self.playbooks_only
        )
        filter_input = self.query_one("#filter", Input)
        if changed and filter_input.value:
            filter_input.value = ""
            filter_input.remove_class("visible")
        self._render_entries(select)
        self.border_title = str(directory).replace(str(Path.home()), "~", 1)
        self.post_message(self.DirectoryChanged(directory))

    def reload(self) -> None:
        current = self.current
        self.load(self.directory, select=current.path if current else None)

    def _render_entries(self, select: Path | None = None) -> None:
        needle = self.query_one("#filter", Input).value.strip().lower()
        self._visible = [
            e for e in self._entries if e.kind is EntryKind.PARENT or needle in e.name.lower()
        ]
        files = self.files
        files.clear_options()
        files.add_options([Option(entry_label(e)) for e in self._visible])
        if not self._visible:
            return
        index = next((i for i, e in enumerate(self._visible) if e.path == select), None)
        if index is None:
            first_real = 1 if self._visible[0].kind is EntryKind.PARENT else 0
            index = min(first_real, len(self._visible) - 1)
        files.highlighted = index
        # Setting the same index again does not fire OptionHighlighted, so announce it ourselves.
        self.post_message(self.Highlighted(self._visible[index]))

    @on(OptionList.OptionHighlighted, "#files")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._visible):
            self.post_message(self.Highlighted(self._visible[event.option_index]))

    @on(OptionList.OptionSelected, "#files")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_open()

    def action_open(self) -> None:
        entry = self.current
        if entry is None:
            return
        if entry.kind is EntryKind.PARENT:
            self.action_parent()
        elif entry.is_dir:
            self.load(entry.path)
        else:
            self.post_message(self.Activated(entry))

    def action_parent(self) -> None:
        if self.directory.parent != self.directory:
            self.load(self.directory.parent, select=self.directory)

    def action_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        filter_input.add_class("visible")
        filter_input.focus()

    def action_clear_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        if filter_input.value or filter_input.has_class("visible"):
            filter_input.value = ""
            filter_input.remove_class("visible")
            self._render_entries(self.current.path if self.current else None)
            self.files.focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._render_entries()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.files.focus()

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.notify(f"Hidden files {'shown' if self.show_hidden else 'hidden'}", timeout=2)
        self.reload()

    def action_toggle_playbooks_only(self) -> None:
        self.playbooks_only = not self.playbooks_only
        self.notify("Showing playbooks only" if self.playbooks_only else "Showing all files", timeout=2)
        self.reload()

    def action_reload(self) -> None:
        self.reload()
