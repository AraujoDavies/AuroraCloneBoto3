"""
Agendador cron para o pipeline Aurora Clone (serviço Compose aurora-scheduler).

Lê SCHEDULE_CRON e SCHEDULE_COMMAND do ambiente; respeita TZ.
Evita execuções sobrepostas com flock no arquivo de lock.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from croniter import croniter

logging.basicConfig(
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s - %(levelname)s: %(message)s",
)
log = logging.getLogger("aurora-scheduler")

DEFAULT_COMMAND = "make PYTHON=python pipeline"
DEFAULT_LOCK = Path(".aurora_scheduler.lock")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_cron(expr: str) -> croniter:
    if not expr:
        raise SystemExit("SCHEDULE_CRON is empty; set a cron expression (e.g. 0 3 * * 0).")
    try:
        return croniter(expr, datetime.now())
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"Invalid SCHEDULE_CRON {expr!r}: {exc}") from exc


class PipelineLock:
    """Exclusive non-blocking flock for the duration of one pipeline run."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fp = None

    def try_acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self._path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fp.seek(0)
            self._fp.truncate()
            self._fp.write(f"pid={os.getpid()} started={datetime.now().isoformat()}\n")
            self._fp.flush()
            return True
        except BlockingIOError:
            self._fp.close()
            self._fp = None
            return False

    def release(self) -> None:
        if self._fp is None:
            return
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        finally:
            self._fp.close()
            self._fp = None


def sleep_until(target: datetime) -> None:
    while True:
        seconds = (target - datetime.now()).total_seconds()
        if seconds <= 0:
            return
        time.sleep(min(seconds, 3600.0))


def run_scheduled_command(command: str) -> int:
    log.info("Running: %s", command)
    completed = subprocess.run(
        command,
        shell=True,
        cwd=os.getcwd(),
        env=os.environ.copy(),
    )
    log.info("Finished with exit code %s", completed.returncode)
    return completed.returncode


def main() -> None:
    cron_expr = env("SCHEDULE_CRON")
    command = env("SCHEDULE_COMMAND", DEFAULT_COMMAND)
    lock_path = Path(env("SCHEDULE_LOCK_PATH", str(DEFAULT_LOCK)))

    tz = env("TZ")
    if tz:
        log.info("Timezone TZ=%s", tz)
    else:
        log.info("Timezone: system default (set TZ in Compose for clarity)")

    base = parse_cron(cron_expr)
    log.info("Schedule cron=%r command=%r lock=%s", cron_expr, command, lock_path)

    while True:
        next_run = base.get_next(datetime)
        log.info("Next run at %s", next_run.isoformat(sep=" ", timespec="seconds"))
        sleep_until(next_run)

        lock = PipelineLock(lock_path)
        if not lock.try_acquire():
            log.warning(
                "Skipping tick: previous run still holds lock (%s)",
                lock_path,
            )
            base = croniter(cron_expr, datetime.now())
            continue

        try:
            exit_code = run_scheduled_command(command)
            if exit_code != 0:
                log.error("Scheduled command failed (exit %s)", exit_code)
        finally:
            lock.release()

        base = croniter(cron_expr, datetime.now())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Scheduler stopped")
        sys.exit(0)
