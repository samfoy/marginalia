"""test_setup_wizard.py — unit tests for setup_wizard module."""

import os
from pathlib import Path

import pytest

import setup_wizard
from setup_wizard import _write_config, _load_existing


# ═══════════════════════════════════════════════════════════════════════════════
# _write_config
# ═══════════════════════════════════════════════════════════════════════════════

class TestWriteConfig:

    def test_writes_export_lines(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        _write_config({"MARGINALIA_PORT": "7731", "MARGINALIA_VAULT": "/home/user/docs"})
        content = cfg_file.read_text()
        assert "export MARGINALIA_PORT=7731" in content
        assert "export MARGINALIA_VAULT=/home/user/docs" in content

    def test_quotes_values_with_spaces(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        _write_config({"MARGINALIA_VAULT": "/Users/sam painter/Documents"})
        content = cfg_file.read_text()
        assert 'export MARGINALIA_VAULT="/Users/sam painter/Documents"' in content

    def test_does_not_quote_values_without_spaces(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        _write_config({"MARGINALIA_PORT": "7731"})
        content = cfg_file.read_text()
        # Should NOT have quotes around the value
        assert 'export MARGINALIA_PORT="7731"' not in content
        assert "export MARGINALIA_PORT=7731" in content

    def test_creates_file(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        _write_config({"K": "V"})
        assert cfg_file.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# _load_existing
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadExisting:

    def test_parses_export_lines(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        cfg_file.write_text(
            "export MARGINALIA_PORT=7731\n"
            "export MARGINALIA_VAULT=/some/path\n"
        )
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        result = _load_existing()
        assert result["MARGINALIA_PORT"] == "7731"
        assert result["MARGINALIA_VAULT"] == "/some/path"

    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "no_such_file.env"
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        result = _load_existing()
        assert result == {}

    def test_strips_double_quotes(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        cfg_file.write_text('export MARGINALIA_VAULT="/Users/sam painter/Docs"\n')
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        result = _load_existing()
        assert result["MARGINALIA_VAULT"] == "/Users/sam painter/Docs"

    def test_strips_single_quotes(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        cfg_file.write_text("export MARGINALIA_KEY='sk-abc123'\n")
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        result = _load_existing()
        assert result["MARGINALIA_KEY"] == "sk-abc123"

    def test_ignores_comment_lines(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        cfg_file.write_text(
            "# This is a comment\n"
            "export MARGINALIA_PORT=7731\n"
        )
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        result = _load_existing()
        assert len(result) == 1
        assert "MARGINALIA_PORT" in result

    def test_roundtrip_write_then_load(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / ".marginalia.env"
        monkeypatch.setattr(setup_wizard, "CONFIG_FILE", cfg_file)
        original = {
            "MARGINALIA_PORT": "7731",
            "MARGINALIA_VAULT": "/home/user/vault",
            "MARGINALIA_MODEL_ID": "openai:gpt-4o",
        }
        _write_config(original)
        loaded = _load_existing()
        for k, v in original.items():
            assert loaded[k] == v


# ═══════════════════════════════════════════════════════════════════════════════
# subdir path resolution logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubdirResolution:
    """
    Tests for the path resolution logic inside _setup_vault's _ask_subdir.
    We test the formula directly (no need to invoke the interactive wizard):
        resolved = val if os.path.isabs(val) else os.path.join(vault, val)
    """

    def _resolve(self, vault: str, val: str) -> str:
        """Mirror the resolution logic from _ask_subdir."""
        if val.startswith("~"):
            val = os.path.expanduser(val)
        return val if os.path.isabs(val) else os.path.join(vault, val)

    def test_relative_path_resolved_against_vault(self):
        vault = "/a/b"
        resolved = self._resolve(vault, "Notes/Books")
        assert resolved == "/a/b/Notes/Books"

    def test_absolute_path_unchanged(self):
        vault = "/a/b"
        resolved = self._resolve(vault, "/other/path")
        assert resolved == "/other/path"

    def test_tilde_expanded(self):
        vault = "/a/b"
        home = str(Path.home())
        resolved = self._resolve(vault, "~/MyNotes")
        assert resolved == os.path.join(home, "MyNotes")
        assert "~" not in resolved

    def test_relative_captures_resolved(self):
        vault = "/Users/sam/Documents/Sam"
        resolved = self._resolve(vault, "Notes/Captures")
        assert resolved == "/Users/sam/Documents/Sam/Notes/Captures"
