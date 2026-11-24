# pp-engine-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client)
**read-only** access to your [Portfolio Performance](https://www.portfolio-performance.info)
file, so you can ask questions like:

- *"How did my portfolio do this year, and how much of that was dividends?"*
- *"Which position has the worst IRR since I bought it?"*
- *"Show my allocation by region and how much is unclassified."*
- *"List every fee I paid in 2025."*

The default server never writes anything. A separate, optional server can
[stage additions](#staged-writes-optional-in-development) in a copy for you to review. See
[SAFETY.md](SAFETY.md) for how both are kept away from your real file.

It loads your file with Portfolio Performance's own Java classes, so returns, IRR, cost basis and
allocations are the same numbers the app shows.

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
git clone https://github.com/MaxvanderLinden/pp-engine-mcp.git
cd pp-engine-mcp
python3 -m venv .venv
./.venv/bin/pip install -e .
```

## Make a replica of your file

```bash
PP_LIVE_FILE="/path/to/your/portfolio.xml" ./scripts/refresh-replica.sh
```

This copies your file to `~/pp-engine-mcp/replica/portfolio.xml` and makes the copy read-only. Re-run it
after saving in the app to pick up your changes; the server reloads the replica automatically,
no restart needed. (On Windows, copy the file yourself and mark the copy read-only.)

Check that it loads and that the totals match the app:

```bash
./.venv/bin/python -m pp_engine_mcp
```

## Connect it to Claude

**Claude Code:**

```bash
claude mcp add portfolio -- "$PWD/.venv/bin/pp-engine-mcp"
```

**Claude Desktop** — add this to `claude_desktop_config.json` (Settings → Developer → Edit Config),
using the absolute path to your clone:

```json
{
  "mcpServers": {
    "portfolio": {
      "command": "/absolute/path/to/pp-engine-mcp/.venv/bin/pp-engine-mcp"
    }
  }
}
```

Add an `"env": {...}` block to that entry if you need any of the settings below.

## Configuration

| variable | default | meaning |
| --- | --- | --- |
| `PP_FILE` | `~/pp-engine-mcp/replica/portfolio.xml` | the replica the server reads |
| `PP_APP_PATH` | macOS `/Applications/PortfolioPerformance.app`, Linux `/opt/portfolio`, Windows `%ProgramFiles%\PortfolioPerformance` | the Portfolio Performance install |
| `JAVA_HOME` | unset | a Java 21+ runtime, used only if none is bundled with Portfolio Performance |
| `PP_LIVE_FILE` | none (required) | your real file; read by `refresh-replica.sh` only |
| `PP_ALLOW_WRITABLE_SOURCE` | unset | set to `1` to skip the read-only check (not recommended) |
| `PP_STAGED_DIR` | `~/pp-engine-mcp/staged` | where `pp-write` keeps staged changes; read by `promote-staged.sh` too |

The server **refuses to start on a writable `PP_FILE`**. That catches the dangerous mistake of
pointing it at your real file instead of the replica.

## Tools

| tool | returns |
| --- | --- |
| `list_securities` | securities with latest price, `include_retired` optional |
| `list_accounts` / `list_portfolios` | current cash balances / market values |
| `get_holdings` | every position and account with share of total, optionally on a past date |
| `get_transactions` | filtered by security, account, portfolio, type, date range; a buy or sell is one row with both its portfolio and cash account |
| `get_prices` | history plus the latest quote, with a summary of the full range |
| `list_taxonomies` / `get_allocation` | classification schemes and the breakdown by one |
| `get_performance` | TTWROR, annualized, IRR, max drawdown, volatility for a period and scope |
| `get_performance_snapshot` | the waterfall: initial value, capital gains, earnings, fees, taxes, final value |
| `get_security_performance` | per-security IRR, TTWROR, realized/unrealized gains, dividends, cost |
| `get_dividends` | dividend income in the base currency, grouped by year or security |
| `get_performance_series` | the TTWROR curve, sampled monthly/weekly/daily |

**Periods** accept aliases (`1M`, `3M`, `6M`, `1Y`, `2Y`, `3Y`, `5Y`, `10Y`, `YTD`, `MTD`, `QTD`,
`WTD`, `MAX`),
a calendar year (`2025`), a range (`2024-01-01..2024-12-31`), or Portfolio Performance's own codes
(`L1Y0`, `D30`, `T30`, `S2020-01-01`); responses echo the resolved dates.

**Rates are fractions:** `0.223` is 22.3%. `irr` and `ttwror_annualized` are annualized; `ttwror` is
cumulative over the period. **Scopes:** `client`, `portfolio`, `account`, `security`, `classification`.

Every tool is marked read-only. Transactions, prices and the performance series are capped and say
so (`truncated`, `total_matching`), so narrow with date ranges rather than raising `limit`.

## Staged writes (optional, in development)

A second server, `pp-write`, adds tools that **add** records (prices, transactions, …). It never
touches your real file or the replica: every change goes into a staged copy in
`~/pp-engine-mcp/staged/` (`PP_STAGED_DIR`), checked by reloading it with Portfolio Performance's own
classes. Once something is staged, its read tools show the staged copy.

When you are happy with the changes, quit Portfolio Performance and promote them yourself:

```bash
PP_LIVE_FILE="/path/to/your/portfolio.xml" ./scripts/promote-staged.sh
```

It shows every staged change with its amounts. It refuses if the app is running (or if it cannot
tell), if your file changed since the replica was taken, or if anything changed while you were
reviewing. It asks for confirmation at the terminal, backs up your file, and refreshes the
replica. On Windows, run it from Git Bash or WSL; that path is untested so far. Register `pp-write`
only while you are editing (`claude mcp add portfolio-write -- "$PWD/.venv/bin/pp-write"`), and
read [SAFETY.md](SAFETY.md) first.

| tool | does |
| --- | --- |
| `add_prices` | adds prices to one security for dates that have none; refuses if any date already has one |
| `record_cash` | `DEPOSIT`, `REMOVAL`, `INTEREST`, `INTEREST_CHARGE`, `FEES`, `FEES_REFUND`, `TAXES`, `TAX_REFUND` on one account, optionally linked to a `security`, with `tax` withheld for interest; refuses a likely duplicate unless `allow_duplicate` |
| `add_security` | a new security (name, currency, optional ISIN / ticker / WKN); refuses one whose ISIN, ticker or name is already taken |
| `record_buy` / `record_sell` | a trade in one portfolio, settled on its reference account (or `account`); `price` or `total`, plus `fees` and `taxes`; `exchange_rate` when the security trades in another currency; a sell is refused if the portfolio would hold too few shares on that date or any later one |
| `record_dividend` | a dividend on an account (`amount` is net), with `tax`, `fees` and `exchange_rate`; `shares` defaults to the shares held on that date |
| `record_cash_transfer` | moves cash between two accounts in the same currency |
| `record_share_transfer` | moves shares between two portfolios |
| `record_delivery` | shares in or out of a portfolio without cash (`direction` inbound/outbound), e.g. a transfer from another broker |
| `add_classification` | a new category in a taxonomy, at the top level or under a `parent` (id, name or path such as `Developed markets » Europe`); refuses a name its parent already has |
| `assign_classification` | puts a `security` or `account` into a category with a `weight` in percent; refuses one already in that category or weights above 100% in the taxonomy |
| `get_staged_changes` | what is staged, and whether it can still be promoted |
| `discard_staged` | moves the staged copy to `backups/` and starts over from the replica |

Write tools match securities and accounts exactly (UUID, ISIN, ticker or full name), never by a
fragment, refuse future dates, and refuse a likely duplicate unless `allow_duplicate`. Every
change must also pass Portfolio Performance's own consistency checks (the ones the app runs when
it opens a file) before it is staged. Existing records, weights included, are never changed.

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
