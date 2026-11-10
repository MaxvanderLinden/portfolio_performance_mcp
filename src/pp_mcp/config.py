"""Configuration for the Portfolio Performance bridge.

The server is pointed at a *replica* of the portfolio file, never the live one. See SAFETY.md:
the live file's path is deliberately absent from this configuration, so no bug or bad tool call
can reach it. `scripts/refresh-replica.sh` keeps the replica current.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPLICA = Path.home() / "pp-mcp" / "replica" / "portfolio.xml"
DEFAULT_APP = Path("/Applications/PortfolioPerformance.app")


class ConfigError(Exception):
    """Raised when the environment cannot be trusted to be read-only."""


@dataclass(frozen=True)
class Config:
    source: Path
    app_path: Path

    @property
    def plugins(self) -> Path:
        return self.app_path / "Contents" / "Eclipse" / "plugins"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    source = Path(env.get("PP_FILE", str(DEFAULT_REPLICA))).expanduser()
    app_path = Path(env.get("PP_APP_PATH", str(DEFAULT_APP))).expanduser()
    allow_writable = env.get("PP_ALLOW_WRITABLE_SOURCE") == "1"

    if not app_path.is_dir():
        raise ConfigError(
            f"Portfolio Performance not found at {app_path}. Set PP_APP_PATH to the .app bundle."
        )
    if not (app_path / "Contents" / "Eclipse" / "plugins").is_dir():
        raise ConfigError(f"{app_path} does not look like a Portfolio Performance bundle.")

    if not source.exists():
        raise ConfigError(
            f"No portfolio replica at {source}. Run scripts/refresh-replica.sh, or set PP_FILE."
        )
    if not source.is_file():
        raise ConfigError(f"PP_FILE must be a file, got {source}.")

    stat = source.stat()
    if stat.st_size == 0:
        raise ConfigError(f"Replica {source} is empty.")
    if stat.st_blocks == 0:
        raise ConfigError(
            f"Replica {source} is a dataless iCloud placeholder. Run: brctl download '{source}'"
        )

    # A replica that we can write to is not a replica. This is the check that catches the
    # dangerous mistake: PP_FILE pointing at the live file, which is mode 644.
    if os.access(source, os.W_OK) and not allow_writable:
        raise ConfigError(
            f"Replica {source} is writable (mode {stat.st_mode & 0o777:o}). Expected a read-only "
            "copy: run 'chmod 444' on it, or point PP_FILE at a replica rather than the live file. "
            "Set PP_ALLOW_WRITABLE_SOURCE=1 only if you understand why that is unsafe."
        )

    return Config(source=source, app_path=app_path)
