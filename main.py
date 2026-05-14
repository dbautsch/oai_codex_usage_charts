"""Collect a single codex /status snapshot and refresh the weekly usage chart.

Designed to be run periodically (cron / scheduled task). The interval is irrelevant
to the math — samples are timestamped and the chart interpolates between them.

Outputs (next to this script):
    codex_usage_data.csv         — append-only timeseries
    codex_usage_statistics.png   — chart (actual + 24h-rate extrapolation)
    errors.txt                   — append-only error log; written only on failures
"""

from __future__ import annotations

import subprocess
import os
import sys
import time
import traceback
import shlex
from datetime import datetime
from pathlib import Path

from codex_status import (
    CodexNotInstalled,
    CodexNotLoggedIn,
    CodexStatusError,
    fetch_status_raw,
    find_codex,
    parse_status,
)
from usage_plot import append_sample, render_chart, render_error_png, render_login_required_png


HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "codex_usage_data.csv"
PNG_PATH = HERE / "codex_usage_statistics.png"
ERR_PATH = HERE / "errors.txt"
CRON_LOG_PATH = HERE / "cron.log"

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 3.0
CRON_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_REMOTE_PNG = "codex.png"
DEFAULT_REMOTE_ERROR_LOG = "errors.txt"


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag with a conservative parser."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _trim_log(path: Path, max_bytes: int) -> None:
    """Keep only the tail of `path` so it stays under `max_bytes`."""
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        data = path.read_bytes()
        # Trim from the start; find the first newline after the cut point.
        cut = len(data) - max_bytes
        nl = data.index(b"\n", cut)
        path.write_bytes(data[nl + 1:])
    except OSError:
        pass


_PUBLISH_ENABLED = _env_bool("CODEX_USAGE_PUBLISH", False)
_PUBLISH_REMOTE_BASE = os.getenv("CODEX_USAGE_PUBLISH_REMOTE", "").strip()
_REMOTE_PNG_NAME = os.getenv("CODEX_USAGE_REMOTE_PNG", DEFAULT_REMOTE_PNG).strip() or DEFAULT_REMOTE_PNG
_REMOTE_ERROR_NAME = os.getenv("CODEX_USAGE_REMOTE_ERROR", DEFAULT_REMOTE_ERROR_LOG).strip() or DEFAULT_REMOTE_ERROR_LOG
_SSH_KEY = os.getenv("CODEX_USAGE_SSH_KEY", "").strip()


def _build_rsync_ssh() -> str:
    parts = ["ssh"]
    if _SSH_KEY:
        parts.extend(["-i", str(Path(_SSH_KEY).expanduser())])
    parts.extend(["-o", "IdentitiesOnly=yes"])
    return " ".join(shlex.quote(part) for part in parts)


def _publish() -> None:
    """Push the PNG and error log to the remote host via rsync."""
    if not _PUBLISH_ENABLED:
        return
    if not _PUBLISH_REMOTE_BASE:
        return

    rsync_ssh = _build_rsync_ssh()
    transfers = [
        (ERR_PATH, _REMOTE_ERROR_NAME),
        (PNG_PATH, _REMOTE_PNG_NAME),
    ]
    for src, dst in transfers:
        if not src.exists():
            continue
        try:
            remote_target = f"{_PUBLISH_REMOTE_BASE.rstrip('/')}/{dst}"
            subprocess.run(
                ["rsync", "-e", rsync_ssh, str(src), remote_target],
                check=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e:
            _log_error(f"rsync {src.name} -> {dst}: exit {e.returncode}")
        except Exception as e:
            _log_error(f"rsync {src.name} -> {dst}: {e}")


def _log_error(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    with ERR_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")
    sys.stderr.write(f"[{ts}] {msg}\n")


def collect_once() -> int:
    _trim_log(CRON_LOG_PATH, CRON_LOG_MAX_BYTES)
    # Hard preconditions — these never warrant a retry.
    try:
        find_codex()
    except CodexNotInstalled as e:
        _log_error(str(e))
        render_error_png(PNG_PATH, str(e)) if not CSV_PATH.exists() else render_chart(
            PNG_PATH, CSV_PATH, error_message=str(e)
        )
        _publish()
        return 2

    last_err: str = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = fetch_status_raw()
            snapshot = parse_status(raw)
            append_sample(CSV_PATH, snapshot)
            render_chart(PNG_PATH, CSV_PATH)
            print(
                f"OK: weekly {snapshot.weekly_pct_used}% used, "
                f"resets {snapshot.weekly_next_reset:%Y-%m-%d %H:%M}"
            )
            _publish()
            return 0
        except CodexNotLoggedIn as e:
            # Login problems aren't transient — fail fast and surface a big banner
            # on the PNG (the only channel the web frontend exposes).
            _log_error(str(e))
            render_login_required_png(PNG_PATH)
            _publish()
            return 3
        except (CodexStatusError, Exception) as e:  # noqa: BLE001 — broad on purpose; we want to retry
            last_err = f"attempt {attempt}/{MAX_ATTEMPTS}: {type(e).__name__}: {e}"
            sys.stderr.write(last_err + "\n")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS)

    # All retries exhausted.
    detail = last_err or "unknown failure"
    _log_error(detail)
    if CSV_PATH.exists():
        render_chart(PNG_PATH, CSV_PATH, error_message=detail)
    else:
        render_error_png(PNG_PATH, detail)
    _publish()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(collect_once())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        _log_error("fatal: " + traceback.format_exc())
        sys.exit(1)
