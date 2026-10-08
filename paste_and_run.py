# Copy ALL of this into a Python window (or save as run_bot.py and run `python run_bot.py`).
# It downloads the project, installs it, and starts the paper learning bot. Paper only.
# GitHub asks you to sign in the first time (the repository is private).
import os
import pathlib
import subprocess
import sys

URL = "https://github.com/FL1105/CLAUDE-PAPER-TRADING.git"
BRANCH = "claude/polybit-probe-domains-yd5w11"
MINUTES = "10080"  # how long to run: 10080 = one week; stop any time with Ctrl+C
HOME = pathlib.Path(os.environ.get("BOT_HOME", pathlib.Path.home())) / "CLAUDE-PAPER-TRADING"


def run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True)


if not HOME.exists():
    run("git", "clone", "-b", BRANCH, URL, HOME)
else:
    run("git", "-C", HOME, "pull", "origin", BRANCH)
venv = HOME / ".venv"
bindir = venv / ("Scripts" if os.name == "nt" else "bin")
if not (bindir / ("python.exe" if os.name == "nt" else "python")).exists():
    run(sys.executable, "-m", "venv", venv)
run(bindir / ("python.exe" if os.name == "nt" else "python"), "-m", "pip", "install", "-q", "-e", f"{HOME}[dev]")
run(bindir / ("polybot.exe" if os.name == "nt" else "polybot"), "learn", "run", "--minutes", MINUTES)
