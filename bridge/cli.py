"""marginalia command-line interface."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / ".marginalia.env"


def _ensure_bridge_on_path() -> None:
    bridge_dir = os.path.dirname(os.path.abspath(__file__))
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)


def _load_config() -> None:
    """Load ~/.marginalia.env written by 'marginalia setup' (if it exists).
    Uses set -a semantics: every KEY=VALUE is exported to the environment,
    but existing env vars take precedence (so CLI overrides still work).
    """
    if not CONFIG_FILE.exists():
        return
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # Don't override values already in the environment
        if key and key not in os.environ:
            os.environ[key] = val


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

    _load_config()

    if args.port is not None:
        os.environ["MARGINALIA_PORT"] = str(args.port)

    _ensure_bridge_on_path()
    # Change cwd to bridge/ so relative imports inside bridge modules resolve.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    from server import main  # noqa: E402 — bridge/ must be on path first
    main()


def setup(argv: list[str] | None = None) -> None:
    """Run the interactive setup wizard."""
    _ensure_bridge_on_path()
    from setup_wizard import run
    run()


def translations(argv: list[str] | None = None) -> None:
    """Generate or refresh an EPUB's adjacent offline translation sidecar."""
    parser = argparse.ArgumentParser(
        prog="marginalia translations",
        description="Generate an offline English translation sidecar for an EPUB.",
    )
    parser.add_argument("epub", type=Path, help="Path to the source EPUB.")
    parser.add_argument(
        "--batch-size", type=int, default=20,
        help="Candidates per model request (default: 20; maximum: 50).",
    )
    args = parser.parse_args(argv)

    _load_config()
    _ensure_bridge_on_path()
    from translation_sidecar import generate_translation_sidecar

    output = generate_translation_sidecar(args.epub, batch_size=args.batch_size)
    document = json.loads(output.read_text(encoding="utf-8"))
    count = len(document["translations"])
    noun = "translation" if count == 1 else "translations"
    print(f"Wrote {count} {noun} to {output}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="marginalia",
        description="marginalia — AI reading companion for KOReader.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Start the bridge server.")
    sub.add_parser("setup", help="Interactive first-run setup wizard.")
    sub.add_parser("translations", help="Generate an offline EPUB translation sidecar.")
    args, rest = parser.parse_known_args(argv)

    if args.command == "serve":
        serve(rest)
    elif args.command == "setup":
        setup(rest)
    elif args.command == "translations":
        translations(rest)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
