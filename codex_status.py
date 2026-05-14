"""Spawn the codex TUI, send `/status`, capture and parse its output.

PTY reads on Windows (pywinpty/winpty ReadFile) are unconditionally blocking, so
the read loop runs on a background thread that pushes chunks into a queue. The
main thread enforces deadlines on `queue.get()` instead.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>M]")
BOX_CHARS = "│╭╮╰╯─━┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼"

REPO_ROOT = Path(__file__).resolve().parent
TMP_DIR = REPO_ROOT / "tmp"
TMP_DIR.mkdir(exist_ok=True)


class CodexNotInstalled(RuntimeError):
    pass


class CodexNotLoggedIn(RuntimeError):
    pass


class CodexStatusError(RuntimeError):
    pass


@dataclass
class StatusSnapshot:
    captured_at: datetime
    account: Optional[str]
    model: Optional[str]
    weekly_pct_left: int
    weekly_pct_used: int
    weekly_next_reset: datetime
    raw_text: str


def find_codex() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise CodexNotInstalled(
            "codex CLI not found in PATH. Install with: npm install -g @openai/codex"
        )
    return exe


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _type(proc, text: str, per_char_delay: float = 0.02) -> None:
    """Send `text` to the PTY one character at a time. Codex's ConPTY reader
    silently drops bursts that arrive before its input widget is mounted, so
    human-paced typing is the safe default for slash commands."""
    for ch in text:
        proc.write(ch if sys.platform == "win32" else ch.encode("utf-8"))
        time.sleep(per_char_delay)


def _spawn(cmd: list[str]):
    if sys.platform == "win32":
        from winpty import PtyProcess  # type: ignore

        return PtyProcess.spawn(cmd, dimensions=(40, 200))
    else:
        from ptyprocess import PtyProcess  # type: ignore

        return PtyProcess.spawn(cmd, dimensions=(40, 200))


class _PtyDrainer:
    """Background thread that drains PTY output into a queue. EOF is signalled with None."""

    def __init__(self, proc):
        self.proc = proc
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data = self.proc.read(4096)
            except EOFError:
                self.q.put(None)
                return
            except Exception:
                self.q.put(None)
                return
            if data is None:
                self.q.put(None)
                return
            if not data:
                # ptyprocess may return b"" on transient empty reads; back off briefly.
                time.sleep(0.02)
                continue
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            self.q.put(data)

    def read_for(self, seconds: float, until: Optional[str] = None, grace: float = 0.4) -> str:
        """Accumulate output up to `seconds`. If `until` substring appears in the
        ANSI-stripped buffer, drain `grace` more seconds and return."""
        deadline = time.monotonic() + seconds
        chunks: list[str] = []
        stripped = ""
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                chunk = self.q.get(timeout=min(remaining, 0.3))
            except queue.Empty:
                continue
            if chunk is None:
                break  # EOF
            chunks.append(chunk)
            stripped += _strip_ansi(chunk)
            if until and until in stripped:
                grace_deadline = time.monotonic() + grace
                while time.monotonic() < grace_deadline:
                    try:
                        more = self.q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if more is None:
                        break
                    chunks.append(more)
                break
        return "".join(chunks)

    def stop(self):
        self._stop.set()


def fetch_status_raw(
    ready_seconds: float = 4.0,
    status_seconds: float = 6.0,
    capture_log: bool = True,
) -> str:
    """Launch codex, wait for the TUI to be input-ready, send `/status`, capture.

    Two timing knobs:
      - `ready_seconds`: ceiling for the wait between the intro box and the
        moment the placeholder text appears (the latter is our "MCP server boot
        finished, input is live" signal).
      - `status_seconds`: ceiling for the wait between sending `/status` and the
        `Weekly limit` line of the status panel being rendered.
    """
    codex = find_codex()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")

    proc = _spawn([codex])
    drainer = _PtyDrainer(proc)
    capture_parts: list[str] = []

    log_path: Optional[Path] = None
    if capture_log:
        log_path = TMP_DIR / f"capture-{datetime.now():%Y%m%d-%H%M%S}.log"

    try:
        # Wait for the placeholder text that codex shows once it's done booting
        # (MCP servers, etc.) and is ready to accept input. Any one of the rotating
        # placeholders is fine — we just look for "@filename" or "Summarize"
        # which are stable substrings across them.
        ready = drainer.read_for(ready_seconds, until="@filename", grace=0.15)
        if "@filename" not in _strip_ansi(ready):
            # Try a few alternative placeholder phrases.
            for alt in ("Summarize recent commits", "Explain this codebase", "Tip:"):
                if alt in _strip_ansi(ready):
                    break
            else:
                # Last resort: a fixed settle wait, hoping the TUI is now live.
                more = drainer.read_for(1.5, until=None, grace=0.0)
                ready += more
        capture_parts.append(ready)

        # Login sanity check.
        ready_stripped = _strip_ansi(ready)
        if ("Sign in" in ready_stripped and "›" not in ready_stripped) or (
            "Not signed in" in ready_stripped
        ):
            raise CodexNotLoggedIn("codex requires login. Run: codex login")

        # First /status: codex shows the box but the Limits section often says
        # "refresh requested; run /status again shortly" because limits are
        # fetched lazily from the server.
        _type(proc, "/status\r")

        first = drainer.read_for(min(status_seconds, 4.0), until="refresh requested", grace=0.3)
        capture_parts.append(first)

        # If the first call already returned limits, we're done.
        if "Weekly limit" in _strip_ansi(first):
            full = ready + first
        else:
            # Send a second /status now that the refresh is in flight.
            _type(proc, "/status\r")
            second = drainer.read_for(status_seconds, until="Weekly limit", grace=0.4)
            capture_parts.append(second)
            full = ready + first + second
        cleaned = _strip_ansi(full)

        if "Weekly limit" not in cleaned:
            if "Sign in" in cleaned or "log in" in cleaned.lower():
                raise CodexNotLoggedIn("codex requires login. Run: codex login")
            raise CodexStatusError(
                "Did not find 'Weekly limit' in /status output within "
                f"{status_seconds:.1f}s (TUI may have changed or codex is slow)."
            )
        return full
    finally:
        drainer.stop()
        try:
            try:
                proc.write("\x03" if sys.platform == "win32" else b"\x03")  # Ctrl-C in case we left something half-typed
                time.sleep(0.05)
            except Exception:
                pass
            if proc.isalive():
                if sys.platform != "win32":
                    proc.terminate(force=True)
                else:
                    proc.terminate()
        except Exception:
            pass
        if log_path is not None:
            try:
                log_path.write_text("".join(capture_parts), encoding="utf-8")
            except OSError:
                pass
            _recycle_tmp()


_TMP_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _recycle_tmp() -> None:
    """Delete oldest files in tmp/ until total directory size is under _TMP_MAX_BYTES."""
    files = [f for f in TMP_DIR.iterdir() if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= _TMP_MAX_BYTES:
            break
        try:
            total -= f.stat().st_size
            f.unlink()
        except OSError:
            pass


_WEEKLY_RE = re.compile(
    r"Weekly limit:\s*\[[^\]]*\]\s*(\d+)\s*%\s*left\s*\(resets\s+(\d{1,2}:\d{2})\s+on\s+(\d{1,2})\s+([A-Za-z]+)",
    re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(r"Account:\s*([^\s│|]+(?:@[^\s│|]+)?)")
_MODEL_RE = re.compile(r"Model:\s*([^\n│]+?)\s{2,}")

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def _resolve_reset_datetime(now: datetime, hh_mm: str, day: int, month_name: str) -> datetime:
    month = _MONTHS.get(month_name.lower())
    if month is None:
        raise CodexStatusError(f"Unknown month in reset date: {month_name!r}")
    hour, minute = (int(x) for x in hh_mm.split(":"))
    # Pick the year that places the reset in the future (or within the last hour).
    year = now.year
    candidate = datetime(year, month, day, hour, minute)
    if candidate < now - timedelta(hours=1):
        candidate = datetime(year + 1, month, day, hour, minute)
    return candidate


def parse_status(raw: str, captured_at: Optional[datetime] = None) -> StatusSnapshot:
    captured_at = captured_at or datetime.now()
    text = _strip_ansi(raw)
    cleaned = text
    for ch in BOX_CHARS:
        cleaned = cleaned.replace(ch, " ")

    m = _WEEKLY_RE.search(cleaned)
    if not m:
        raise CodexStatusError("Could not parse Weekly limit line from /status output.")
    pct_left = int(m.group(1))
    pct_used = 100 - pct_left
    reset_dt = _resolve_reset_datetime(captured_at, m.group(2), int(m.group(3)), m.group(4))

    account_match = _ACCOUNT_RE.search(cleaned)
    account = account_match.group(1) if account_match else None

    model_match = _MODEL_RE.search(cleaned)
    model = model_match.group(1).strip() if model_match else None

    return StatusSnapshot(
        captured_at=captured_at,
        account=account,
        model=model,
        weekly_pct_left=pct_left,
        weekly_pct_used=pct_used,
        weekly_next_reset=reset_dt,
        raw_text=text,
    )
