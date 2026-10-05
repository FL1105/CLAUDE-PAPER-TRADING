#!/usr/bin/env python3
"""Bundle dashboard.html + a report into one shareable page (out/artifact/polybot_desk.html).

The page embeds report.json and a trimmed trades.csv so it renders with no file
loading. It is meant for previews (e.g. the synthetic demo). Usage:
    python scripts/build_artifact.py [--report out/demo/report.json] [--trades out/demo/trades.csv]
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--report", default=str(ROOT / "out/demo/report.json"))
ap.add_argument("--trades", default=str(ROOT / "out/demo/trades.csv"))
ap.add_argument("--max-skipped", type=int, default=1500)
ap.add_argument("--out", default=str(ROOT / "out/artifact/polybot_desk.html"))
a = ap.parse_args()

html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
title = re.search(r"<title>.*?</title>", html, re.S).group(0)
style = re.search(r"<style>.*?</style>", html, re.S).group(0)
body = re.search(r"<body>(.*)</body>", html, re.S).group(1)

rows = list(csv.DictReader(io.StringIO(Path(a.trades).read_text(encoding="utf-8"))))
keep = [r for r in rows if r.get("status") != "skipped"]
keep += [r for r in rows if r.get("status") == "skipped"][: a.max_skipped]
buf = io.StringIO()
if rows:
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(keep)


def safe(text: str) -> str:
    return text.replace("</", "<\\/")  # cannot close the <script> early


report = Path(a.report).read_text(encoding="utf-8")
data = (
    f'<script id="embedded-report" type="application/json">{safe(report)}</script>\n'
    f'<script id="embedded-trades" type="text/plain">{safe(buf.getvalue())}</script>\n'
)
# the data blocks must exist before the dashboard script runs
body = body.replace("<script>", data + "<script>", 1)
out = Path(a.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(f"{title}\n{style}\n{body}", encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB; {len(keep)} of {len(rows)} ledger rows)")
