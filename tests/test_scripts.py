"""The shell scripts, the only code that touches the live file. Throwaway files; no PP needed."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash scripts need a POSIX system")

ROOT = Path(__file__).resolve().parents[1]
REFRESH = ROOT / "scripts" / "refresh-replica.sh"
PROMOTE = ROOT / "scripts" / "promote-staged.sh"
BASE, STAGED = b"<client>base</client>\n", b"<client>staged</client>\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Setup:
    def __init__(self, tmp: Path) -> None:
        self.live = tmp / "live" / "portfolio.xml"
        self.replica = tmp / "replica" / "portfolio.xml"
        self.staged_dir = tmp / "staged"
        self.staged = self.staged_dir / "portfolio.staged.xml"
        self.manifest = self.staged_dir / "manifest.json"
        self.bin = tmp / "bin"
        for folder in (self.live.parent, self.staged_dir, self.bin):
            folder.mkdir()
        self.live.write_bytes(BASE)
        self.live.chmod(0o600)
        self.staged.write_bytes(STAGED)
        self.staged.chmod(0o444)
        self.write_manifest()
        self.fake("pgrep", 1)
        self.env = {
            **os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PP_LIVE_FILE": str(self.live), "PP_FILE": str(self.replica),
            "PP_STAGED_DIR": str(self.staged_dir),
        }
        self.env.pop("WSL_DISTRO_NAME", None)

    def write_manifest(self, description: str = "record BUY") -> None:
        details = {"transaction": {"uuid": "u-1", "shares": 12, "amount": {"amount": 1178.4, "currency": "EUR"}}}
        self.manifest.write_text(json.dumps({
            "base_sha256": _sha(BASE), "staged_sha256": _sha(STAGED), "replica": str(self.replica),
            "changes": [{"at": "now", "description": description, "delta": {"x": 1}, "details": details}],
        }))

    def fake(self, name: str, status: int, output: str = "") -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\necho '{output}'\nexit {status}\n")
        path.chmod(0o755)

    def run(self, script: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(script)], env=self.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def promote_with_terminal(self, before_answer=lambda: None, answer: bytes = b"y\n") -> tuple[int, str]:
        """Run promote on a pseudo-terminal, doing something while it waits at the prompt."""
        import pty

        # Not pty.fork(): forking this process with a JVM in it can deadlock.
        take_terminal = (
            "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
            "os.execvp('bash', ['bash', sys.argv[1]])"
        )
        fd, child_end = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-c", take_terminal, str(PROMOTE)], env=self.env,
            stdin=child_end, stdout=child_end, stderr=child_end, start_new_session=True,
        )
        os.close(child_end)
        out = b""
        try:
            while b"[y/N]" not in out:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                out += chunk
            if b"[y/N]" in out:
                before_answer()
                os.write(fd, answer)
            while chunk := os.read(fd, 4096):
                out += chunk
        except OSError:  # Linux reports the closed terminal this way
            pass
        finally:
            os.close(fd)
        return process.wait(timeout=30), out.decode(errors="replace")


@pytest.fixture
def setup(tmp_path):
    return Setup(tmp_path)


# --- refresh-replica.sh ----------------------------------------------------------------------


@pytest.mark.parametrize("link", ["same", "symlink", "hardlink"])
def test_refresh_never_replaces_the_live_file(setup, tmp_path, link):
    replica = setup.live if link == "same" else tmp_path / "alias.xml"
    if link == "symlink":
        replica.symlink_to(setup.live)
    elif link == "hardlink":
        os.link(setup.live, replica)
    setup.env["PP_FILE"] = str(replica)

    result = setup.run(REFRESH)

    assert result.returncode != 0
    assert "same file" in result.stderr
    assert setup.live.read_bytes() == BASE


def test_refresh_makes_a_read_only_copy(setup):
    for _ in range(2):  # the second run replaces a read-only replica
        assert setup.run(REFRESH).returncode == 0
    assert setup.replica.read_bytes() == BASE
    assert setup.replica.stat().st_mode & 0o777 == 0o444
    assert os.listdir(setup.replica.parent) == ["portfolio.xml"]


def test_a_failed_refresh_keeps_the_old_replica(setup):
    assert setup.run(REFRESH).returncode == 0
    setup.live.write_bytes(b"newer")
    setup.fake("cp", 1)

    assert setup.run(REFRESH).returncode != 0
    assert setup.replica.read_bytes() == BASE
    assert setup.replica.stat().st_mode & 0o777 == 0o444


# --- promote-staged.sh -----------------------------------------------------------------------


def test_promote_writes_the_reviewed_file_and_keeps_permissions(setup):
    code, out = setup.promote_with_terminal()

    assert code == 0, out
    assert setup.live.read_bytes() == STAGED
    assert setup.live.stat().st_mode & 0o777 == 0o600
    assert setup.replica.read_bytes() == STAGED
    assert not setup.staged.exists() and not setup.manifest.exists()
    backups = list((setup.staged_dir / "backups").glob("*.xml"))
    assert [b.read_bytes() for b in backups] == [BASE]
    assert os.listdir(setup.live.parent) == ["portfolio.xml"]


def test_promote_keeps_a_symlinked_live_file_a_symlink(setup, tmp_path):
    link = tmp_path / "link.xml"
    link.symlink_to(setup.live)
    setup.env["PP_LIVE_FILE"] = str(link)

    code, out = setup.promote_with_terminal()

    assert code == 0, out
    assert link.is_symlink()
    assert setup.live.read_bytes() == STAGED


def test_promote_can_be_declined(setup):
    code, _ = setup.promote_with_terminal(answer=b"n\n")
    assert code != 0
    assert setup.live.read_bytes() == BASE
    assert setup.staged.exists()


@pytest.mark.parametrize("which", ["staged", "manifest", "live"])
def test_promote_refuses_what_changed_during_review(setup, which):
    def change():
        path = getattr(setup, which)
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"\n")

    code, out = setup.promote_with_terminal(change)

    assert code != 0
    assert "nothing changed" in out
    assert STAGED not in setup.live.read_bytes()
    assert os.listdir(setup.live.parent) == ["portfolio.xml"], "no temp file left behind"


def test_promote_refuses_while_pp_write_holds_the_lock(setup):
    import fcntl

    with open(setup.staged_dir / ".lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        code, out = setup.promote_with_terminal()

    assert code != 0
    assert "staging a change right now" in out
    assert setup.live.read_bytes() == BASE


def test_promote_refuses_when_the_app_starts_during_review(setup):
    code, out = setup.promote_with_terminal(lambda: setup.fake("pgrep", 0))
    assert code != 0
    assert "is running" in out
    assert setup.live.read_bytes() == BASE


@pytest.mark.parametrize("status, message", [(0, "is running"), (2, "cannot check"), (3, "cannot check")])
def test_promote_fails_closed_on_pgrep(setup, status, message):
    setup.fake("pgrep", status)
    result = setup.run(PROMOTE)
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize("output, status, message", [
    ("PortfolioPerformance.exe 1234", 0, "is running"),
    ("", 1, "cannot check"),
    ("INFO: No tasks are running", 0, "no terminal"),  # got past the check
])
def test_promote_asks_windows_under_wsl(setup, output, status, message):
    setup.env["WSL_DISTRO_NAME"] = "Ubuntu"
    setup.fake("tasklist.exe", status, output)
    assert message in setup.run(PROMOTE).stderr


def test_promote_under_wsl_needs_tasklist(setup):
    setup.env["WSL_DISTRO_NAME"] = "Ubuntu"
    if Path("/proc/version").exists() and "microsoft" in Path("/proc/version").read_text().lower():
        pytest.skip("a real WSL has tasklist.exe")
    assert "cannot check" in setup.run(PROMOTE).stderr


def test_review_shows_amounts_and_cannot_be_disguised(setup):
    setup.write_manifest("record\x1b[2K BUY\nfake line ‮")
    out = setup.run(PROMOTE).stdout
    assert "amount: EUR 1178.4" in out
    assert "shares: 12" in out
    assert "\x1b" not in out and "‮" not in out and "\nfake line" not in out
