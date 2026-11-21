"""Per-profile registry of tagged sessions (scaffold).

Stored at ``plugin_data_dir()/registry.json``. See docs/design.md §4 for schema.
"""

from __future__ import annotations

REGISTRY_VERSION = 1


class Registry:
    """Roadmap step 2: tag/untag/status/chain for ``type: mainplane`` sessions."""

    def __init__(self, data_dir):
        raise NotImplementedError("Registry lands in roadmap step 2")
