# Contributing

## Setup and tests

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

Dependencies are pinned in `uv.lock` (`uv sync --extra dev` reproduces the environment).

The integration tests run against `tests/fixtures/demo.xml`, a **fictional** portfolio — never your
own data. They need Portfolio Performance installed (set `PP_APP_PATH` if it is not in the default
place) and skip when it is missing, which is what happens in CI. Config, conversion,
platform-discovery and server tests run everywhere.

To change the demo portfolio, edit and re-run the generator, which builds it with Portfolio
Performance's own classes:

```bash
./.venv/bin/python tests/fixtures/make_demo.py
```

`scripts/smoke.sh` calls the installed server over stdio, the way an MCP client does.

**Never commit a real portfolio file.** `.gitignore` excludes `*.xml` except the demo fixture.

## Design notes

- **No write tools in this server.** `tests/test_server.py` fails if a write module appears. Read
  [SAFETY.md](SAFETY.md) before proposing write support.
- **The JVM starts on the main thread**, in `read_only()`. FastMCP runs sync tools in worker
  threads and starting the JVM from one of those hangs; `get_client()` attaches each worker.
  The JVM cannot be restarted in-process; `reset_for_tests()` only drops the cache.
- **Enum names, not labels.** `transaction.type` is `BUY`; `type_label` is whatever the app's
  language shows (e.g. "Aankoop" in Dutch). Never match on the label.
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
from pp_mcp import get_client, fingerprint
from pp_mcp.convert import jsonable, security

handle = get_client()
print(fingerprint(handle))
print(jsonable([security(s) for s in handle.client.getSecurities()]))
```

`handle.client` is PP's `Client` object, so anything in the core jar is reachable —
`ClientSnapshot`, `PerformanceIndex`, `ClientPerformanceSnapshot`. `handle.converter` is a
`CurrencyConverter` in the base currency, needed by most of those.
