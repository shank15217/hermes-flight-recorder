"""Hermes plugin entry point (scaffold).

Roadmap steps 2-5 replace this with ``register_hook`` / ``register_command`` /
``register_skill`` wiring. Until then the plugin is deliberately inert: it
registers nothing, so installing the scaffold cannot affect a live session.
"""

from __future__ import annotations


def register(ctx):
    """Inert scaffold entry point. No hooks, tools or commands are registered."""
    return None
