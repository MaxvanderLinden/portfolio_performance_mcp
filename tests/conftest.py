"""Integration tests run against tests/fixtures/demo.xml, a fictional portfolio.

The fixture is copied to a temporary, read-only replica and PP_FILE is pointed at it, so the
tests never read your own data. They need Portfolio Performance itself (for the core jar and
JRE) and skip when it is not installed; set PP_APP_PATH if it is not in /Applications.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from pp_mcp.bridge import get_client
from pp_mcp.config import ConfigError, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "demo.xml"


@pytest.fixture(scope="session")
def config():
    replica = Path(tempfile.mkdtemp(prefix="pp-mcp-tests-")) / "portfolio.xml"
    shutil.copyfile(FIXTURE, replica)
    replica.chmod(0o444)
    os.environ["PP_FILE"] = str(replica)
    os.environ.pop("PP_ALLOW_WRITABLE_SOURCE", None)
    try:
        yield load_config()
    except ConfigError as exc:
        pytest.skip(f"Portfolio Performance not available: {exc}")
    finally:
        replica.chmod(0o644)
        shutil.rmtree(replica.parent, ignore_errors=True)


@pytest.fixture(scope="session")
def handle(config):
    return get_client(config)
