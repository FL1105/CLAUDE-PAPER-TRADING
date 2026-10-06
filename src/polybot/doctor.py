"""`polybot doctor`: check this machine is ready, in plain language.

Checks: Python version, packages, config, output folder + SQLite, disk space,
clock accuracy, reachability of each public Polymarket API, whether the API probe
has run, and the state of the local database / forward run (heartbeat, halts).

Failure mode guarded against: a 30-day forward run silently not running or
producing bad evidence because of a setup problem nobody noticed. Every check
prints OK / WARN / FAIL with what to do; FAIL makes the command exit non-zero.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from polybot.config import Config, config_hash, load_config

OK, WARN, FAIL = "OK", "WARN", "FAIL"
REQUIRED = ("httpx", "numpy", "pandas", "scipy", "pydantic", "typer", "yaml")
OPTIONAL = ("duckdb", "pyarrow")


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def _python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    return Check(
        OK if ok else FAIL,
        "Python",
        f"{platform.python_version()} ({sys.executable})",
        "" if ok else "Install Python 3.11 or newer (Windows: winget install Python.Python.3.12)",
    )


def _packages() -> list[Check]:
    out = []
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    out.append(
        Check(
            FAIL if missing else OK,
            "Required packages",
            "missing: " + ", ".join(missing) if missing else "all installed",
            'Run: pip install -e ".[dev]"' if missing else "",
        )
    )
    opt = [m for m in OPTIONAL if importlib.util.find_spec(m) is None]
    out.append(
        Check(
            WARN if opt else OK,
            "History import packages",
            "missing: " + ", ".join(opt) if opt else "duckdb + pyarrow installed",
            'Only needed for `polybot history`: pip install -e ".[history]"' if opt else "",
        )
    )
    return out


def _config(path: Path | None) -> tuple[Check, Config | None]:
    try:
        cfg = load_config(path)
    except Exception as exc:  # reported, not swallowed: this is the check's result
        return Check(FAIL, "Config", f"could not load: {exc}", "Fix config/default.yaml"), None
    return Check(OK, "Config", f"version {cfg.config_version}, hash {config_hash(cfg)[:12]}"), cfg


def _storage(root: Path, cfg: Config) -> list[Check]:
    out = []
    out_dir = root / "out"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=out_dir, suffix=".sqlite", delete=False) as fh:
            tmp = Path(fh.name)
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.close()
        tmp.unlink(missing_ok=True)
        for extra in (tmp.with_suffix(".sqlite-wal"), tmp.with_suffix(".sqlite-shm")):
            extra.unlink(missing_ok=True)
        out.append(Check(OK, "Output folder + SQLite", f"{out_dir} is writable"))
    except Exception as exc:  # reported as a FAIL with a fix
        out.append(
            Check(
                FAIL,
                "Output folder + SQLite",
                str(exc),
                "Run from a folder you own (not Program Files / a synced read-only drive)",
            )
        )
    free_gb = shutil.disk_usage(out_dir if out_dir.exists() else root).free / 1e9
    out.append(
        Check(
            OK if free_gb >= 5 else WARN,
            "Free disk",
            f"{free_gb:.1f} GB",
            "" if free_gb >= 5 else "Keep at least 5 GB free for the database and logs",
        )
    )
    return out


def _network(
    cfg: Config, timeout: float, transport: httpx.BaseTransport | None = None
) -> list[Check]:
    targets: tuple[tuple[str, str, dict[str, int]], ...] = (
        ("Gamma API (markets)", f"{cfg.api.gamma_base_url}/markets", {"limit": 1}),
        ("Data API (trades)", f"{cfg.api.data_base_url}/trades", {"limit": 1}),
        ("CLOB API (books)", f"{cfg.api.clob_base_url}/time", {}),
    )
    out: list[Check] = []
    skew: float | None = None
    with httpx.Client(
        timeout=timeout,
        headers={"User-Agent": cfg.api.user_agent},
        follow_redirects=True,
        transport=transport,
    ) as c:
        for name, url, params in targets:
            t0 = time.monotonic()
            try:
                r = c.get(url, params=params)
            except httpx.TimeoutException:
                out.append(
                    Check(FAIL, name, f"timed out after {timeout:.0f}s", "Check your connection")
                )
                continue
            except httpx.ProxyError as exc:
                out.append(
                    Check(
                        FAIL,
                        name,
                        f"blocked by a proxy: {exc}",
                        "A work/school network or proxy blocks this host. "
                        "Try another network, or ask for *.polymarket.com to be allowed.",
                    )
                )
                continue
            except httpx.TransportError as exc:
                out.append(
                    Check(
                        FAIL,
                        name,
                        f"cannot connect: {exc}",
                        "Check internet / firewall / VPN. Polymarket may block some regions.",
                    )
                )
                continue
            ms = (time.monotonic() - t0) * 1000
            if r.status_code == 200:
                out.append(Check(OK, name, f"HTTP 200 in {ms:.0f} ms"))
            elif r.status_code in (403, 451):
                out.append(
                    Check(
                        FAIL,
                        name,
                        f"HTTP {r.status_code}: access refused",
                        "Polymarket refuses this network/region. Public data only; "
                        "check its terms for your location.",
                    )
                )
            else:
                out.append(
                    Check(
                        WARN,
                        name,
                        f"HTTP {r.status_code} in {ms:.0f} ms",
                        "Unexpected status; run `polybot probe` for details",
                    )
                )
            if skew is None and r.headers.get("date"):
                try:
                    skew = time.time() - parsedate_to_datetime(r.headers["date"]).timestamp()
                except (TypeError, ValueError):
                    skew = None
    if skew is not None:
        bad = abs(skew) > 5
        out.append(
            Check(
                WARN if bad else OK,
                "Clock",
                f"{skew:+.1f}s vs server",
                "Turn on automatic time sync (Windows: Settings > Time > Sync now)" if bad else "",
            )
        )
    return out


def _state(root: Path, cfg: Config) -> list[Check]:
    out = []
    live = root / "tests" / "fixtures" / "live"
    has_probe = live.exists() and any(live.glob("*.json"))
    out.append(
        Check(
            OK if has_probe else WARN,
            "API probe",
            "live fixtures saved" if has_probe else "not run yet",
            "" if has_probe else "Run: polybot probe",
        )
    )
    db = root / cfg.storage.db_path
    if not db.exists():
        out.append(
            Check(
                WARN,
                "Database",
                "not created yet",
                "Run: polybot backfill --days 90 (then polybot backtest)",
            )
        )
        return out
    try:
        # as_uri() percent-escapes '#', '?' and '%' in folder names; ro = never modify
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            n = {
                t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("trades", "markets", "paper_fills", "paper_settlements", "frozen_sets")
            }
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
    except sqlite3.Error as exc:
        out.append(
            Check(
                FAIL,
                "Database",
                f"cannot read {db}: {exc}",
                "Stop other polybot processes; if it persists, restore from a backup",
            )
        )
        return out
    out.append(
        Check(
            OK if n["trades"] else WARN,
            "Database",
            f"{n['trades']} trades, {n['markets']} markets, {n['paper_fills']} paper fills, "
            f"{n['paper_settlements']} settled",
            "" if n["trades"] else "Run: polybot backfill --days 90",
        )
    )
    if "forward_start_ts" in meta:
        days = (time.time() - int(meta["forward_start_ts"])) / 86_400
        out.append(
            Check(
                OK, "Forward run", f"started {days:.1f} days ago; {n['frozen_sets']} epochs frozen"
            )
        )
        same = meta.get("forward_config_hash") == config_hash(cfg)
        out.append(
            Check(
                OK if same else FAIL,
                "Config unchanged since start",
                "yes" if same else "NO: gate 12 will fail",
                "" if same else "Restore the original config, or start a new database",
            )
        )
        beat = json.loads(meta["heartbeat"]) if "heartbeat" in meta else None
        if beat is None:
            out.append(Check(WARN, "Heartbeat", "none yet", "Is `polybot paper` running?"))
        else:
            age = time.time() - beat["ts"]
            stale = age > 3 * cfg.paper.heartbeat_interval_s + 60
            out.append(
                Check(
                    WARN if stale else OK,
                    "Heartbeat",
                    f"{age:.0f}s ago",
                    "The loop is not running: restart it (see docs/RUNBOOK.md)" if stale else "",
                )
            )
        if "halted" in meta:
            out.append(
                Check(
                    WARN,
                    "Kill switch",
                    f"HALTED: {meta['halted']}",
                    'Review `polybot report`, then: polybot resume --note "..."',
                )
            )
    else:
        out.append(Check(WARN, "Forward run", "not started", "When ready: polybot paper"))
    return out


def run(
    root: Path,
    config: Path | None = None,
    *,
    offline: bool = False,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    checks = [_python(), *_packages()]
    c, cfg = _config(config)
    checks.append(c)
    if cfg is None:
        return checks
    checks += _storage(root, cfg)
    if not offline:
        checks += _network(cfg, timeout, transport)
    checks += _state(root, cfg)
    return checks


def render(checks: list[Check]) -> str:
    """Plain ASCII so it prints the same in every terminal (Windows included)."""
    lines = []
    for c in checks:
        lines.append(f"[{c.status:<4}] {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"       -> {c.fix}")
    n_fail = sum(c.status == FAIL for c in checks)
    n_warn = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append(
        f"{n_fail} problem(s), {n_warn} warning(s)." if n_fail or n_warn else "All checks passed."
    )
    return "\n".join(lines)
