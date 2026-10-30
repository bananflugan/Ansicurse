"""Command line entry point."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from . import __version__


def version_text() -> str:
    """The version, plus the branch and commit when installed from GitHub (pipx install git+…)."""
    try:
        # pip records where a package came from, including the exact git commit.
        direct_url = json.loads(distribution("ansicurse").read_text("direct_url.json") or "{}")
    except (PackageNotFoundError, OSError, ValueError):
        direct_url = {}
    vcs_info = direct_url.get("vcs_info") or {}
    commit = vcs_info.get("commit_id")
    if not commit:
        return f"ansicurse {__version__}"
    # The @branch from the install URL. Without one, pip installed the repo's default branch.
    revision = vcs_info.get("requested_revision")
    if revision and not commit.startswith(revision):  # @<commit> needs no repeating
        return f"ansicurse {__version__} ({revision} {commit[:7]})"
    return f"ansicurse {__version__} ({commit[:7]})"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ansicurse",
        description="Browse, edit and run Ansible playbooks from the terminal.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="directory to start in (default: current directory)",
    )
    parser.add_argument("--version", action="version", version=version_text())
    args = parser.parse_args(argv)

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        parser.error(f"not a directory: {directory}")

    # Imported late so --help and --version work without Textual installed.
    from .app import AnsicurseApp

    AnsicurseApp(directory).run()


if __name__ == "__main__":
    main()
