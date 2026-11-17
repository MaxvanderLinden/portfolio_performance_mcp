"""Install-layout discovery, tested with fake folders so it runs on any OS without PP."""

from __future__ import annotations

from pathlib import Path

import pytest

from pp_mcp.bridge import BridgeError, find_classpath, find_libjvm, newest_bundles
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


def test_classpath_keeps_only_the_newest_version_of_each_bundle(tmp_path):
    """An app update leaves old bundle versions in plugins/; loading both mixes their classes."""
    app = tmp_path / "PortfolioPerformance.app"
    plugins = app / "Contents" / "Eclipse" / "plugins"
    for name in (
        "name.abuchen.portfolio_0.89.0.jar",
        "name.abuchen.portfolio_0.90.0.jar",
        "name.abuchen.portfolio_0.9.0.jar",          # 0.9 < 0.90 only when compared numerically
        "name.abuchen.portfolio.ui_0.89.0.jar",
        "com.google.gson_2.10.1.v20230109-0753.jar",
        "com.google.gson_2.10.1.v20240101-0000.jar",  # same version, newer qualifier
        "some_unversioned.jar",
    ):
        _touch(plugins / name)

    names = {Path(jar).name for jar in find_classpath(Config(source=tmp_path / "x.xml", app_path=app))}
    assert names == {
        "name.abuchen.portfolio_0.90.0.jar",
        "name.abuchen.portfolio.ui_0.89.0.jar",
        "com.google.gson_2.10.1.v20240101-0000.jar",
        "some_unversioned.jar",
    }


def test_bundle_names_with_underscores_keep_their_versions_apart():
    assert newest_bundles(["a/foo_bar_1.0.0.jar", "a/foo_bar_1.2.0.jar", "a/foo_2.0.0.jar"]) == [
        "a/foo_2.0.0.jar", "a/foo_bar_1.2.0.jar",
    ]


def test_classpath_without_core_jar_is_an_error(tmp_path):
    app = tmp_path / "PortfolioPerformance.app"
    _touch(app / "Contents" / "Eclipse" / "plugins" / "com.google.gson_2.10.1.jar")
    with pytest.raises(BridgeError, match="core jar"):
        find_classpath(Config(source=tmp_path / "x.xml", app_path=app))
