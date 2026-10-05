"""Static guarantees for dashboard.html (offline, no CDN, safe DOM insertion)."""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text()


def test_no_network_or_cdn() -> None:
    assert not re.search(r"<script[^>]+src=", HTML)
    assert not re.search(r"<link[^>]+href=", HTML)
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", HTML))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls  # the SVG namespace is not a request
    assert "fetch(" not in HTML and "XMLHttpRequest" not in HTML


def test_untrusted_data_never_goes_through_inner_html() -> None:
    assert "innerHTML" not in HTML and "outerHTML" not in HTML and "insertAdjacentHTML" not in HTML


def test_storage_is_guarded_and_features_present() -> None:
    assert "try { return localStorage.getItem(k); } catch" in HTML
    for needle in (
        "dragover",
        'type="file"',
        "Download report JSON",
        "Download filtered CSV",
        'id="heroNum"',
        'id="graph"',
        'id="engine"',
        'id="pulse"',
        'id="lat"',
        'id="placeboMini"',
        'id="trades"',
        'id="checklist"',
        'id="replay"',
        "data-theme",
        "prefers-reduced-motion",
        "embedded-report",
    ):
        assert needle in HTML, needle
