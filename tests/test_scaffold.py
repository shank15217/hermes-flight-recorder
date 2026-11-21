"""Scaffold / repository-contract tests.

Real layout and attribution checks, plus the remaining skipped specification
for the last roadmap step. Step 1 and step 2 specs now live as real tests in
tests/test_records.py, tests/test_registry.py and tests/test_commands.py.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_scaffold_layout():
    for rel in (
        "README.md", "NOTICE", "LICENSE", "CONTRIBUTING.md",
        "docs/design.md", "docs/prior-art.md", "pyproject.toml", "plugin.yaml",
        "__init__.py",
        "src/hermes_flight_recorder/__init__.py",
    ):
        assert (ROOT / rel).is_file(), f"missing {rel}"


def test_attribution_is_present():
    notice = (ROOT / "NOTICE").read_text()
    assert "mainplane-ai/mainplane" in notice
    assert "@Alezander9" in notice
    assert "MIT" in notice


@pytest.mark.skip(reason="roadmap step 5: rollover not implemented")
def test_cold_limit_rollover_hands_off_to_new_session():
    ...
