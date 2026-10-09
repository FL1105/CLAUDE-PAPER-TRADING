"""Scheduling helpers: next run time, heartbeat check, webhook alerts, and a simple blocking loop.
Prefer OS schedulers (cron / launchd / Task Scheduler); see docs/RUNBOOK.md."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.data.calendar import nyse_sessions


def next_run_time(now: datetime, tz: str = "America/New_York", hhmm: str = "16:10") -> datetime:
    """Next NYSE session at hh:mm local time that is strictly after `now`."""
    z = ZoneInfo(tz)
    now = now.astimezone(z) if now.tzinfo else now.replace(tzinfo=z)
    h, m = (int(x) for x in hhmm.split(":"))
    day = now.date()
    for _ in range(14):
        sess = nyse_sessions(day, day)
        cand = datetime(day.year, day.month, day.day, h, m, tzinfo=z)
        if len(sess) and cand > now:
            return cand
        day += timedelta(days=1)
    raise RuntimeError("no session found in 14 days")


def last_completed_session(now: datetime, tz: str = "America/New_York", hhmm: str = "16:10"):
    """The most recent NYSE session whose data should exist at `now` (after hh:mm local on a session day,
    that is today; otherwise the previous session). Naive datetimes are read as `tz` wall-clock time."""
    import pandas as pd

    z = ZoneInfo(tz)
    now = now.astimezone(z) if now.tzinfo else now.replace(tzinfo=z)
    h, m = (int(x) for x in hhmm.split(":"))
    today = pd.Timestamp(now.date())
    sess = nyse_sessions(today - pd.Timedelta(days=14), today)
    if len(sess) and sess[-1] == today and (now.hour, now.minute) < (h, m):
        sess = sess[:-1]
    return sess[-1]


def heartbeat_age_hours(path: Path, now: float | None = None) -> float | None:
    if not path.exists():
        return None
    return ((now or time.time()) - path.stat().st_mtime) / 3600


def heartbeat_ok(path: Path, max_age_hours: float, now: float | None = None) -> bool:
    age = heartbeat_age_hours(path, now)
    return age is not None and age <= max_age_hours


def alert(message: str, post: Callable | None = None) -> bool:
    """POST {"text": message} to ALERT_WEBHOOK_URL if set. Never raises."""
    url = os.environ.get("ALERT_WEBHOOK_URL", "")
    if not url:
        print(f"[ALERT] {message}")
        return False
    try:
        if post is None:
            import httpx
            post = lambda u, body: httpx.post(u, json=body, timeout=10)  # noqa: E731
        post(url, {"text": f"CompoundBot: {message}"})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[ALERT FAILED] {e}: {message}")
        return False


def run_loop(job: Callable[[], None], tz: str, hhmm: str, sleep=time.sleep, now_fn=datetime.now,
             max_iter: int | None = None) -> None:
    i = 0
    while max_iter is None or i < max_iter:
        z = ZoneInfo(tz)
        nxt = next_run_time(now_fn(z), tz, hhmm)
        sleep(max((nxt - now_fn(z).astimezone(nxt.tzinfo)).total_seconds(), 0))
        try:
            job()
        except Exception as e:  # noqa: BLE001
            alert(f"daily job failed: {type(e).__name__}: {e}")
        i += 1


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
