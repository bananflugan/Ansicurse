"""Playbook detection, directory scanning and outline parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

YAML_SUFFIXES = frozenset({".yml", ".yaml"})
MAX_PARSE_BYTES = 1_000_000
VAULT_HEADER = "$ANSIBLE_VAULT;"
IMPORT_PLAYBOOK_KEYS = ("import_playbook", "ansible.builtin.import_playbook")
INCLUDE_KEYS = frozenset(
    {
        "include_tasks",
        "import_tasks",
        "include_role",
        "import_role",
        "ansible.builtin.include_tasks",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    }
)
PLAY_SECTIONS = ("pre_tasks", "roles", "tasks", "post_tasks", "handlers")

# Keys that can appear on a task without being the module it calls.
TASK_KEYWORDS = frozenset(
    {
        "name", "when", "tags", "register", "notify", "listen", "vars", "args",
        "become", "become_user", "become_method", "become_flags", "become_exe",
        "loop", "loop_control", "until", "retries", "delay",
        "changed_when", "failed_when", "ignore_errors", "ignore_unreachable",
        "delegate_to", "delegate_facts", "run_once", "local_action",
        "environment", "no_log", "check_mode", "diff", "any_errors_fatal",
        "async", "poll", "throttle", "timeout", "connection", "remote_user",
        "module_defaults", "collections", "debugger", "port",
        "block", "rescue", "always",
    }
)


class EntryKind(str, Enum):
    PARENT = "parent"
    DIR = "dir"
    PLAYBOOK = "playbook"
    VAULT = "vault"
    YAML = "yaml"
    FILE = "file"


@dataclass
class OutlineNode:
    label: str
    kind: str
    tags: list[str] = field(default_factory=list)
    detail: str = ""
    children: list[OutlineNode] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class PlaybookInfo:
    plays: list[OutlineNode]
    uses_become: bool = False
    """A play, block or task in this file sets `become` (roles are not inspected)."""

    @property
    def play_count(self) -> int:
        return len(self.plays)

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for play in self.plays:
            for node in play.walk():
                tags.update(node.tags)
        return sorted(tags)


@dataclass
class Entry:
    path: Path
    kind: EntryKind
    playbook: PlaybookInfo | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return ".." if self.kind is EntryKind.PARENT else self.path.name

    @property
    def is_dir(self) -> bool:
        return self.kind in (EntryKind.PARENT, EntryKind.DIR)


class _Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # type: ignore[misc]
    """Safe loader that tolerates Ansible's custom tags (!vault, !unsafe, ...)."""


def _construct_tagged(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return "<vault>" if tag_suffix == "vault" else loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)  # type: ignore[arg-type]


_Loader.add_multi_constructor("!", _construct_tagged)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader


def is_playbook_data(data: Any) -> bool:
    """A playbook is a non-empty list where every item is a play or an import."""
    if not isinstance(data, list) or not data:
        return False
    return all(
        isinstance(item, dict)
        and ("hosts" in item or any(key in item for key in IMPORT_PLAYBOOK_KEYS))
        for item in data
    )


def parse_playbook(data: list[dict[str, Any]]) -> PlaybookInfo:
    return PlaybookInfo(
        [_parse_play(play, index) for index, play in enumerate(data, 1)],
        uses_become=_sets_become(data),
    )


def _sets_become(value: Any) -> bool:
    """True if any mapping in `value` sets `become` to something other than false."""
    if isinstance(value, dict):
        become = value.get("become")
        if become is not None and str(become).strip().lower() not in ("false", "no", "0", "off"):
            return True
        return any(_sets_become(v) for v in value.values())
    if isinstance(value, list):
        return any(_sets_become(v) for v in value)
    return False


def inspect_file(path: Path) -> Entry:
    """Classify a single file, parsing it if it looks like YAML."""
    if path.suffix.lower() not in YAML_SUFFIXES:
        return Entry(path, EntryKind.FILE)
    try:
        if path.stat().st_size > MAX_PARSE_BYTES:
            return Entry(path, EntryKind.YAML, error="file too large to inspect")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Entry(path, EntryKind.YAML, error=str(exc))
    if text.startswith(VAULT_HEADER):
        return Entry(path, EntryKind.VAULT)
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        return Entry(path, EntryKind.YAML, error=f"YAML error: {exc}")
    if is_playbook_data(data):
        return Entry(path, EntryKind.PLAYBOOK, playbook=parse_playbook(data))
    return Entry(path, EntryKind.YAML)


_FILE_ORDER = {EntryKind.PLAYBOOK: 0, EntryKind.VAULT: 1, EntryKind.YAML: 2, EntryKind.FILE: 3}


def scan_directory(
    directory: Path, *, show_hidden: bool = False, playbooks_only: bool = False
) -> list[Entry]:
    """List a directory: parent link first, then folders, then playbooks and other files."""
    entries: list[Entry] = []
    if directory.parent != directory:
        entries.append(Entry(directory.parent, EntryKind.PARENT))
    try:
        children = list(directory.iterdir())
    except OSError:
        return entries

    dirs: list[Entry] = []
    files: list[Entry] = []
    for child in children:
        if not show_hidden and child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                dirs.append(Entry(child, EntryKind.DIR))
                continue
        except OSError:
            continue
        entry = inspect_file(child)
        if playbooks_only and entry.kind is not EntryKind.PLAYBOOK:
            continue
        files.append(entry)

    dirs.sort(key=lambda e: e.name.lower())
    files.sort(key=lambda e: (_FILE_ORDER[e.kind], e.name.lower()))
    return entries + dirs + files


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(",") if tag.strip()]
    if isinstance(value, list):
        return [str(tag) for tag in value]
    return []


def _short(value: Any, width: int = 50) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _parse_play(play: dict[str, Any], index: int) -> OutlineNode:
    for key in IMPORT_PLAYBOOK_KEYS:
        if key in play:
            return OutlineNode(
                label=f"import_playbook: {play[key]}", kind="import", tags=_tags(play.get("tags"))
            )

    hosts = play.get("hosts")
    if isinstance(hosts, list):
        hosts = ",".join(str(h) for h in hosts)
    node = OutlineNode(
        label=str(play.get("name") or f"Play {index}"),
        kind="play",
        tags=_tags(play.get("tags")),
        detail=f"hosts: {hosts}",
    )
    for section in PLAY_SECTIONS:
        items = play.get(section)
        if not isinstance(items, list) or not items:
            continue
        parse = _parse_role if section == "roles" else _parse_task
        node.children.append(
            OutlineNode(label=section, kind="section", children=[parse(item) for item in items])
        )
    return node


def _parse_role(role: Any) -> OutlineNode:
    if isinstance(role, dict):
        name = role.get("role") or role.get("name") or "?"
        return OutlineNode(label=str(name), kind="role", tags=_tags(role.get("tags")))
    return OutlineNode(label=str(role), kind="role")


def _module_name(task: dict[str, Any]) -> str | None:
    for key in task:
        if key not in TASK_KEYWORDS and not str(key).startswith("with_"):
            return str(key)
    return None


def _parse_task(task: Any) -> OutlineNode:
    if not isinstance(task, dict):
        return OutlineNode(label=_short(task), kind="task")

    name = task.get("name")
    tags = _tags(task.get("tags"))

    if "block" in task:
        node = OutlineNode(label=str(name or "block"), kind="block", tags=tags)
        node.children = [_parse_task(t) for t in task.get("block") or []]
        for section in ("rescue", "always"):
            if task.get(section):
                node.children.append(
                    OutlineNode(
                        label=section,
                        kind="section",
                        children=[_parse_task(t) for t in task[section]],
                    )
                )
        return node

    module = _module_name(task)
    args = task.get(module) if module else None
    if module in INCLUDE_KEYS:
        target = args.get("name") or args.get("file") if isinstance(args, dict) else args
        detail = f"{module.rsplit('.', 1)[-1]} → {target}"
        return OutlineNode(label=str(name or detail), kind="include", tags=tags, detail=detail)

    if name:
        return OutlineNode(label=str(name), kind="task", tags=tags, detail=module or "")
    label = f"{module}: {_short(args)}" if module and isinstance(args, str) else (module or "task")
    return OutlineNode(label=label, kind="task", tags=tags)
