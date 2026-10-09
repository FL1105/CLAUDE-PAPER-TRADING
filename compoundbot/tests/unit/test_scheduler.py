import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.run import scheduler

NY = ZoneInfo("America/New_York")


def test_next_run_skips_weekend_and_holiday():
    fri_after = datetime(2024, 6, 28, 17, 0, tzinfo=NY)
    assert scheduler.next_run_time(fri_after).date().isoformat() == "2024-07-01"  # Monday
    before_holiday = datetime(2024, 7, 3, 17, 0, tzinfo=NY)  # next is Thu Jul 4 (closed) -> Fri Jul 5
    assert scheduler.next_run_time(before_holiday).date().isoformat() == "2024-07-05"
    morning = datetime(2024, 7, 1, 9, 0, tzinfo=NY)
    assert scheduler.next_run_time(morning) == datetime(2024, 7, 1, 16, 10, tzinfo=NY)


def test_heartbeat(tmp_path):
    p = tmp_path / "hb"
    assert scheduler.heartbeat_ok(p, 30) is False  # missing = unhealthy
    p.write_text("x")
    assert scheduler.heartbeat_ok(p, 30)
    old = time.time() - 40 * 3600
    os.utime(p, (old, old))
    assert not scheduler.heartbeat_ok(p, 30)


def test_alert_never_raises(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert scheduler.alert("hello") is False
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://example.invalid/hook")
    sent = []
    assert scheduler.alert("boom", post=lambda u, b: sent.append((u, b))) is True and sent
    def bad(u, b):
        raise OSError("down")
    assert scheduler.alert("boom", post=bad) is False


def test_run_loop_alerts_on_failure(monkeypatch):
    msgs = []
    monkeypatch.setattr(scheduler, "alert", lambda m, post=None: msgs.append(m))
    def job():
        raise ValueError("x")
    clock = [datetime(2024, 7, 1, 9, 0, tzinfo=NY)]
    scheduler.run_loop(job, "America/New_York", "16:10", sleep=lambda s: None,
                       now_fn=lambda tz=None: clock[0], max_iter=1)
    assert msgs and "ValueError" in msgs[0]
