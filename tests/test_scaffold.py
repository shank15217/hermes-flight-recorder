"""Scaffold tests.

One real test (the layout exists) plus skipped specifications that become real
tests as each roadmap step lands. Kept explicit so the intended behaviour is
documented before any implementation exists.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_scaffold_layout():
    for rel in (
        "README.md", "NOTICE", "LICENSE", "CONTRIBUTING.md",
        "docs/design.md", "docs/prior-art.md", "pyproject.toml", "plugin.yaml",
        "src/hermes_flight_recorder/__init__.py",
    ):
        assert (ROOT / rel).is_file(), f"missing {rel}"


def test_attribution_is_present():
    notice = (ROOT / "NOTICE").read_text()
    assert "mainplane-ai/mainplane" in notice
    assert "@Alezander9" in notice
    assert "MIT" in notice


# Step 1 landed: the record round-trip and Build() specs now live as real
# tests in tests/test_records.py.


@pytest.mark.skip(reason="roadmap step 2: registry not implemented")
def test_untagged_sessions_are_untouched():
    ...


@pytest.mark.skip(reason="roadmap step 5: rollover not implemented")
def test_cold_limit_rollover_hands_off_to_new_session():
    ...
