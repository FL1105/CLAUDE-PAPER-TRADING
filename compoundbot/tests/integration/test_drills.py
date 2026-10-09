from bot.monitor import drills


def test_kill_switch_drill():
    assert drills.drill_kill_switch()


def test_drawdown_halt_drill():
    assert drills.drill_drawdown_halt()


def test_crash_recovery_drill():
    assert drills.drill_crash_recovery()
