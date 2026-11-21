"""The append-only ``.state`` file for one tagged session.

Thin, testable wrapper over :mod:`hermes_flight_recorder.records`. Session
identity, tagging and the registry are a later roadmap step -- this class only
owns one file on disk.
"""

from __future__ import annotations

from pathlib import Path

from .records import Record, append_record, build_context, read_records


class StateFile:
    """One session's append-only record log.

    Each ``append`` is locked, fsynced, and numbered. ``context()`` returns the
    deterministic rebuild used to construct a prompt.
    """

    def __init__(self, path):
        self.path = Path(path)

    def append(self, kind: str, body=b"", **kwargs) -> Record:
        return append_record(self.path, kind, body, **kwargs)

    def read(self) -> list[Record]:
        return read_records(self.path)

    def context(self) -> tuple[list[Record], int]:
        return build_context(self.read())

    @property
    def last_n(self) -> int:
        records = self.read()
        return records[-1].n if records else 0

    def __len__(self) -> int:
        return len(self.read())

    def __repr__(self) -> str:
        return f"<StateFile {self.path} records={len(self)}>"
