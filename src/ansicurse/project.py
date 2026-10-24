"""Find ansible.cfg, inventories and the directory to run ansible-playbook from."""

from __future__ import annotations

import configparser
import itertools
import os
from dataclasses import dataclass
from pathlib import Path

INVENTORY_CANDIDATES = (
    "inventory",
    "inventory.ini",
    "inventory.yml",
    "inventory.yaml",
    "inventories",
    "hosts",
    "hosts.ini",
    "hosts.yml",
    "hosts.yaml",
)


SYSTEM_CONFIG = Path("/etc/ansible/ansible.cfg")
VARS_DIRS = ("group_vars", "host_vars", "vars")
MAX_VAULT_SCAN_FILES = 300
MAX_VAULT_SCAN_BYTES = 1_000_000


@dataclass
class ProjectInfo:
    workdir: Path
    """Directory ansible-playbook runs in (where ansible.cfg is picked up)."""
    config: Path | None
    config_inventory: str | None
    """Inventory set in ansible.cfg; ansible uses it when no -i is given."""
    inventory: str | None
    """Inventory file found next to the playbook, relative to workdir."""
    become_password_file: str | None = None
    """Become password file from the environment or ansible.cfg; ansible reads it itself."""
    vault_password_file: str | None = None
    """Vault password file or vault identity list from the environment or ansible.cfg."""


def find_config_dir(start: Path) -> Path | None:
    """Nearest directory at or above `start` that contains ansible.cfg."""
    for directory in (start, *start.parents):
        if (directory / "ansible.cfg").is_file():
            return directory
    return None


def effective_config(workdir: Path) -> Path | None:
    """The config file ansible-playbook will use when run from `workdir`.

    Same order as Ansible: $ANSIBLE_CONFIG, ./ansible.cfg, ~/.ansible.cfg,
    /etc/ansible/ansible.cfg. Only the first one found is used.
    """
    env_config = os.environ.get("ANSIBLE_CONFIG")
    candidates = [
        Path(env_config).expanduser() if env_config else None,
        workdir / "ansible.cfg",
        Path.home() / ".ansible.cfg",
        SYSTEM_CONFIG,
    ]
    return next((c for c in candidates if c is not None and c.is_file()), None)


def read_config_defaults(config: Path) -> dict[str, str]:
    """The [defaults] section of an ansible.cfg, or {} if it can't be read."""
    parser = configparser.ConfigParser(
        interpolation=None, strict=False, inline_comment_prefixes=("#", ";")
    )
    try:
        parser.read(config, encoding="utf-8")
    except (configparser.Error, OSError):
        return {}
    if not parser.has_section("defaults"):
        return {}
    return {key: value.strip() for key, value in parser.items("defaults") if value.strip()}


def read_config_inventory(config: Path) -> str | None:
    return read_config_defaults(config).get("inventory")


def uses_vault(workdir: Path, playbook: Path) -> bool:
    """Whether the playbook or the vars files around it contain vault-encrypted data."""
    from .playbooks import VAULT_HEADER, YAML_SUFFIXES

    def candidates():
        yield playbook
        for base in dict.fromkeys((playbook.parent, workdir)):
            for name in VARS_DIRS:
                vars_dir = base / name
                if vars_dir.is_dir():
                    yield from (p for p in vars_dir.rglob("*") if p.is_file())

    for path in itertools.islice(candidates(), MAX_VAULT_SCAN_FILES):
        # Vars files are YAML, with or without an extension (group_vars/all).
        if path.suffix and path.suffix.lower() not in YAML_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_VAULT_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith(VAULT_HEADER) or "!vault" in text:
            return True
    return False


def resolve_workdir(directory: Path, launch_dir: Path) -> Path:
    """Run from the ansible.cfg directory, else the launch directory, else `directory`."""
    config_dir = find_config_dir(directory)
    if config_dir is not None:
        return config_dir
    if directory.is_relative_to(launch_dir):
        return launch_dir
    return directory


def project_info(directory: Path, launch_dir: Path) -> ProjectInfo:
    workdir = resolve_workdir(directory, launch_dir)
    config = effective_config(workdir)
    defaults = read_config_defaults(config) if config else {}

    config_inventory = defaults.get("inventory")
    inventory = None
    if not config_inventory:
        for base in dict.fromkeys((workdir, directory)):
            match = next((base / n for n in INVENTORY_CANDIDATES if (base / n).exists()), None)
            if match is not None:
                inventory = os.path.relpath(match, workdir)
                break

    # Environment variables override ansible.cfg, as in Ansible itself.
    become_password_file = os.environ.get("ANSIBLE_BECOME_PASSWORD_FILE") or defaults.get(
        "become_password_file"
    )
    vault_password_file = (
        os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE")
        or os.environ.get("ANSIBLE_VAULT_IDENTITY_LIST")
        or defaults.get("vault_password_file")
        or defaults.get("vault_identity_list")
    )
    return ProjectInfo(
        workdir, config, config_inventory, inventory, become_password_file, vault_password_file
    )
