# pp-mcp

Read-only access to a local [Portfolio Performance](https://www.portfolio-performance.info) file
for MCP tools, using PP's own model classes so every number matches the application.

Thirteen read and analytics tools served over MCP, backed by a read-only replica of your file. Write access is deliberately absent — see [SAFETY.md](SAFETY.md).

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
PP_LIVE_FILE=/path/to/your/portfolio.xml ./scripts/refresh-replica.sh
./.venv/bin/python -m pp_mcp
```

`refresh-replica.sh` copies the live file to `~/pp-mcp/replica/portfolio.xml` and makes it
read-only. Run it whenever you want the MCP to see recent edits; the bridge notices the new mtime
and reloads without a restart. Re-run it after saving in the app.

## Configuration

| variable | default | meaning |
| --- | --- | --- |
| `PP_FILE` | `~/pp-mcp/replica/portfolio.xml` | the replica the bridge reads |
| `PP_APP_PATH` | `/Applications/PortfolioPerformance.app` | supplies the core jar and a Java 21 runtime |
| `PP_LIVE_FILE` | none (required) | your live `.xml` file; read by `refresh-replica.sh` only |
| `PP_ALLOW_WRITABLE_SOURCE` | unset | escape hatch; disables the read-only check |

Startup **refuses a writable `PP_FILE`**, which is what catches the dangerous mistake of pointing
the bridge at the live file (mode 644) instead of a replica (mode 444).

## Running the MCP server

```bash
./.venv/bin/pp-read
```

Register it with Claude Code:

```bash
claude mcp add portfolio -- "$PWD/.venv/bin/pp-read"
```

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
| `get_dividends` | dividend income grouped by year or security |
| `get_performance_series` | the TTWROR curve, sampled monthly/weekly/daily |

Reporting periods accept aliases (`1M`, `3M`, `6M`, `1Y`, `3Y`, `5Y`, `YTD`, `MTD`, `QTD`, `MAX`),
a calendar year (`2025`), a range (`2024-01-01..2024-12-31`), or Portfolio Performance's own codes
(`L1Y0`, `D30`, `T30`, `S2020-01-01`) — the response echoes the resolved code and dates. Rates are
fractions: `0.223` is 22.3%. `irr` and `ttwror_annualized` are annualized; `ttwror` is the
cumulative return over the period, so for a part-year window the two differ by design. Scopes: `client`, `portfolio`, `account`, `security`, `classification`.

All thirteen are annotated `read_only_hint`. Large responses are capped and report `truncated`,
`total_matching` and `showing`; narrow with date ranges rather than raising `limit`.

## Using it as a library

```python
from pp_mcp import get_client, fingerprint
from pp_mcp.convert import jsonable, security

handle = get_client()
print(fingerprint(handle))
print(jsonable([security(s) for s in handle.client.getSecurities()]))
```

`handle.client` is PP's `Client` object, so anything in the core jar is reachable —
`ClientSnapshot`, `PerformanceIndex`, `ClientPerformanceSnapshot`. `handle.converter` is a
`CurrencyConverter` in the base currency, needed by most of those.

## Notes for the next phase

- **The JVM starts on the main thread**, in `read_only()`. FastMCP runs sync tools in worker
  threads and starting the JVM from one of those hangs; `get_client()` then attaches each worker.

- **Enum names, not labels.** `transaction.type` is `BUY`; `type_label` is whatever the app's
  language shows (e.g. "Aankoop" in Dutch). Never match on the label.
- **Decimals, not floats.** Money, shares and quotes are fixed-point longs in Java (÷100, ÷10⁸,
  ÷10⁸, verified against `Values.*` at startup). They become `Decimal`; `jsonable()` converts at
  the boundary.
- **Notes are off by default.** `include_notes=True` is opt-in on every converter, because free
  text in the file reaches the model's context (SAFETY.md, layer 7).
- **Reporting periods are PP's own.** Aliases map to codes that `ReportingPeriod.from()` parses,
  so the interval convention matches the app: the start date is the *baseline*, one day before
  the period proper (`Y2025` runs 2024-12-31 → 2025-12-31).
- **NaN.** Java returns NaN for a period with no data; `_percent()` turns it into `null`, because
  JSON cannot carry it.
- **Threads.** `get_client()` attaches the calling thread to the JVM, so sync FastMCP tools are
  safe. The JVM cannot be restarted in-process; `reset_for_tests()` only drops the cache.

## Tests

```bash
./.venv/bin/python -m pytest
```

Scalar conversions run without a JVM. The integration tests load a synthetic portfolio,
`tests/fixtures/demo.xml`, so they need Portfolio Performance installed (`PP_APP_PATH`) but not
your own data; they skip if PP is not found. To regenerate the fixture:

```bash
./.venv/bin/python tests/fixtures/make_demo.py
```

## License

MIT — see [LICENSE](LICENSE). This project is not affiliated with Portfolio Performance; it loads
the core jar from your own installation at runtime and does not redistribute it.
