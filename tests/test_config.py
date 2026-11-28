from __future__ import annotations

import pytest

from pp_engine_mcp.config import ConfigError, default_rates_file, load_config


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


def test_platforms_without_st_blocks(tmp_path, monkeypatch):
    """Windows' stat result has no st_blocks."""
    from pathlib import Path
    from types import SimpleNamespace

    source = tmp_path / "portfolio.xml"
    source.write_text("<client/>")
    source.chmod(0o444)
    env, real = _env(tmp_path, source), Path.stat

    def windows_stat(self, **kwargs):
        result = real(self, **kwargs)
        return SimpleNamespace(st_mode=result.st_mode, st_size=result.st_size) if self == source else result

    monkeypatch.setattr(Path, "stat", windows_stat)
    assert load_config(env).source == source


def _replica(tmp_path):
    source = tmp_path / "portfolio.xml"
    source.write_text("<client/>")
    source.chmod(0o444)
    return source


def test_rates_file_defaults_to_the_apps_workspace(tmp_path):
    rates_file = load_config(_env(tmp_path, _replica(tmp_path))).rates_file
    assert rates_file is not None
    assert rates_file.name == "ecb_exchange_rates.pb" and rates_file.parent.name == "name.abuchen.portfolio"


@pytest.mark.parametrize("platform, expected", [
    ("darwin", "Library/Application Support/name.abuchen.portfolio.product/workspace"),
    ("linux", ".PortfolioPerformance/workspace"),
    ("win32", "Local/PortfolioPerformance/workspace"),
])
def test_rates_file_per_platform(tmp_path, monkeypatch, platform, expected):
    monkeypatch.setattr("pp_engine_mcp.config.sys.platform", platform)
    path = default_rates_file({"LOCALAPPDATA": str(tmp_path / "Local")})
    assert expected in path.as_posix()


def test_rates_file_can_be_set_and_must_exist(tmp_path):
    source = _replica(tmp_path)
    with pytest.raises(ConfigError, match="PP_RATES_FILE"):
        load_config(_env(tmp_path, source, PP_RATES_FILE=str(tmp_path / "none.pb")))
    rates = tmp_path / "rates.pb"
    rates.touch()
    assert load_config(_env(tmp_path, source, PP_RATES_FILE=str(rates))).rates_file == rates
