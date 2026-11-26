"""Keep Jarvis running: the Core and the voice service, restarted if they stop.

Run it hidden (pythonw) at Windows login; see `scripts/install_autostart.cmd`.

    .venv\\Scripts\\pythonw.exe scripts\\jarvis_service.py

- Starts each service unless something already listens on its port (e.g. one
  you started by hand), so it never runs a second copy.
- If a service exits or is killed, it's started again after a short pause that
  grows (2 s up to 60 s) when it keeps failing, and resets once it stays up.
- Only one supervisor runs at a time (a lock in the data folder).
- Logs: %LOCALAPPDATA%\\Jarvis\\logs\\{service,core,voice}.log
"""

from __future__ import annotations

import msvcrt
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
DATA = Path(os.environ.get("JARVIS_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "Jarvis")
LOGS = DATA / "logs"
SERVICES = {"core": 8765, "voice": 8766}
CREATE_NO_WINDOW = 0x08000000
STABLE_SECS = 120  # up this long counts as healthy: the restart pause resets


def log(msg: str) -> None:
    with open(LOGS / "service.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start(name: str) -> subprocess.Popen:
    out = open(LOGS / f"{name}.log", "a", encoding="utf-8")
    out.write(f"\n===== started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    out.flush()
    return subprocess.Popen(
        [str(PYTHON), "-m", name], cwd=REPO, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
        creationflags=CREATE_NO_WINDOW, env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    lock = open(DATA / "service.lock", "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit(0)  # another supervisor is already keeping Jarvis up
    log("supervisor started")

    procs: dict[str, subprocess.Popen | None] = {n: None for n in SERVICES}
    started_at: dict[str, float] = {n: 0.0 for n in SERVICES}
    pause: dict[str, float] = {n: 2.0 for n in SERVICES}
    next_try: dict[str, float] = {n: 0.0 for n in SERVICES}

    while True:
        now = time.monotonic()
        for name, port in SERVICES.items():
            p = procs[name]
            if p is not None and p.poll() is None:
                if now - started_at[name] > STABLE_SECS:
                    pause[name] = 2.0
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
            log(f"started {name} (pid {procs[name].pid})")
        time.sleep(3)


if __name__ == "__main__":
    main()
