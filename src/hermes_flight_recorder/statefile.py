"""The append-only ``.state`` file for one tagged session (scaffold)."""

from __future__ import annotations


class StateFile:
    """Roadmap step 1: open/append/flush/fsync and per-step usage capture."""

    def __init__(self, path):
        raise NotImplementedError("StateFile lands in roadmap step 1")
