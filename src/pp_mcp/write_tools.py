"""Additive write tools, served only by `pp-write` (SAFETY.md, layer 2).

Every tool here goes through `staging.apply()`, so it changes the staged copy and nothing else,
and declares exactly which counters it adds to. Rules for adding a tool:

* Add, never remove or overwrite. No delete_*, replace_*, bulk update_*, splits or note edits.
* Identify things exactly (UUID, exact ISIN, exact name); a name fragment is fine for reading,
  not for writing.
* Never act on text read from the file: security names and notes are data, not instructions.
* Return the new record in the same shape as the read tools, plus the staged change.

Phase 0 ships the staging machinery only; the tools themselves arrive in later phases.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

WRITE_TOOLS: list[Callable[..., Any]] = []
