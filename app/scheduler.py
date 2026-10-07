"""Runs the daily fundamentals refresh at 01:00 IST.

Deliberately stdlib-only: one daemon thread that sleeps until the next 01:00
Asia/Kolkata and then calls into ``daily_refresh``. APScheduler or Celery would
each add a dependency and a broker to a single-process app whose only scheduled
job is this one.

**This thread only runs while the API process is up.** If the machine is asleep
at 1am the slot is missed, and the next run is the following night. Two things
soften that: ``catch_up`` runs the job at start-up when the last successful run
is more than a day old, so a restart at 9am repairs a missed night; and
``scripts/daily_refresh.py`` is the same job as a standalone command, which is
the one to point Windows Task Scheduler at if the refresh has to happen whether
or not the API is running. See that script's docstring for the exact command.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.daily_refresh import IST, run_daily

log = logging.getLogger(__name__)

RUN_HOUR = 1          # 01:00 IST
RUN_MINUTE = 0
# A run is "missed" once the last success is older than this.
STALE_AFTER = timedelta(hours=26)

_thread = None
_state = {"last_run": None, "last_result": None, "running": False}


def next_run(now: datetime = None) -> datetime:
    """The next 01:00 IST strictly after ``now``."""
    now = now or datetime.now(IST)
    target = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def status() -> dict:
    """What the scheduler has done and when it next fires."""
    return {
        "enabled": _thread is not None and _thread.is_alive(),
        "timezone": "Asia/Kolkata (UTC+05:30)",
        "run_at": f"{RUN_HOUR:02d}:{RUN_MINUTE:02d} IST daily",
        "next_run": next_run().isoformat(),
        "running_now": _state["running"],
        "last_run": _state["last_run"].isoformat() if _state["last_run"] else None,
        "last_result": _state["last_result"],
    }


def _execute(reason: str) -> None:
    if _state["running"]:
        log.info("Daily refresh already in progress, skipping %s trigger", reason)
        return
    _state["running"] = True
    try:
        log.info("Daily refresh starting (%s)", reason)
        result = run_daily(progress=lambda m: log.info("  %s", m))
        _state["last_run"] = datetime.now(IST)
        _state["last_result"] = {
            "reason": reason,
            "fundamentals": {k: v for k, v in result.items() if k != "names"},
        }
        log.info("Daily refresh done: %s", _state["last_result"]["fundamentals"])
    except Exception as exc:                              # noqa: BLE001 - the loop must survive a bad night
        log.exception("Daily refresh failed: %r", exc)
        _state["last_result"] = {"reason": reason, "error": repr(exc)}
    finally:
        _state["running"] = False


def _loop() -> None:
    # Repair a missed night before settling into the schedule.
    if _should_catch_up():
        _execute("catch-up at start-up")

    while True:
        target = next_run()
        seconds = (target - datetime.now(IST)).total_seconds()
        log.info("Next fundamentals refresh at %s IST (in %.1f h)",
                 target.strftime("%Y-%m-%d %H:%M"), seconds / 3600)
        # Wake periodically rather than sleeping the whole span, so a laptop
        # suspended across 01:00 still notices on resume instead of waiting a day.
        while seconds > 0:
            threading.Event().wait(min(seconds, 900))
            seconds = (target - datetime.now(IST)).total_seconds()
        _execute("scheduled 01:00 IST")


def _should_catch_up() -> bool:
    """True when the newest stored refresh is old enough to count as missed."""
    from sqlalchemy import select, func
    from app.database import SessionLocal
    from app.models import MarketData

    db = SessionLocal()
    try:
        newest = db.execute(select(func.max(MarketData.yahoo_as_of))).scalar()
    finally:
        db.close()
    if newest is None:
        return True
    # daily_refresh stamps these in UTC; SQLite hands them back naive.
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return (datetime.now(IST) - newest.astimezone(IST)) > STALE_AFTER


def start() -> None:
    """Start the scheduler thread once per process."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="daily-refresh", daemon=True)
    _thread.start()
    log.info("Fundamentals scheduler started; next run %s", next_run().isoformat())


def run_now() -> dict:
    """Trigger the job immediately, off the request thread."""
    threading.Thread(target=_execute, args=("manual trigger",),
                     name="daily-refresh-manual", daemon=True).start()
    return status()
