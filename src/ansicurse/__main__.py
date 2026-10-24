"""Command line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__


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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        parser.error(f"not a directory: {directory}")

    # Imported late so --help and --version work without Textual installed.
    from .app import AnsicurseApp

    AnsicurseApp(directory).run()


if __name__ == "__main__":
    main()
