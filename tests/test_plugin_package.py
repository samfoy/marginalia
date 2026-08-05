"""Distribution contract for the self-contained KOReader plugin package."""

from __future__ import annotations

import re
import shutil
from pathlib import Path


ROOT = Path(__file__).parent.parent
PACKAGE_NAME = "marginalia.koplugin"
REQUIRE_RE = re.compile(r"\brequire\s*\(?\s*[\"']([^\"']+)[\"']")


def metadata(package: Path) -> dict[str, str]:
    source = (package / "_meta.lua").read_text(encoding="utf-8")
    return dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"', source, re.MULTILINE))


def local_require_targets(package: Path) -> set[str]:
    bundled = {path.stem for path in package.glob("*.lua")}
    required: set[str] = set()
    for source_path in package.glob("*.lua"):
        for module in REQUIRE_RE.findall(source_path.read_text(encoding="utf-8")):
            if module == "bridge" or module.startswith("marginalia_"):
                required.add(module)
    assert required <= bundled
    return required


def test_repository_has_one_canonical_app_store_package():
    packages = sorted(path for path in ROOT.glob("*.koplugin") if path.is_dir())
    assert [path.name for path in packages] == [PACKAGE_NAME]
    package = packages[0]
    assert (package / "_meta.lua").is_file()
    assert (package / "main.lua").is_file()
    assert metadata(package)["name"] == "marginalia"
    assert metadata(package)["version"] == "0.10.3"


def test_all_plugin_local_requires_are_bundled():
    package = ROOT / PACKAGE_NAME
    required = local_require_targets(package)
    assert {
        "bridge",
        "marginalia_translation_sidecar",
        "marginalia_translation_text",
    } <= required


def test_copied_plugin_directory_is_self_contained(tmp_path):
    source = ROOT / PACKAGE_NAME
    installed = tmp_path / PACKAGE_NAME
    shutil.copytree(source, installed)

    assert metadata(installed) == metadata(source)
    assert local_require_targets(installed) == local_require_targets(source)
    assert not any(path.is_symlink() for path in installed.rglob("*"))
    assert not list(installed.rglob("*.py"))
    assert not list(installed.rglob("*.marginalia-translations.json"))
    assert not list(installed.rglob("test*"))

    for path in installed.rglob("*.lua"):
        text = path.read_text(encoding="utf-8")
        assert "../" not in text
        assert not any(module.endswith(".py") for module in REQUIRE_RE.findall(text))


def test_device_translation_modules_have_no_generated_or_bridge_side_dependency():
    package = ROOT / PACKAGE_NAME
    for name in ("marginalia_translation_sidecar.lua", "marginalia_translation_text.lua"):
        source = (package / name).read_text(encoding="utf-8")
        modules = REQUIRE_RE.findall(source)
        assert "bridge" not in modules
        assert not any(module.endswith(".py") for module in modules)
        assert "dofile" not in source
        assert "loadfile" not in source
        assert "os.execute" not in source
        assert "io.popen" not in source


def test_mutable_settings_live_outside_plugin_directory():
    package = ROOT / PACKAGE_NAME
    main = (package / "main.lua").read_text(encoding="utf-8")
    cache = (package / "marginalia_cache.lua").read_text(encoding="utf-8")
    queue = (package / "marginalia_queue.lua").read_text(encoding="utf-8")

    assert 'local SETTINGS_KEY   = "marginalia"' in main
    assert "G_reader_settings:readSetting(SETTINGS_KEY)" in main
    assert "G_reader_settings:saveSetting(SETTINGS_KEY" in main
    assert 'DataStorage:getSettingsDir() .. "/marginalia"' in cache
    assert 'DataStorage:getSettingsDir() .. "/marginalia"' in queue
    assert PACKAGE_NAME not in "\n".join((main, cache, queue))
