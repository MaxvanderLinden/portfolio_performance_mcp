"""Staged writes: every change lands in a proposed copy of the replica, never in the live file.

See SAFETY.md, layers 3 and 4. A write goes through `apply()`, which

1. loads the staged copy (or the replica, for the first change of a session),
2. runs the mutation against PP's own model and counts what it changed,
3. saves to a temp file with PP's own `ClientFactory`, reloads that file, and checks that the
   counters moved by exactly the declared delta and that nothing decreased,
4. atomically replaces the staged copy and appends the change to a manifest.

The manifest records the SHA-256 of the replica the session started from. Once the replica is
refreshed (the app saved something new), the staged changes are based on an outdated file and
every further write is refused; `scripts/promote-staged.sh` performs the same check against the
live file before it copies anything over it.

This module is only imported by the write server. The live file's path is never known here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import threading
from collections import Counter
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from typing_extensions import NotRequired, TypedDict

from . import jtypes as j
from .bridge import ensure_thread_attached, start_jvm
from .config import Config, ConfigError, load_config
from .jsontypes import JsonObject, JsonValue

DEFAULT_STAGED_DIR = Path.home() / "pp-mcp" / "staged"
STAGED_NAME = "portfolio.staged.xml"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


class StagingError(Exception):
    """A staged write was refused; the staged copy is unchanged."""


# TypedDict from typing_extensions: pydantic (via FastMCP) requires it before Python 3.12.


class Change(TypedDict):
    at: str
    description: str
    delta: dict[str, int]
    details: JsonObject


class Manifest(TypedDict):
    version: int
    replica: str
    base_sha256: str
    base_census: dict[str, int]
    created: str
    staged_sha256: str
    changes: list[Change]


class ApplyResult(TypedDict):
    staged_file: str
    change: Change
    pending_changes: int
    note: str


class Status(TypedDict):
    staged: bool
    pending_changes: int
    changes: list[Change]
    staged_file: NotRequired[str]
    created: NotRequired[str]
    up_to_date_with_replica: NotRequired[bool]
    unmodified: NotRequired[bool]


class DiscardResult(TypedDict):
    discarded: bool
    note: NotRequired[str]
    changes_set_aside: NotRequired[int]
    moved_to: NotRequired[str]


@dataclass(frozen=True)
class StagingArea:
    replica: Path
    directory: Path
    app_path: Path

    @property
    def staged(self) -> Path:
        return self.directory / STAGED_NAME

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST_NAME

    @property
    def tmp(self) -> Path:
        """Where a write is saved and verified before it replaces the staged copy."""
        return self.directory / f".{STAGED_NAME}.tmp"


def staging_area(config: Config | None = None, env: Mapping[str, str] | None = None) -> StagingArea:
    """Where staged changes go: `PP_STAGED_DIR`, default `~/pp-mcp/staged`."""
    env = os.environ if env is None else env
    config = config or load_config(env)
    directory = Path(env.get("PP_STAGED_DIR", str(DEFAULT_STAGED_DIR))).expanduser()

    replica_dir = config.source.resolve().parent
    if directory.resolve() == replica_dir:
        raise ConfigError(
            f"PP_STAGED_DIR must not be the replica's folder ({replica_dir}); "
            "staged changes are kept apart from the copy they were made from."
        )
    if directory.exists() and not directory.is_dir():
        raise ConfigError(f"PP_STAGED_DIR must be a folder, got {directory}.")
    directory.mkdir(parents=True, exist_ok=True)
    return StagingArea(replica=config.source, directory=directory, app_path=config.app_path)


# --- counting --------------------------------------------------------------------------------


def census(client: j.Client) -> dict[str, int]:
    """Counters for everything a write could add. Transactions are counted per type and side,
    so a tool that meant to add a DEPOSIT cannot quietly add a BUY as well."""
    counts: Counter[str] = Counter()
    securities = list(client.getSecurities())
    accounts = list(client.getAccounts())
    portfolios = list(client.getPortfolios())
    taxonomies = list(client.getTaxonomies())

    counts["securities"] = len(securities)
    counts["accounts"] = len(accounts)
    counts["portfolios"] = len(portfolios)
    counts["taxonomies"] = len(taxonomies)
    counts["prices"] = sum(s.getPrices().size() for s in securities)
    for taxonomy in taxonomies:
        classifications = list(taxonomy.getAllClassifications())
        counts["classifications"] += len(classifications)
        counts["assignments"] += sum(c.getAssignments().size() for c in classifications)
    for account in accounts:
        for transaction in account.getTransactions():
            counts[f"account_tx:{transaction.getType().name()}"] += 1
    for portfolio in portfolios:
        for transaction in portfolio.getTransactions():
            counts[f"portfolio_tx:{transaction.getType().name()}"] += 1
    return dict(counts)


def diff(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    """Non-zero changes between two censuses."""
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0) for k in sorted(keys) if after.get(k, 0) != before.get(k, 0)}


def check_delta(before: Mapping[str, int], after: Mapping[str, int], expected: Mapping[str, int]) -> None:
    """Raise unless `after - before` equals `expected` exactly. Pure; unit-testable."""
    negative = {k: v for k, v in expected.items() if v < 0}
    if negative:
        raise StagingError(f"write tools may only add, but this change declares removals: {negative}")
    wanted = {k: v for k, v in expected.items() if v}
    actual = diff(before, after)
    if actual != wanted:
        raise StagingError(
            f"the change did not do what it declared (expected {wanted}, got {actual}); "
            "nothing was staged"
        )


# --- file helpers ----------------------------------------------------------------------------


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _replace(source: Path, target: Path) -> None:
    """Atomic rename. Windows refuses to replace a read-only file, so lift the flag first."""
    if sys.platform == "win32" and target.exists():
        target.chmod(0o644)
    os.replace(source, target)


def _write_json(path: Path, payload: object) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    _replace(tmp, path)


def read_manifest(area: StagingArea) -> Manifest | None:
    if not area.manifest.exists():
        return None
    try:
        raw: object = json.loads(area.manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StagingError(f"cannot read {area.manifest}: {exc}") from exc
    return _parse_manifest(raw, area.manifest)


def _parse_manifest(raw: object, path: Path) -> Manifest:
    """Check the shape of a manifest read back from disk before trusting its fields."""
    if not isinstance(raw, dict):
        raise StagingError(f"{path} is not a manifest")
    data = cast("dict[str, object]", raw)
    if data.get("version") != MANIFEST_VERSION:
        raise StagingError(f"{path} has an unknown format version {data.get('version')!r}")
    for field in ("replica", "base_sha256", "created", "staged_sha256"):
        if not isinstance(data.get(field), str):
            raise StagingError(f"{path} is missing {field!r}")
    if not isinstance(data.get("base_census"), dict) or not isinstance(data.get("changes"), list):
        raise StagingError(f"{path} is missing its census or change list")
    return cast(Manifest, data)


_thread_lock = threading.RLock()


@contextmanager
def _locked(area: StagingArea) -> Generator[None, None, None]:
    """One writer at a time, also across processes (two clients may each run a pp-write)."""
    with _thread_lock:
        lock_path = area.directory / ".lock"
        with open(lock_path, "a+") as handle:
            try:
                import fcntl
            except ImportError:  # Windows: in-process lock only
                yield
                return
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _verify_staged(
    area: StagingArea,
    manifest: Manifest | None,
    replica_sha: str,
    hasher: Callable[[Path], str] = sha256,
) -> None:
    """The staged copy must still be ours, and built on the replica as it is now."""
    if not area.staged.exists():
        return
    if manifest is None:
        raise StagingError(
            f"{area.staged} exists without a manifest, so its origin is unknown. "
            f"Delete it (and nothing else) to start over."
        )
    if manifest["base_sha256"] != replica_sha:
        raise StagingError(
            "the replica was refreshed after these changes were staged, so they are based on an "
            "outdated file. Promote them first (scripts/promote-staged.sh, which will also refuse "
            "if the live file changed), or delete the files in "
            f"{area.directory} to start over."
        )
    if hasher(area.staged) != manifest["staged_sha256"]:
        raise StagingError(
            f"{area.staged} was modified outside pp-write (opened and saved in the app?). "
            f"Delete the files in {area.directory} to start over."
        )


def _load(path: Path) -> j.Client:
    stream = j.file_input_stream()(str(path))
    try:
        return j.client_factory().load(stream)
    finally:
        stream.close()


# --- the write path --------------------------------------------------------------------------


Mutation = Callable[[j.Client], Mapping[str, JsonValue]]


def apply(
    area: StagingArea,
    description: str,
    expected: Mapping[str, int],
    mutate: Mutation,
) -> ApplyResult:
    """Run `mutate(client)` against the staged copy, verify it, and stage the result.

    `expected` maps census counters to how much they must grow, e.g. `{"prices": 3}`. Every
    counter not named must stay put. `mutate` returns a JSON-able summary of what it did, which
    is recorded in the manifest and returned to the caller.
    """
    if any(v < 0 for v in expected.values()):
        check_delta({}, {}, expected)  # raises with the standard message

    start_jvm()
    ensure_thread_attached()

    with _locked(area):
        replica_sha = sha256(area.replica)
        manifest = read_manifest(area)
        _verify_staged(area, manifest, replica_sha)

        base = area.staged if area.staged.exists() else area.replica
        if not area.staged.exists():
            manifest = None  # a stale manifest without a staged file describes nothing

        client = _load(base)
        before = census(client)
        details = dict(mutate(client))

        tmp = area.tmp
        tmp.unlink(missing_ok=True)
        try:
            j.client_factory().save(client, j.file()(str(tmp)))
            after = census(_load(tmp))  # what is on disk, not what is in memory
            check_delta(before, after, expected)
            tmp.chmod(0o444)
            staged_sha = sha256(tmp)
            _replace(tmp, area.staged)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        if manifest is None:
            manifest = Manifest(
                version=MANIFEST_VERSION,
                replica=str(area.replica),
                base_sha256=replica_sha,
                base_census=before,
                created=now,
                staged_sha256=staged_sha,
                changes=[],
            )
        change = Change(at=now, description=description, delta=diff(before, after), details=details)
        manifest["changes"].append(change)
        manifest["staged_sha256"] = staged_sha
        _write_json(area.manifest, manifest)

        return {
            "staged_file": str(area.staged),
            "change": change,
            "pending_changes": len(manifest["changes"]),
            "note": "Nothing was written to your real file. Review, then run scripts/promote-staged.sh.",
        }


def status(area: StagingArea) -> Status:
    """What is staged, relative to the replica it was built on. Reads only the manifest."""
    with _locked(area):
        manifest = read_manifest(area)
        if not area.staged.exists() or manifest is None:
            return {"staged": False, "pending_changes": 0, "changes": []}
        replica_sha = sha256(area.replica)
        return {
            "staged": True,
            "staged_file": str(area.staged),
            "created": manifest["created"],
            "up_to_date_with_replica": manifest["base_sha256"] == replica_sha,
            "unmodified": sha256(area.staged) == manifest["staged_sha256"],
            "pending_changes": len(manifest["changes"]),
            "changes": manifest["changes"],
        }


def discard(area: StagingArea) -> DiscardResult:
    """Move the staged copy and manifest to backups/discarded-<time>/. Nothing is deleted, and
    neither the replica nor the live file is involved."""
    with _locked(area):
        present = [p for p in (area.staged, area.manifest) if p.exists()]
        if not present:
            return {"discarded": False, "note": "nothing was staged"}
        manifest = read_manifest(area) if area.manifest.exists() else None
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = area.directory / "backups" / f"discarded-{stamp}"
        target.mkdir(parents=True)
        for path in present:
            _replace(path, target / path.name)
        return {
            "discarded": True,
            "changes_set_aside": len(manifest["changes"]) if manifest else 0,
            "moved_to": str(target),
        }


def read_config(area: StagingArea) -> Config:
    """Config for the write server's read tools: they see the staged copy once one exists, so a
    model that just added prices can read them back. A staged copy built on an outdated replica
    is refused rather than shown as current."""
    if not area.staged.exists():
        return load_config()
    manifest = read_manifest(area)
    try:
        _verify_staged(area, manifest, _cached_sha(area.replica), hasher=_cached_sha)
    except StagingError as exc:
        raise ConfigError(str(exc)) from exc
    return Config(source=area.staged, app_path=area.app_path)


_sha_cache: dict[tuple[str, int, int], str] = {}


def _cached_sha(path: Path) -> str:
    """Reads happen on every tool call; hash a file again only when it changed."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _sha_cache:
        if len(_sha_cache) > 8:
            _sha_cache.clear()
        _sha_cache[key] = sha256(path)
    return _sha_cache[key]
