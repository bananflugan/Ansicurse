"""Preview, outline and built-in editor for the highlighted file."""

from __future__ import annotations

from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane, TextArea, Tree

from ..playbooks import Entry, EntryKind, OutlineNode, inspect_file

MAX_PREVIEW_BYTES = 512_000
TABS = ("preview", "outline", "edit")


def read_text(path: Path) -> str | None:
    """File contents, or None for binary, unreadable or oversized files."""
    try:
        if path.stat().st_size > MAX_PREVIEW_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def outline_label(node: OutlineNode) -> Text:
    styles = {
        "play": "bold green",
        "section": "bold blue",
        "block": "bold",
        "role": "cyan",
        "include": "cyan",
        "import": "cyan",
    }
    label = Text(node.label, style=styles.get(node.kind, ""))
    if node.detail and node.detail != node.label:
        label.append(f"  {node.detail}", style="dim")
    if node.tags:
        label.append(f"  [{', '.join(node.tags)}]", style="yellow")
    return label


class DetailPane(Vertical):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.entry: Entry | None = None
        self.edit_path: Path | None = None
        self._saved_text = ""

    def compose(self) -> ComposeResult:
        yield Static(id="detail-title")
        with TabbedContent(initial="preview", id="detail-tabs"):
            with TabPane("Preview", id="preview"):
                with VerticalScroll(id="preview-scroll"):
                    yield Static(id="preview-body")
            with TabPane("Outline", id="outline"):
                yield Tree("outline", id="outline-tree")
            with TabPane("Edit", id="edit"):
                yield TextArea.code_editor("", id="editor", read_only=True)

    def on_mount(self) -> None:
        editor = self.query_one("#editor", TextArea)
        try:
            editor.language = "yaml"
        except Exception:  # syntax highlighting needs textual[syntax]; plain editing still works
            pass

    @property
    def dirty(self) -> bool:
        return self.edit_path is not None and self.query_one("#editor", TextArea).text != self._saved_text

    def show(self, entry: Entry, *, force_editor: bool = False) -> None:
        self.entry = entry
        text = None if entry.is_dir else read_text(entry.path)
        self._show_preview(entry, text)
        self._show_outline(entry)
        if force_editor or not self.dirty:
            self._load_editor(entry, text)
        self._update_title()

    def _show_preview(self, entry: Entry, text: str | None) -> None:
        body = self.query_one("#preview-body", Static)
        if entry.is_dir:
            body.update(Text(f"Folder {entry.path}\n\nPress Enter or → to open it.", style="dim"))
        elif entry.kind is EntryKind.VAULT:
            body.update(Text("Encrypted with ansible-vault.", style="magenta"))
        elif text is None:
            body.update(Text("Binary or very large file; no preview.", style="dim"))
        else:
            lexer = "yaml" if entry.kind in (EntryKind.PLAYBOOK, EntryKind.YAML) else None
            lexer = lexer or Syntax.guess_lexer(str(entry.path), text)
            syntax = Syntax(text, lexer, theme="ansi_dark", line_numbers=True, word_wrap=False)
            if entry.error:
                body.update(Text.assemble((entry.error + "\n\n", "bold red"), syntax.highlight(text)))
            else:
                body.update(syntax)
        self.query_one("#preview-scroll", VerticalScroll).scroll_home(animate=False)

    def _show_outline(self, entry: Entry) -> None:
        tree = self.query_one("#outline-tree", Tree)
        tree.clear()
        tree.show_root = True
        if entry.playbook is None:
            tree.root.set_label(Text("Outline is available for playbooks.", style="dim"))
            return
        tree.root.set_label(Text(entry.name, style="bold"))
        tags = entry.playbook.all_tags()
        if tags:
            tree.root.add_leaf(Text(f"tags: {', '.join(tags)}", style="yellow"))
        for play in entry.playbook.plays:
            self._add_outline(tree.root, play)
        tree.root.expand_all()

    def _add_outline(self, parent, node: OutlineNode) -> None:
        if node.children:
            branch = parent.add(outline_label(node))
            for child in node.children:
                self._add_outline(branch, child)
        else:
            parent.add_leaf(outline_label(node))

    def _load_editor(self, entry: Entry, text: str | None) -> None:
        editor = self.query_one("#editor", TextArea)
        if entry.is_dir or text is None or entry.kind is EntryKind.VAULT:
            self.edit_path = None
            self._saved_text = ""
            editor.load_text("")
            editor.read_only = True
            return
        self.edit_path = entry.path
        self._saved_text = text
        editor.load_text(text)
        editor.read_only = False

    def save(self) -> Path | None:
        if self.edit_path is None:
            return None
        text = self.query_one("#editor", TextArea).text
        try:
            self.edit_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.notify(f"Could not save: {exc}", severity="error")
            return None
        self._saved_text = text
        self._update_title()
        saved = self.edit_path
        if self.entry is not None and self.entry.path == saved:
            refreshed = inspect_file(saved)
            self.entry = refreshed
            self._show_preview(refreshed, text)
            self._show_outline(refreshed)
        return saved

    def cycle_tab(self) -> None:
        tabs = self.query_one("#detail-tabs", TabbedContent)
        tabs.active = TABS[(TABS.index(tabs.active) + 1) % len(TABS)] if tabs.active in TABS else TABS[0]

    def focus_editor(self) -> None:
        self.query_one("#detail-tabs", TabbedContent).active = "edit"
        self.query_one("#editor", TextArea).focus()

    @on(TextArea.Changed, "#editor")
    def _editor_changed(self, event: TextArea.Changed) -> None:
        self._update_title()

    def _update_title(self) -> None:
        title = self.query_one("#detail-title", Static)
        if self.entry is None:
            title.update("")
            return
        label = Text(self.entry.name, style="bold")
        kinds = {
            EntryKind.PLAYBOOK: ("  playbook", "green"),
            EntryKind.VAULT: ("  vault", "magenta"),
            EntryKind.DIR: ("  folder", "blue"),
        }
        if self.entry.kind in kinds:
            label.append(*kinds[self.entry.kind])
        if self.dirty and self.edit_path is not None:
            label.append(f"   ● editing {self.edit_path.name} (unsaved, Ctrl+S to save)", style="bold yellow")
        title.update(label)
