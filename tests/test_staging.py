"""Staged writes (SAFETY.md, layers 3-4). The pure checks run everywhere; the rest need PP.

Each integration test stages against its own copy of the demo fixture, so nothing here can
touch the session replica the read tests use.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from pp_mcp import staging
from pp_mcp.config import Config, ConfigError
from pp_mcp.staging import StagingError, check_delta

FIXTURE = Path(__file__).parent / "fixtures" / "demo.xml"
ROOT = Path(__file__).resolve().parents[1]


# --- pure ------------------------------------------------------------------------------------


def test_delta_must_match_exactly():
    check_delta({"prices": 10}, {"prices": 12}, {"prices": 2})
    with pytest.raises(StagingError, match="did not do what it declared"):
        check_delta({"prices": 10}, {"prices": 13}, {"prices": 2})


def test_undeclared_counters_must_not_move():
    with pytest.raises(StagingError, match="account_tx:BUY"):
        check_delta({"prices": 1}, {"prices": 2, "account_tx:BUY": 1}, {"prices": 1})


def test_removals_cannot_be_declared():
    with pytest.raises(StagingError, match="only add"):
        check_delta({"prices": 2}, {"prices": 1}, {"prices": -1})


def test_staged_dir_must_differ_from_replica_dir(tmp_path):
    replica = tmp_path / "portfolio.xml"
    replica.write_text("<client/>")
    config = Config(source=replica, app_path=tmp_path)
    with pytest.raises(ConfigError, match="must not be the replica"):
        staging.staging_area(config, env={"PP_STAGED_DIR": str(tmp_path)})


# --- against Portfolio Performance -----------------------------------------------------------


@pytest.fixture
def area(config, tmp_path):
    """A private read-only replica and an empty staging folder."""
    replica = tmp_path / "replica" / "portfolio.xml"
    replica.parent.mkdir()
    shutil.copyfile(FIXTURE, replica)
    replica.chmod(0o444)
    return staging.staging_area(
        Config(source=replica, app_path=config.app_path),
        env={"PP_STAGED_DIR": str(tmp_path / "staged")},
    )


def add_prices(count: int, year: int = 2031):
    """A mutation that adds `count` prices to the first security that has any."""
    import jpype

    def mutate(client):
        SecurityPrice = jpype.JClass("name.abuchen.portfolio.model.SecurityPrice")
        LocalDate = jpype.JClass("java.time.LocalDate")
        security = next(s for s in client.getSecurities() if s.getPrices().size())
        for day in range(1, count + 1):
            security.addPrice(SecurityPrice(LocalDate.of(year, 1, day), 100 * 100_000_000))
        return {"security": str(security.getName()), "added": count}

    return mutate


def test_first_write_stages_a_copy_and_leaves_the_replica_alone(area):
    replica_sha = staging.sha256(area.replica)

    result = staging.apply(area, "add 2 prices", {"prices": 2}, add_prices(2))

    assert staging.sha256(area.replica) == replica_sha
    assert area.staged.exists()
    assert not os.access(area.staged, os.W_OK), "the staged copy is kept read-only"
    assert result["pending_changes"] == 1
    assert result["change"]["delta"] == {"prices": 2}

    manifest = json.loads(area.manifest.read_text())
    assert manifest["base_sha256"] == replica_sha
    assert manifest["staged_sha256"] == staging.sha256(area.staged)


def test_later_writes_build_on_the_staged_copy(area):
    staging.apply(area, "first", {"prices": 1}, add_prices(1, year=2031))
    staging.apply(area, "second", {"prices": 1}, add_prices(1, year=2032))

    status = staging.status(area)
    assert status["pending_changes"] == 2
    assert status["up_to_date_with_replica"] and status["unmodified"]
    total = sum(c["delta"]["prices"] for c in status["changes"])
    assert total == 2


def test_a_change_that_does_more_than_declared_is_not_staged(area):
    staging.apply(area, "ok", {"prices": 1}, add_prices(1, year=2031))
    before = staging.sha256(area.staged)

    with pytest.raises(StagingError, match="did not do what it declared"):
        staging.apply(area, "sneaky", {"prices": 1}, add_prices(3, year=2033))

    assert staging.sha256(area.staged) == before
    assert staging.status(area)["pending_changes"] == 1
    assert not list(area.directory.glob(".*.tmp")), "no temp file left behind"


def test_refreshed_replica_blocks_further_writes(area):
    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    area.replica.chmod(0o644)
    area.replica.write_bytes(area.replica.read_bytes() + b"\n")
    area.replica.chmod(0o444)

    with pytest.raises(StagingError, match="replica was refreshed"):
        staging.apply(area, "again", {"prices": 1}, add_prices(1, year=2032))


def test_staged_copy_edited_elsewhere_blocks_writes(area):
    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    area.staged.chmod(0o644)
    area.staged.write_bytes(area.staged.read_bytes() + b"\n")

    with pytest.raises(StagingError, match="modified outside pp-write"):
        staging.apply(area, "again", {"prices": 1}, add_prices(1, year=2032))


def test_reads_see_the_staged_copy_once_there_is_one(area, monkeypatch):
    monkeypatch.setenv("PP_FILE", str(area.replica))
    assert staging.read_config(area).source == area.replica

    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    assert staging.read_config(area).source == area.staged


def _promote(area, live: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PP_LIVE_FILE": str(live), "PP_STAGED_DIR": str(area.directory)}
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "promote-staged.sh")],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _pp_running() -> bool:
    return subprocess.run(["pgrep", "-f", "PortfolioPerformance"], capture_output=True).returncode == 0


def test_promote_refuses_when_live_file_moved_on(area, tmp_path):
    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    live = tmp_path / "live.xml"
    live.write_bytes(area.replica.read_bytes() + b"\n")  # edited in the app since the replica
    before = live.read_bytes()

    result = _promote(area, live)

    assert result.returncode != 0
    assert live.read_bytes() == before
    if not _pp_running():
        assert "live file changed" in result.stderr


def test_promote_needs_a_terminal(area, tmp_path):
    if _pp_running():
        pytest.skip("Portfolio Performance is running; the script refuses before this check")
    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    live = tmp_path / "live.xml"
    shutil.copyfile(area.replica, live)
    live.chmod(0o644)
    before = live.read_bytes()

    result = _promote(area, live)

    assert result.returncode != 0
    assert "no terminal" in result.stderr
    assert live.read_bytes() == before


def test_promote_fails_closed_without_a_way_to_check_the_app(area, tmp_path):
    """No pgrep and no tasklist (e.g. a bare Git Bash) must block, not count as 'not running'."""
    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    live = tmp_path / "live.xml"
    shutil.copyfile(area.replica, live)
    live.chmod(0o644)
    before = live.read_bytes()
    no_tools = tmp_path / "no-tools.sh"
    no_tools.write_text("command() { return 1; }\n")  # every `command -v` lookup fails

    env = {**os.environ, "PP_LIVE_FILE": str(live), "PP_STAGED_DIR": str(area.directory),
           "BASH_ENV": str(no_tools)}
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "promote-staged.sh")],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=True,
    )

    assert result.returncode != 0
    assert "cannot check whether Portfolio Performance is running" in result.stderr
    assert live.read_bytes() == before


def test_a_change_pp_would_flag_is_not_staged(area):
    """A portfolio BUY without its cash leg is something PP's own checks report on opening."""
    import jpype

    def orphan_buy(client):
        portfolio = list(client.getPortfolios())[0]
        security = next(s for s in client.getSecurities() if s.getPrices().size())
        transaction = jpype.JClass("name.abuchen.portfolio.model.PortfolioTransaction")()
        transaction.setType(jpype.JClass("name.abuchen.portfolio.model.PortfolioTransaction$Type").BUY)
        transaction.setDateTime(jpype.JClass("java.time.LocalDateTime").of(2026, 11, 3, 0, 0))
        transaction.setSecurity(security)
        transaction.setShares(100_000_000)
        transaction.setCurrencyCode(security.getCurrencyCode())
        transaction.setAmount(10_000)
        portfolio.addTransaction(transaction)
        return {}

    with pytest.raises(StagingError, match="consistency checks flag the result"):
        staging.apply(area, "orphan", {"portfolio_tx:BUY": 1}, orphan_buy)
    assert not area.staged.exists()
    assert not list(area.directory.glob(".*.tmp"))


# --- recovering ------------------------------------------------------------------------------


def _start_write_server(area, monkeypatch):
    from pp_mcp import server
    from pp_mcp.bridge import set_config_provider

    monkeypatch.setenv("PP_FILE", str(area.replica))
    monkeypatch.setenv("PP_STAGED_DIR", str(area.directory))
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self: None)
    try:
        server.main_write()
    finally:
        set_config_provider(None)


@pytest.mark.parametrize("damage", ["replica", "manifest", "no manifest"])
def test_pp_write_starts_and_can_discard_unusable_staged_changes(area, monkeypatch, damage):
    from pp_mcp import write_tools

    staging.apply(area, "ok", {"prices": 1}, add_prices(1))
    if damage == "replica":
        area.replica.chmod(0o644)
        area.replica.write_bytes(area.replica.read_bytes() + b"\n")
        area.replica.chmod(0o444)
    elif damage == "manifest":
        area.manifest.write_text("{bad")
    else:
        area.manifest.unlink()

    _start_write_server(area, monkeypatch)
    with pytest.raises(ConfigError):
        staging.read_config(area)
    if damage == "manifest":
        with pytest.raises(ToolError, match="cannot read"):
            write_tools.get_staged_changes()

    assert write_tools.discard_staged()["discarded"] is True
    assert staging.read_config(area).source == area.replica


def test_editing_an_existing_record_is_not_staged(area):
    """No counter moves, so only comparing contents catches it."""
    def edit_in_place(client):
        account = next(a for a in client.getAccounts() if a.getTransactions().size())
        account.getTransactions().get(0).setAmount(123)
        return {}

    with pytest.raises(StagingError, match=r"altered existing records \(1 transaction"):
        staging.apply(area, "sneaky edit", {}, edit_in_place)
    assert not area.staged.exists()


def test_editing_alongside_a_declared_addition_is_not_staged(area):
    add = add_prices(1)

    def add_and_rename(client):
        list(client.getAccounts())[0].setName("Renamed")
        return add(client)

    with pytest.raises(StagingError, match="altered existing records"):
        staging.apply(area, "sneaky rename", {"prices": 1}, add_and_rename)
