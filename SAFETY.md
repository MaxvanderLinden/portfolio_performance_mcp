# Not breaking the portfolio

Evidence this is built on (measured with Portfolio Performance 0.87.0 on a real 2.6 MB file):

- **Round-trip is lossless.** `ClientFactory.load()` → `save()` → `load()` reproduced the file
  byte-for-byte and an identical model fingerprint (counts of securities, accounts, portfolios,
  transactions and prices, plus total assets). PP's own serializer is deterministic. It is *not*
  the thing that will hurt you.
- **PP does not lock its data file.** With the app running there was no lock file beside the
  `.xml`; the only `.lock` is the Eclipse workspace lock, which says nothing about which file is
  open. PP keeps the client in memory and rewrites the whole file on save, so a concurrent
  MCP write is silently overwritten on the next Cmd+S — and vice versa. **This is the main risk.**

## Layer 1 — the server never learns the live file's path

The strongest control costs nothing: point `PP_FILE` at a **read-only replica**, not the original.

```bash
# refresh-replica.sh — run by launchd, or by hand before a session
cp "$PP_LIVE_FILE" ~/pp-engine-mcp/replica/portfolio.xml
chmod 444 ~/pp-engine-mcp/replica/portfolio.xml
```

A bug, a bad tool call or a confused model cannot damage a file whose path isn't in the config.
Reads lose nothing — the replica is 0.9 s to load and refreshes in a second.

## Layer 2 — the tool surface has no destructive verbs

Claude cannot call a tool that does not exist. Implement **additive operations only**:
`add_prices`, `record_buy`, `assign_classification`. Do not implement `delete_transaction`,
`delete_security`, `replace_prices`, or any bulk `update_*`. Deletion stays a human action in the
app. This is a design constraint, not a runtime check, so there is nothing to argue past.

Ship it as **two servers from one package**:

| server | entry point | registered | tools |
| --- | --- | --- | --- |
| `pp-engine-mcp` (alias `pp-read`) | `pp_engine_mcp.server:main` → `read_only()` | always | read + analytics, write module never imported |
| `pp-write` | `pp_engine_mcp.server:main_write` → `read_write()` | only when you're actively editing | adds the additive tools |

Both are implemented (`pyproject.toml`). Layer 3 lives in
`src/pp_engine_mcp/staging.py` and `scripts/promote-staged.sh`; the Layer 4 checks (process, compare
against the base hash, atomic replace, declared-delta guard) are applied at promote time and on
every staged write respectively.

## Layer 3 — staging, not live writes

Write tools produce a **proposed file plus a domain-level diff**, never touching the original:

```
record_buy(...) -> staged/portfolio.staged.xml
  record BUY of 12 IE00XXXXXXXX in Broker on 2026-09-04
      changes: {'account_tx:BUY': 1, 'portfolio_tx:BUY': 1}
      transaction:
        shares: 12
        amount: EUR 1178.4
```

Before a change is staged, the saved copy is reloaded and must show exactly the declared change
in its counters, must leave every existing record's contents unchanged, and must not raise any
consistency issue (PP's own `Checker`, run on a throwaway copy because some checks repair in
memory) that the file did not already have.

You review the changes and promote them with `scripts/promote-staged.sh`: a shell command **you**
run, not a tool the model can call. Each write returns that command with the staging folder filled
in; the live file stays a placeholder, because the server never learns its path (Layer 1). The
model is told not to run it, but that is only an instruction. What holds is that the script asks
for confirmation at a terminal.

Do not save over the staged file in PP: `pp-write` and the script refuse a staged copy they did
not produce. Do not "Save As" it over your real file either, which skips the script's checks and
its backup.

## Layer 4 — if you later write to the live file directly

All four of these, together:

1. **Refuse if PP is running.** `pgrep -f PortfolioPerformance` → abort. There is no lock file to
   check, so process presence is the only signal available.
2. **Compare-and-swap.** Record `(mtime, sha256)` at load; re-stat and re-hash immediately before
   writing. Any change means the app or iCloud touched it — abort, reload, ask again.
3. **Atomic replace with a verify step.** Never let `save()` write over the original in place:

   ```python
   ClientFactory.save(client, File(str(tmp)))          # temp file, same directory
   reloaded = ClientFactory.load(FileInputStream(str(tmp)))
   assert fingerprint(reloaded) == expected            # parses, and matches the declared delta
   os.replace(tmp, live)                               # atomic; a crash leaves the original intact
   ```

4. **Invariant guard.** Every mutation declares its expected fingerprint delta; the guard asserts
   nothing else moved and **no counter decreased**. A tool that meant to add one transaction and
   produced 119 aborts before the rename.

   ```python
   @dataclass(frozen=True)
   class Fingerprint:
       securities: int; accounts: int; portfolios: int
       taxonomies: int; transactions: int; prices: int; total_assets: str
   ```

## Layer 5 — backups you can restore from

- **Git.** The file is 2.6 MB of text; a repo of nothing but `portfolio.xml`, committed before
  every session and before every write, gives you real diffs and `git checkout` as undo.
- **PP's own** `portfolio.backup.xml` / `.backup-after-open.xml` — already there, keep them.
- **Time Machine / iCloud version history** — the only copies outside this machine's agent reach.

## Layer 6 — permissions in Claude Code

Do not put write tools on an allowlist, and never run this project with permission prompts
auto-accepted. Registering only `pp-engine-mcp` by default means the write server has to be enabled
deliberately, which is the moment you actually think about it.

## Layer 7 — treat file contents as data

Security names, account names and transaction notes are free text that flows from the file into
the model's context. Anything in there — including text a broker's PDF import put there — is
untrusted input, not instructions. Keep note fields out of tool responses unless asked for, and
never let file content trigger a write.

## The honest boundary

Guards written in the server's own Python are for catching *bugs*, not for restraining an agent:
anything with shell access can edit them. The controls that actually hold are the ones outside
that code — the live path being absent from the config, the read-only replica, PP being the only
writer of the real file, Claude Code's permission prompt, and backups on another volume.

## Recommended starting configuration

1. Read-only replica + `pp-engine-mcp` only. Use it for a week; check its numbers against the app.
2. Add `pp-write` in staging mode. Promote staged files by hand.
3. Only consider direct live writes after the four Layer-4 controls are in place and tested — and
   even then, keep `add_prices` as the first and only live-writing tool for a while.
