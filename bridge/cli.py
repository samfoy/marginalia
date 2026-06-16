"""marginalia CLI — start the bridge server."""
from __future__ import annotations
import argparse
import os
import sys


def _ensure_bridge_on_path() -> None:
    bridge_dir = os.path.dirname(os.path.abspath(__file__))
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)


def serve(argv: list[str] | None = None) -> None:
    """Start the marginalia bridge server."""
    parser = argparse.ArgumentParser(
        prog="marginalia serve",
        description="Start the marginalia bridge (KOReader ↔ LLM ↔ Obsidian).",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override MARGINALIA_PORT (default: 7731).",
    )
    args = parser.parse_args(argv)

    if args.port is not None:
        os.environ.setdefault("MARGINALIA_PORT", str(args.port))

    _ensure_bridge_on_path()
    # Change cwd to bridge/ so relative imports inside bridge modules resolve.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    from server import main  # noqa: E402 — bridge/ must be on path first
    main()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="marginalia",
        description="marginalia — AI reading companion for KOReader.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Start the bridge server.")
    args, rest = parser.parse_known_args(argv)

    if args.command == "serve":
        serve(rest)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
