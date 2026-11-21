"""hermes-flight-recorder — opt-in append-only statefile sessions for Hermes Agent.

SCAFFOLD / step 0: this package contains the public module layout and the record
vocabulary only. Every operation raises ``NotImplementedError`` until roadmap step 1.

Design and attribution: see the repository README, NOTICE and docs/design.md.
"""

__version__ = "0.0.0"

# The internal session-format tag. See docs/prior-art.md "Note on naming".
TAGGED_SESSION_TYPE = "mainplane"

__all__ = ["__version__", "TAGGED_SESSION_TYPE"]
