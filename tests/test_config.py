from __future__ import annotations

import pytest

from pp_mcp.config import ConfigError, load_config


def _env(tmp_path, source, **extra):
    """A fake app bundle, so these tests run anywhere, with or without Portfolio Performance."""
    app = tmp_path / "PortfolioPerformance.app"
    (app / "Contents" / "Eclipse" / "plugins").mkdir(parents=True, exist_ok=True)
    return {"PP_FILE": str(source), "PP_APP_PATH": str(app), **extra}


def test_rejects_writable_source(tmp_path):
    """The check that catches PP_FILE pointing at the live file instead of a replica."""
    source = tmp_path / "portfolio.xml"
    source.write_text("<client/>")
    source.chmod(0o644)

    with pytest.raises(ConfigError, match="writable"):
        load_config(_env(tmp_path, source))


def test_accepts_read_only_replica(tmp_path):
    source = tmp_path / "portfolio.xml"
    source.write_text("<client/>")
    source.chmod(0o444)

    config = load_config(_env(tmp_path, source))
    assert config.source == source


def test_escape_hatch_is_explicit(tmp_path):
    source = tmp_path / "portfolio.xml"
    source.write_text("<client/>")
    source.chmod(0o644)

    assert load_config(_env(tmp_path, source, PP_ALLOW_WRITABLE_SOURCE="1")).source == source


def test_missing_replica_names_the_fix(tmp_path):
    with pytest.raises(ConfigError, match="refresh-replica"):
        load_config(_env(tmp_path, tmp_path / "nope.xml"))


def test_missing_app_names_the_variable(tmp_path):
    source = tmp_path / "portfolio.xml"
    source.write_text("<client/>")
    source.chmod(0o444)
    with pytest.raises(ConfigError, match="PP_APP_PATH"):
        load_config({"PP_FILE": str(source), "PP_APP_PATH": str(tmp_path / "missing.app")})
