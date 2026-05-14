"""Diagnostic: spawn codex via PTY, write raw bytes to tmp/diag-<ts>.log (UTF-8).

Stops automatically. Old diag logs are recycled (kept = newest 5).
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TMP = ROOT / "tmp"
TMP.mkdir(exist_ok=True)

LOG = TMP / f"diag-{datetime.now():%Y%m%d-%H%M%S}.log"


def recycle(prefix: str, keep: int = 5) -> None:
    files = sorted(TMP.glob(f"{prefix}-*.log"))
    for f in files[:-keep]:
        try:
            f.unlink()
        except OSError:
            pass


recycle("diag")


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


CMD_PATH = shutil.which("codex")
log(f"codex path: {CMD_PATH}")


def spawn(cmd_list):
    if sys.platform == "win32":
        from winpty import PtyProcess
        return PtyProcess.spawn(cmd_list, dimensions=(40, 200))
    else:
        from ptyprocess import PtyProcess
        return PtyProcess.spawn(cmd_list, dimensions=(40, 200))


def read_chunks(proc, secs):
    deadline = time.monotonic() + secs
    out = ""
    while time.monotonic() < deadline:
        try:
            data = proc.read(4096)
        except EOFError:
            log("[EOF]")
            break
        except Exception as e:
            log(f"[read err] {e!r}")
            time.sleep(0.1)
            continue
        if not data:
            time.sleep(0.05)
            continue
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        out += data
        log(f"[chunk len={len(data)}] {data!r}")
    return out


t0 = time.monotonic()
proc = spawn([CMD_PATH])
log(f"--- startup phase ({time.monotonic()-t0:.1f}s) ---")
startup = read_chunks(proc, 8)
log(f"--- sending /status (elapsed {time.monotonic()-t0:.1f}s) ---")
proc.write("/status\r")
log(f"--- post-status phase ---")
post = read_chunks(proc, 10)
log(f"--- terminating (elapsed {time.monotonic()-t0:.1f}s) ---")
try:
    proc.write("\x03")
except Exception:
    pass
time.sleep(0.3)
try:
    if sys.platform == "win32":
        proc.terminate()
    else:
        proc.terminate(force=True)
except Exception as e:
    log(f"term err: {e}")
log("done")
log(f"=== STARTUP FULL ===\n{startup!r}\n=== POST FULL ===\n{post!r}")
print(f"diag log: {LOG}")
