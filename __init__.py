"""Hermes plugin entry point for this standalone repository.

A native Hermes plugin directory needs ``plugin.yaml`` plus an ``__init__.py``
with ``register(ctx)``. The library lives under ``src/``, so make it importable
when this repo is installed as ``~/.hermes/plugins/hermes-flight-recorder/``
(no pip install required). Runtime state never lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from hermes_flight_recorder.plugin import register

__all__ = ["register"]
