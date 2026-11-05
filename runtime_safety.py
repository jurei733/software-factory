"""Controller-only runtime paths and process identity; never inspect credential contents."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def runtime_path(value, source=None):
    root = Path(value).expanduser().resolve()
    source = Path(source or __file__).resolve().parent
    if root == source or source in root.parents:
        raise ValueError("Keep runtime outside the factory source/package")
    if any((parent/".git").exists() or (parent/".git").is_symlink() for parent in (root, *root.parents)):
        raise ValueError("Keep runtime outside every Git checkout")
    existing = root
    while not existing.exists():
        existing = existing.parent
    result = subprocess.run(["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        raise ValueError("Keep runtime outside every Git checkout (including symlink targets)")
    return root


def process_identity(pid):
    if not isinstance(pid, int) or pid <= 1:
        return None
    if sys.platform.startswith("linux"):
        try:
            proc = Path("/proc")/str(pid)
            fields = (proc/"stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            argv = (proc/"cmdline").read_bytes().rstrip(b"\0").decode(errors="replace")
            if not argv:
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return {"pid": pid, "started": boot+":"+fields[19], "pgid": int(fields[2]), "argv": argv}
        except (OSError, ValueError, IndexError):
            return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid=", "-o", "args="],
                            capture_output=True, text=True, check=False)
    fields = result.stdout.strip().split(None, 6)
    if result.returncode or len(fields) != 7:
        return None
    return {"pid": pid, "started": " ".join(fields[:5]), "pgid": int(fields[5]), "argv": fields[6]}


def capture_identity(pid):
    previous = None
    for _ in range(20):
        current = process_identity(pid)
        if current and current == previous:
            return current
        previous = current
        time.sleep(0.05)
    return None


def owns_process(record, root):
    return (isinstance(record, dict) and record.get("runtime") == str(root) and
            record.get("pid") == record.get("pgid") and
            process_identity(record.get("pid")) == {k: record.get(k) for k in ("pid", "started", "pgid", "argv")})


def read_identity(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}  # Legacy integer pidfiles never authorize a signal.
    except (OSError, ValueError):
        return {}


def stop_owned(record, root, timeout=10):
    if not owns_process(record, root):
        return False
    os.killpg(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while owns_process(record, root) and time.monotonic() < deadline:
        try:
            os.waitpid(record["pid"], os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    if owns_process(record, root):
        os.killpg(record["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 3
        while owns_process(record, root) and time.monotonic() < deadline:
            try:
                os.waitpid(record["pid"], os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.05)
    if owns_process(record, root):
        raise RuntimeError("Service did not terminate; retain its identity receipt")
    # /proc can lose cmdline during exit before waitpid reports completion.
    # Reap our own child even when its full identity has already disappeared.
    reap_deadline = time.monotonic() + 3
    while time.monotonic() < reap_deadline:
        try:
            ended, _ = os.waitpid(record["pid"], os.WNOHANG)
            if ended:
                break
        except ChildProcessError:
            break  # A service started by an earlier helper is reaped by its current parent.
        time.sleep(0.01)
    else:
        raise RuntimeError("Owned child has not terminated; retain its identity receipt")
    return True
