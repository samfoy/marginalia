"""Tests for marginalia CLI command dispatch."""

from __future__ import annotations

import json

import cli
import translation_sidecar


def test_translations_command_loads_config_and_refreshes_sidecar(
    tmp_path, monkeypatch, capsys
):
    epub = tmp_path / "A.Book.epub"
    epub.write_bytes(b"epub")
    output = translation_sidecar.sidecar_path(epub)
    events = []

    monkeypatch.setattr(cli, "_load_config", lambda: events.append("config"))

    def fake_generate(path, *, batch_size):
        events.append((path, batch_size))
        output.write_text(json.dumps({"translations": {"deadbeef": {}}}))
        return output

    monkeypatch.setattr(
        translation_sidecar, "generate_translation_sidecar", fake_generate
    )

    cli.main(["translations", str(epub), "--batch-size", "7"])

    assert events == ["config", (epub, 7)]
    stdout = capsys.readouterr().out
    assert str(output) in stdout
    assert "1 translation" in stdout


def test_legacy_commands_still_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "serve", lambda argv: calls.append(("serve", argv)))
    monkeypatch.setattr(cli, "setup", lambda argv: calls.append(("setup", argv)))

    cli.main(["serve", "--port", "9000"])
    cli.main(["setup"])

    assert calls == [("serve", ["--port", "9000"]), ("setup", [])]