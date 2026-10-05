#!/usr/bin/env python3
"""Probe the public Polymarket APIs once and record what they really return.

Equivalent to `polybot probe`. Run this FIRST on a machine with network access:

    python scripts/probe_apis.py

Writes tests/fixtures/live/*.json, out/probe_report.json and the auto-generated
section of docs/ASSUMPTIONS.md.
"""

from polybot.cli import app

if __name__ == "__main__":
    app(["probe"])
