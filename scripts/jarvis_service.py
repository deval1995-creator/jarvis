"""Keep Jarvis running: the Core and the voice service, restarted if they stop.

Run it hidden (pythonw) at Windows login; see `scripts/install_autostart.cmd`.

    .venv\\Scripts\\pythonw.exe scripts\\jarvis_service.py

- Starts each service unless something already listens on its port (e.g. one
  you started by hand), so it never runs a second copy.
- If a service exits or is killed, it's started again after a short pause that
  grows (2 s up to 60 s) when it keeps failing, and resets once it stays up.
- Every 30 s it also checks that each service answers over HTTP; one that is
  running but hasn't answered for 3 checks in a row (hung) is restarted.
- The services live in a Windows job object tied to the supervisor: if the
  supervisor dies, Windows ends them too, so the next supervisor starts clean
  instead of finding half-started orphans (and loading the models twice).
- Only one supervisor runs at a time (a lock in the data folder). A Windows
  scheduled task runs this script every 30 s (`scripts/install_watchdog.ps1`),
  so if the supervisor itself dies, it's back within half a minute.
- Logs: %LOCALAPPDATA%\\Jarvis\\logs\\{service,core,voice}.log
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
DATA = Path(os.environ.get("JARVIS_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "Jarvis")
LOGS = DATA / "logs"
SERVICES = {"core": 8765, "voice": 8766}
HEALTH_URLS = {"core": "http://127.0.0.1:8765/", "voice": "http://127.0.0.1:8766/api/health"}
HEALTH_EVERY = 30  # seconds
HEALTH_GRACE = 180  # a fresh start may take this long (models loading)
HEALTH_STRIKES = 3
CREATE_NO_WINDOW = 0x08000000
STABLE_SECS = 120  # up this long counts as healthy: the restart pause resets


def kill_with_me_job():
    """A job object whose processes die when this process does."""
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)

    class LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", LIMITS), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = EXTENDED()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    return job


JOB = None


def log(msg: str) -> None:
    with open(LOGS / "service.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def answers(name: str) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URLS[name], timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def stop(p: subprocess.Popen) -> None:
    # /T: also what it started (Claude Code processes under the Core)
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)


def start(name: str) -> subprocess.Popen:
    out = open(LOGS / f"{name}.log", "a", encoding="utf-8")
    out.write(f"\n===== started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    out.flush()
    p = subprocess.Popen(
        [str(PYTHON), "-m", name], cwd=REPO, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
        creationflags=CREATE_NO_WINDOW, env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return p


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    lock = open(DATA / "service.lock", "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit(0)  # another supervisor is already keeping Jarvis up
    global JOB
    JOB = kill_with_me_job()
    # The supervisor joins its own job, so everything it starts is in it from birth
    # (including the real interpreter the venv's python.exe launches, and Claude Code).
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    if not k32.AssignProcessToJobObject(ctypes.c_void_p(JOB), ctypes.c_void_p(k32.GetCurrentProcess())):
        log("could not create the job object; services may outlive a crashed supervisor")
    log("supervisor started")

    procs: dict[str, subprocess.Popen | None] = {n: None for n in SERVICES}
    started_at: dict[str, float] = {n: 0.0 for n in SERVICES}
    pause: dict[str, float] = {n: 2.0 for n in SERVICES}
    next_try: dict[str, float] = {n: 0.0 for n in SERVICES}
    strikes: dict[str, int] = {n: 0 for n in SERVICES}
    next_health = 0.0

    while True:
        now = time.monotonic()
        for name, port in SERVICES.items():
            p = procs[name]
            if p is not None and p.poll() is None:
                if now - started_at[name] > STABLE_SECS:
                    pause[name] = 2.0
                if now >= next_health and now - started_at[name] > HEALTH_GRACE:
                    strikes[name] = 0 if answers(name) else strikes[name] + 1
                    if strikes[name] >= HEALTH_STRIKES:
                        log(f"{name} is running but not answering; restarting it")
                        strikes[name] = 0
                        stop(p)
                continue
            if p is not None:  # it was ours and it stopped
                log(f"{name} exited with code {p.returncode}; restarting in {pause[name]:.0f} s")
                procs[name] = None
                next_try[name] = now + pause[name]
                pause[name] = min(60.0, pause[name] * 2)
                continue
            if now < next_try[name]:
                continue
            if port_open(port):
                continue  # started elsewhere (by hand); leave it, check again later
            procs[name] = start(name)
            started_at[name] = now
            strikes[name] = 0
            log(f"started {name} (pid {procs[name].pid})")
        if now >= next_health:
            next_health = now + HEALTH_EVERY
        time.sleep(3)


if __name__ == "__main__":
    main()
