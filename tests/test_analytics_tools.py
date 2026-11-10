"""Analytics tests: mostly invariants that must hold whatever the portfolio contains."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from pp_mcp import analytics_tools as at
from pp_mcp import read_tools as rt


@pytest.fixture(autouse=True)
def _loaded(handle):
    return handle


def test_period_aliases_map_to_pp_codes():
    assert at.get_performance("1Y")["period"]["code"] == "L1Y0"
    assert at.get_performance("YTD")["period"]["code"] == "X"
    assert at.get_performance("2025")["period"]["code"] == "Y2025"
    assert at.get_performance("2024-01-01..2024-12-31")["period"]["code"] == "F2024-01-01_2024-12-31"


def test_max_period_starts_before_the_first_transaction():
    oldest = min(t["datetime"][:10] for t in rt.get_transactions(limit=1000)["transactions"])
    assert at.get_performance("MAX")["period"]["start"] < oldest


def test_unknown_period_explains_the_vocabulary():
    with pytest.raises(ToolError, match="1M, 3M"):
        at.get_performance("last tuesday")


def test_scopes_need_a_name():
    with pytest.raises(ToolError, match="needs a name"):
        at.get_performance("1Y", scope="portfolio")
    with pytest.raises(ToolError, match="unknown scope"):
        at.get_performance("1Y", scope="galaxy")


def test_performance_matches_current_holdings():
    performance = at.get_performance("1Y")
    assert performance["end_value"] == pytest.approx(
        rt.get_holdings()["total_assets"]["amount"], abs=0.01
    )


def test_period_with_no_data_returns_null_not_nan():
    """Java yields NaN for an empty period; JSON cannot carry it."""
    empty = at.get_performance("1990")
    json.dumps(empty)
    assert empty["ttwror"] is None or isinstance(empty["ttwror"], float)


def test_snapshot_categories_form_a_waterfall():
    snapshot = at.get_performance_snapshot("1Y")
    by_type = {category["type"]: category for category in snapshot["categories"]}
    contributions = sum(
        category["value"] for category in snapshot["categories"] if category["sign"] != "="
    )
    assert contributions == pytest.approx(by_type["FINAL_VALUE"]["value"], abs=0.01)
    assert by_type["FEES"]["value"] <= 0 and by_type["TAXES"]["value"] <= 0


def test_absolute_delta_excludes_transfers():
    """PP's absolute performance is the change in value that deposits and removals did not cause."""
    snapshot = at.get_performance_snapshot("1Y")
    by_type = {category["type"]: category["value"] for category in snapshot["categories"]}
    assert by_type["TRANSFERS"] != 0, "the demo fixture deposits monthly, so this must be exercised"
    assert snapshot["absolute_delta"]["amount"] == pytest.approx(
        by_type["FINAL_VALUE"] - by_type["INITIAL_VALUE"] - by_type["TRANSFERS"], abs=0.01
    )


def test_security_performance_covers_held_positions():
    securities = at.get_security_performance("1Y")
    held = [h for h in rt.get_holdings()["holdings"] if h["kind"] == "SECURITY"]
    assert securities["count"] == len(held)
    assert securities["count"] <= at.get_security_performance("1Y", include_closed=True)["count"]
    json.dumps(securities)


def test_security_market_values_sum_to_invested_total():
    securities = at.get_security_performance("1Y")
    invested = sum(row["market_value"]["amount"] for row in securities["securities"])
    portfolios = sum(p["value"]["amount"] for p in rt.list_portfolios()["portfolios"])
    assert invested == pytest.approx(portfolios, abs=0.01)


def test_cost_method_is_validated():
    with pytest.raises(ToolError, match="FIFO or MOVING_AVERAGE"):
        at.get_security_performance("1Y", cost_method="LIFO")


def test_dividends_match_the_transaction_ledger():
    dividends = at.get_dividends("MAX")
    ledger = rt.get_transactions(type="DIVIDENDS", limit=1000)
    assert dividends["payments"] == ledger["total_matching"]
    base = dividends["currency"]
    in_base = sum(t["amount"]["amount"] for t in ledger["transactions"] if t["amount"]["currency"] == base)
    in_base_only = at.get_dividends("MAX", security="XX0000000001")["total_net"] + at.get_dividends(
        "MAX", security="XX0000000003"
    )["total_net"]
    assert in_base_only == pytest.approx(in_base, abs=0.01)


def test_foreign_dividends_are_converted_to_the_base_currency():
    """The demo fixture pays USD dividends into a USD account; totals must not mix currencies."""
    usd = at.get_dividends("MAX", security="XX0000000005")
    raw = [
        t for t in rt.get_transactions(type="DIVIDENDS", security="XX0000000005", limit=1000)["transactions"]
    ]
    assert raw and {t["amount"]["currency"] for t in raw} == {"USD"}
    assert usd["currency"] == "EUR"
    assert usd["payments"] == len(raw)
    assert usd["total_net"] != pytest.approx(sum(t["amount"]["amount"] for t in raw), abs=0.01)
    assert 0 < usd["total_net"]


def test_dividends_group_by_security():
    by_security = at.get_dividends("MAX", by="security")
    assert by_security["grouped_by"] == "security"
    assert by_security["total_net"] == pytest.approx(at.get_dividends("MAX")["total_net"], abs=0.01)
    with pytest.raises(ToolError, match="year"):
        at.get_dividends("MAX", by="quarter")


def test_series_ends_where_the_performance_figure_does():
    series = at.get_performance_series("1Y")
    performance = at.get_performance("1Y")
    assert series["series"][-1]["ttwror"] == pytest.approx(performance["ttwror"], abs=1e-6)
    assert series["series"][-1]["date"] == performance["period"]["end"]
    assert series["points"] < series["available_points"], "monthly sampling must thin the curve"


def test_series_sampling_is_validated():
    assert at.get_performance_series("1Y", sampling="daily")["points"] > 300
    with pytest.raises(ToolError, match="sampling"):
        at.get_performance_series("1Y", sampling="hourly")
