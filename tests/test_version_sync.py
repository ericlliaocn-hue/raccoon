from __future__ import annotations

import re
import tomllib
from pathlib import Path


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def test_release_versions_are_synced():
    root = Path(__file__).resolve().parent.parent

    with (root / "pyproject.toml").open("rb") as f:
        pyproject_version = str(tomllib.load(f).get("project", {}).get("version", "")).strip()

    src_init_text = (root / "src" / "__init__.py").read_text(encoding="utf-8")
    src_version = _first_match(src_init_text, r'__version__\s*=\s*"([^"]+)"')

    index_text = (root / "src" / "adapters" / "static" / "index.html").read_text(encoding="utf-8")
    static_css_version = _first_match(index_text, r"/static/style\.css\?v=([0-9A-Za-z_.-]+)")
    static_js_version = _first_match(index_text, r"/static/app\.js\?v=([0-9A-Za-z_.-]+)")
    ui_badge_version = _first_match(index_text, r"Project Raccoon v([0-9A-Za-z_.-]+)")

    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_version = _first_match(changelog_text, r"^## \[([^\]]+)\] - ")

    versions = {
        "pyproject": pyproject_version,
        "src_init": src_version,
        "static_css": static_css_version,
        "static_js": static_js_version,
        "ui_badge": ui_badge_version,
        "changelog_latest": changelog_version,
    }

    missing = [name for name, value in versions.items() if not value]
    assert not missing, f"missing version fields: {missing}"
    assert len(set(versions.values())) == 1, f"version mismatch: {versions}"
