"""Install-layout discovery, tested with fake folders so it runs on any OS without PP."""

from __future__ import annotations

from pathlib import Path

import pytest

from pp_mcp.bridge import BridgeError, find_libjvm
from pp_mcp.config import Config, plugins_dir


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_plugins_dir_on_macos_and_elsewhere(tmp_path):
    mac = tmp_path / "PortfolioPerformance.app"
    (mac / "Contents" / "Eclipse" / "plugins").mkdir(parents=True)
    assert plugins_dir(mac) == mac / "Contents" / "Eclipse" / "plugins"

    linux = tmp_path / "portfolio"
    (linux / "plugins").mkdir(parents=True)
    assert plugins_dir(linux) == linux / "plugins"


def test_bundled_macos_jre(tmp_path):
    app = tmp_path / "PortfolioPerformance.app"
    lib = _touch(
        app / "Contents" / "Eclipse" / "plugins" / "name.abuchen.zulu.jre.macosx_21.0.4"
        / "jre" / "Contents" / "Home" / "lib" / "server" / "libjvm.dylib"
    )
    assert find_libjvm(Config(source=tmp_path / "x.xml", app_path=app), env={}) == lib


def test_bundled_jre_beside_plugins(tmp_path):
    app = tmp_path / "portfolio"
    (app / "plugins").mkdir(parents=True)
    lib = _touch(app / "jre" / "lib" / "server" / "libjvm.so")
    assert find_libjvm(Config(source=tmp_path / "x.xml", app_path=app), env={}) == lib


def test_java_home_fallback(tmp_path):
    app = tmp_path / "portfolio"
    (app / "plugins").mkdir(parents=True)
    lib = _touch(tmp_path / "jdk" / "lib" / "server" / "libjvm.so")
    config = Config(source=tmp_path / "x.xml", app_path=app)
    assert find_libjvm(config, env={"JAVA_HOME": str(tmp_path / "jdk")}) == lib


def test_no_runtime_names_java_home(tmp_path):
    app = tmp_path / "portfolio"
    (app / "plugins").mkdir(parents=True)
    with pytest.raises(BridgeError, match="JAVA_HOME"):
        find_libjvm(Config(source=tmp_path / "x.xml", app_path=app), env={})
