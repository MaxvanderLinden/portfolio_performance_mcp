# pp-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client)
**read-only** access to your [Portfolio Performance](https://www.portfolio-performance.info)
file, so you can ask questions like:

- *"How did my portfolio do this year, and how much of that was dividends?"*
- *"Which position has the worst IRR since I bought it?"*
- *"Show my allocation by region and how much is unclassified."*
- *"List every fee I paid in 2025."*

It loads your file with Portfolio Performance's own Java classes, so returns, IRR, cost basis and
allocations are the same numbers the app shows. It never writes anything — see [SAFETY.md](SAFETY.md)
for how it is kept away from your real file.

> Unofficial project, not affiliated with Portfolio Performance. Not financial advice.

## How your data is handled

- The server reads a **read-only copy** of your file (a *replica*), never the file you edit in the app.
- It runs locally; the server itself does not send your data anywhere.
- **What the tools return is sent to your AI provider** as part of the conversation, like anything
  else you share with it. Free-text notes stay out of responses unless a tool is asked for them.

## Requirements

- **Portfolio Performance** installed (tested with 0.87). Its core jar and its bundled Java
  runtime are used directly, so no separate Java install is needed.
- **Python 3.11+**, built for the **same CPU architecture** as Portfolio Performance (an Intel
  Python cannot load an Apple Silicon Java runtime, and vice versa).
- Your portfolio saved as **XML** (`.xml`). The binary and encrypted formats are not supported.

Tested on macOS. Linux and Windows install layouts are detected, but have not been tested yet —
reports welcome.

## Install

```bash
git clone https://github.com/MaxvanderLinden/portfolio_performance_mcp.git
cd portfolio_performance_mcp
python3 -m venv .venv
./.venv/bin/pip install -e .
```

## Make a replica of your file

```bash
PP_LIVE_FILE="/path/to/your/portfolio.xml" ./scripts/refresh-replica.sh
```

This copies your file to `~/pp-mcp/replica/portfolio.xml` and makes the copy read-only. Re-run it
after saving in the app to pick up your changes; the server reloads the replica automatically,
no restart needed. (On Windows, copy the file yourself and mark the copy read-only.)

Check that it loads and that the totals match the app:

```bash
./.venv/bin/python -m pp_mcp
```

## Connect it to Claude

**Claude Code:**

```bash
claude mcp add portfolio -- "$PWD/.venv/bin/pp-mcp"
```

**Claude Desktop** — add this to `claude_desktop_config.json` (Settings → Developer → Edit Config),
using the absolute path to your clone:

```json
{
  "mcpServers": {
    "portfolio": {
      "command": "/absolute/path/to/portfolio_performance_mcp/.venv/bin/pp-mcp"
    }
  }
}
```

Add an `"env": {...}` block to that entry if you need any of the settings below.

## Configuration

| variable | default | meaning |
| --- | --- | --- |
| `PP_FILE` | `~/pp-mcp/replica/portfolio.xml` | the replica the server reads |
| `PP_APP_PATH` | macOS `/Applications/PortfolioPerformance.app`, Linux `/opt/portfolio`, Windows `%ProgramFiles%\PortfolioPerformance` | the Portfolio Performance install |
| `JAVA_HOME` | unset | a Java 21+ runtime, used only if none is bundled with Portfolio Performance |
| `PP_LIVE_FILE` | none (required) | your real file; read by `refresh-replica.sh` only |
| `PP_ALLOW_WRITABLE_SOURCE` | unset | set to `1` to skip the read-only check (not recommended) |

The server **refuses to start on a writable `PP_FILE`**. That catches the dangerous mistake of
pointing it at your real file instead of the replica.

## Tools

| tool | returns |
| --- | --- |
| `list_securities` | securities with latest price, `include_retired` optional |
| `list_accounts` / `list_portfolios` | current cash balances / market values |
| `get_holdings` | every position and account with share of total, optionally on a past date |
| `get_transactions` | filtered by security, account, portfolio, type, date range |
| `get_prices` | history plus the latest quote, with a summary of the full range |
| `list_taxonomies` / `get_allocation` | classification schemes and the breakdown by one |
| `get_performance` | TTWROR, annualized, IRR, max drawdown, volatility for a period and scope |
| `get_performance_snapshot` | the waterfall: initial value, capital gains, earnings, fees, taxes, final value |
| `get_security_performance` | per-security IRR, TTWROR, realized/unrealized gains, dividends, cost |
| `get_dividends` | dividend income in the base currency, grouped by year or security |
| `get_performance_series` | the TTWROR curve, sampled monthly/weekly/daily |

**Periods** accept aliases (`1M`, `3M`, `6M`, `1Y`, `3Y`, `5Y`, `YTD`, `MTD`, `QTD`, `MAX`),
a calendar year (`2025`), a range (`2024-01-01..2024-12-31`), or Portfolio Performance's own codes
(`L1Y0`, `D30`, `T30`, `S2020-01-01`); responses echo the resolved dates.

**Rates are fractions:** `0.223` is 22.3%. `irr` and `ttwror_annualized` are annualized; `ttwror` is
cumulative over the period. **Scopes:** `client`, `portfolio`, `account`, `security`, `classification`.

Every tool is marked read-only. Large responses are capped and say so (`truncated`,
`total_matching`), so narrow with date ranges rather than raising `limit`.

## Troubleshooting

| message | fix |
| --- | --- |
| `No portfolio replica at …` | run `refresh-replica.sh`, or set `PP_FILE` |
| `Replica … is writable` | `PP_FILE` points at your real file, or the copy lost its read-only flag; re-run `refresh-replica.sh` |
| `Portfolio Performance not found at …` | set `PP_APP_PATH` to your install folder |
| `No Java runtime found` / `Could not start the Java runtime` | usually a Python/Java architecture mismatch; use a Python built for the same architecture as Portfolio Performance, or set `JAVA_HOME` |
| `… dataless iCloud placeholder` | the file is not downloaded yet; open it in Finder, or run the `brctl download` command shown |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests, the demo portfolio and design notes.

## License

MIT — see [LICENSE](LICENSE). This project loads the Portfolio Performance core jar from your own
installation at runtime and does not redistribute it.
