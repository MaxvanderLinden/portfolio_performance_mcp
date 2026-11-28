"""Configuration for the Portfolio Performance bridge.

The server is pointed at a *replica* of the portfolio file, never the live one. See SAFETY.md:
the live file's path is deliberately absent from this configuration, so no bug or bad tool call
can reach it. `scripts/refresh-replica.sh` keeps the replica current.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPLICA = Path.home() / "pp-engine-mcp" / "replica" / "portfolio.xml"


def _default_app() -> Path:
    """Where the official installers put Portfolio Performance on each platform."""
    if sys.platform == "darwin":
        return Path("/Applications/PortfolioPerformance.app")
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PortfolioPerformance"
    return Path("/opt/portfolio")


DEFAULT_APP = _default_app()


def default_rates_file(env: Mapping[str, str] | None = None) -> Path:
    """Where the app caches the ECB exchange rates: its workspace folder, per platform."""
    env = os.environ if env is None else env
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "name.abuchen.portfolio.product"
    elif sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "PortfolioPerformance"
    else:
        base = Path.home() / ".PortfolioPerformance"
    return base / "workspace" / ".metadata" / ".plugins" / "name.abuchen.portfolio" / "ecb_exchange_rates.pb"


class ConfigError(Exception):
    """Raised when the environment cannot be trusted to be read-only."""


@dataclass(frozen=True)
class Config:
    source: Path
    app_path: Path
    rates_file: Path | None = None  # the app's exchange rate cache; read only

    @property
    def plugins(self) -> Path:
        return plugins_dir(self.app_path)


def plugins_dir(app_path: Path) -> Path:
    """The Eclipse plugins folder: inside the bundle on macOS, at the top level elsewhere."""
    mac = app_path / "Contents" / "Eclipse" / "plugins"
    return mac if mac.is_dir() else app_path / "plugins"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    source = Path(env.get("PP_FILE", str(DEFAULT_REPLICA))).expanduser()
    app_path = Path(env.get("PP_APP_PATH", str(DEFAULT_APP))).expanduser()
    allow_writable = env.get("PP_ALLOW_WRITABLE_SOURCE") == "1"

    if not app_path.is_dir():
        raise ConfigError(
            f"Portfolio Performance not found at {app_path}. Set PP_APP_PATH to its install folder "
            "(the .app bundle on macOS)."
        )
    if not plugins_dir(app_path).is_dir():
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
    if getattr(stat, "st_blocks", None) == 0:  # Windows has no st_blocks
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

    # An absent default is only a warning later: a file in one currency needs no rates.
    if env.get("PP_RATES_FILE"):
        rates_file = Path(env["PP_RATES_FILE"]).expanduser()
        if not rates_file.is_file():
            raise ConfigError(f"PP_RATES_FILE must be an exchange rate file, but there is none at {rates_file}.")
    else:
        rates_file = default_rates_file(env)

    return Config(source=source, app_path=app_path, rates_file=rates_file)
