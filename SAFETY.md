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
cp "$PP_LIVE_FILE" ~/pp-mcp/replica/portfolio.xml
chmod 444 ~/pp-mcp/replica/portfolio.xml
```

A bug, a bad tool call or a confused model cannot damage a file whose path isn't in the config.
Reads lose nothing — the replica is 0.9 s to load and refreshes in a second.

## Layer 2 — the tool surface has no destructive verbs

Claude cannot call a tool that does not exist. Implement **additive operations only**:
`add_price`, `add_transaction`, `set_taxonomy_assignment`. Do not implement `delete_transaction`,
`delete_security`, `replace_prices`, or any bulk `update_*`. Deletion stays a human action in the
app. This is a design constraint, not a runtime check, so there is nothing to argue past.

Ship it as **two servers from one package**:

| server | entry point | registered | tools |
| --- | --- | --- | --- |
| `pp-read` | `pp_mcp.server:read_only` | always | read + analytics, write module never imported |
| `pp-write` | `pp_mcp.server:read_write` | only when you're actively editing | adds the additive tools |

## Layer 3 — staging, not live writes

Write tools produce a **proposed file plus a domain-level diff**, never touching the original:

```
add_transaction(...) -> staged/portfolio.staged.xml
  + BUY 12 IE00XXXXXXXX @ EUR 98,20 on 2026-09-04  (Broker / Broker cash)
  fingerprint: transactions 120 -> 121, all other counters unchanged
```

You review the diff, open the staged file in PP, and Save As if you agree. Full write capability,
zero blast radius. Promoting a staged file to live should be a shell command **you** run, not a
tool the model can call.

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
auto-accepted. Registering only `pp-read` by default means the write server has to be enabled
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

1. Read-only replica + `pp-read` only. Use it for a week; check its numbers against the app.
2. Add `pp-write` in staging mode. Promote staged files by hand.
3. Only consider direct live writes after the four Layer-4 controls are in place and tested — and
   even then, keep `add_price` as the first and only live-writing tool for a while.
