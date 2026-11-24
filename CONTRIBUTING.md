# Contributing

## Submitting a change

Only the maintainer can merge, so contributions come in through a fork:

1. Fork the repository on GitHub and clone your fork.
2. Create a branch: `git checkout -b my-change`.
3. Make the change, add tests, and run the checks below.
4. Push the branch to your fork and open a pull request against `main`.

For a larger change or a new write tool, open an issue first. CI on a pull request from a fork
may wait for the maintainer's approval before it runs.

Never include real portfolio data in a commit, issue, or pull request. Use the demo portfolio
to reproduce a problem.

## Setup and tests

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

Type-check with pyright in strict mode and lint with ruff (CI runs both, plus ShellCheck):

```bash
./.venv/bin/pyright --pythonpath .venv/bin/python
./.venv/bin/ruff check .
```

Dependencies are pinned in `uv.lock` (`uv sync --extra dev` reproduces the environment).

The integration tests run against `tests/fixtures/demo.xml`, a **fictional** portfolio — never your
own data. They need Portfolio Performance installed (set `PP_APP_PATH` if it is not in the default
place) and skip when it is missing. CI runs them on Linux with pinned Portfolio Performance versions;
the other platforms run the config, conversion, lookup, script, platform-discovery and
server-import tests.

To change the demo portfolio, edit and re-run the generator, which builds it with Portfolio
Performance's own classes:

```bash
./.venv/bin/python tests/fixtures/make_demo.py
```

`scripts/smoke_read.py` calls the installed read server over stdio, the way an MCP client does,
against your configured replica. `scripts/smoke_write.py` does the same for `pp-write`, always against a
temporary copy of the demo portfolio:

```bash
./.venv/bin/python scripts/smoke_write.py
```

Do not smoke-test with `fastmcp call`: MCP clients start servers with a filtered
environment, so `PP_FILE`/`PP_STAGED_DIR` exported in your shell do not reach the server and it
falls back to your real `~/pp-engine-mcp` replica.

**Never commit a real portfolio file.** `.gitignore` excludes `*.xml` except the demo fixture.

## Design notes

- **Writes live in a separate server.** `pp-engine-mcp` never imports `write_tools` or `staging`;
  `pp-write` (`server.read_write()`) does. `tests/test_server.py` fails if building the read server
  loads either. Read [SAFETY.md](SAFETY.md) before adding a write tool.
- **Every write goes through `staging.apply()`** with the counters it adds to, e.g.
  `{"account_tx:DEPOSIT": 1}`. It saves to a temp file, reloads it, and refuses unless exactly those
  counters grew and every existing record is unchanged; then it replaces the staged copy and
  logs the change in `manifest.json`. Add only;
  identify things exactly (UUID, ISIN, exact name), never by fragment.
- **The JVM starts on the main thread**, in `read_only()`. FastMCP runs sync tools in worker
  threads and starting the JVM from one of those hangs; `get_client()` attaches each worker.
  The JVM cannot be restarted in-process; `reset_for_tests()` only drops the cache.
- **Enum names, not labels.** `transaction.type` is `BUY`; `type_label` is whatever the app's
  language shows (e.g. "Aankoop" in Dutch). Never match on the label.
- **No `Any`.** JPype's proxies are untyped, so every Java class is looked up in `jtypes.py` and
  cast to a `Protocol` listing the members we call; add a member there before calling it. Tool
  responses are `JsonObject` (`jsontypes.py`) or a `TypedDict`, which also gives MCP clients a real
  output schema.
- **Decimals, not floats.** Money, shares and quotes are fixed-point longs in Java (÷100, ÷10⁸,
  ÷10⁸, verified against `Values.*` at startup). They become `Decimal`; `jsonable()` converts at
  the boundary.
- **One currency per total.** Anything summed across transactions goes through `handle.converter`
  into the base currency first, as Portfolio Performance does.
- **Notes are off by default.** `include_notes=True` is opt-in, because free text in the file
  reaches the model's context (SAFETY.md, layer 7).
- **Reporting periods are PP's own.** Aliases map to codes that `ReportingPeriod.from()` parses,
  so the interval convention matches the app: the start date is the *baseline*, one day before
  the period proper (`Y2025` runs 2024-12-31 → 2025-12-31).
- **NaN.** Java returns NaN for a period with no data; `_percent()` turns it into `null`, because
  JSON cannot carry it.

## Using it as a library

```python
from pp_engine_mcp import get_client, fingerprint
from pp_engine_mcp.convert import jsonable, security

handle = get_client()
print(fingerprint(handle))
print(jsonable([security(s) for s in handle.client.getSecurities()]))
```

`handle.client` is PP's `Client` object, so anything in the core jar is reachable —
`ClientSnapshot`, `PerformanceIndex`, `ClientPerformanceSnapshot`. `handle.converter` is a
`CurrencyConverter` in the base currency, needed by most of those.
