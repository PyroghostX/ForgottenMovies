import os
import signal
import subprocess
import sys
import time
from typing import List

import config_store
from forgotten_movies import run_startup_checks


def _build_gunicorn_command() -> List[str]:
    workers = os.getenv("GUNICORN_WORKERS", "2")
    timeout = os.getenv("GUNICORN_TIMEOUT", "120")
    bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8741")
    access_log = os.getenv("GUNICORN_ACCESS_LOG", "-")
    cmd = [
        "gunicorn",
        "-w",
        workers,
        "-b",
        bind,
        "--timeout",
        timeout,
        "--access-logfile",
        access_log,
        "webapp:app",
    ]
    error_log = os.getenv("GUNICORN_ERROR_LOG")
    if error_log:
        cmd.extend(["--error-logfile", error_log])
    return cmd


def main() -> None:
    if config_store.is_setup_complete():
        run_startup_checks()
    else:
        print(
            "Forgotten Movies is not configured yet. Open the web UI to run first-time setup.",
            flush=True,
        )

    processes = []

    scheduler_proc = subprocess.Popen([sys.executable, "scheduler_runner.py"])
    processes.append(scheduler_proc)

    web_proc = subprocess.Popen(_build_gunicorn_command())
    processes.append(web_proc)

    def _forward_signal(signum, frame):
        for proc in processes:
            if proc.poll() is None:
                proc.send_signal(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _forward_signal)

    exit_code = 0
    try:
        while processes:
            for proc in list(processes):
                status = proc.poll()
                if status is None:
                    continue
                processes.remove(proc)
                exit_code = status
                for other in processes:
                    if other.poll() is None:
                        other.terminate()
                break
            time.sleep(1)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
