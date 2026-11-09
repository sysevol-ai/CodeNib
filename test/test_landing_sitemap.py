# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""codenib.ai lists every landing page for crawlers, by its canonical URL."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

LANDING = Path(__file__).resolve().parents[1] / "landing"
SITEMAP = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _canonical(page: Path) -> str:
    match = re.search(r'<link rel="canonical" href="([^"]+)"', page.read_text())
    assert match, f"{page.relative_to(LANDING)} has no canonical link"
    return match.group(1)


def test_sitemap_lists_each_landing_page_once():
    pages = sorted(LANDING.glob("**/index.html"))
    expected = sorted(_canonical(page) for page in pages)
    root = ET.parse(LANDING / "sitemap.xml").getroot()
    listed = [loc.text for loc in root.iter(f"{SITEMAP}loc")]

    assert listed == sorted(listed)
    assert listed == expected
    assert all(url.startswith("https://codenib.ai/") for url in listed)


def test_robots_points_crawlers_at_the_sitemap():
    robots = (LANDING / "robots.txt").read_text().splitlines()

    assert "User-agent: *" in robots
    assert "Disallow: /" not in robots
    assert "Sitemap: https://codenib.ai/sitemap.xml" in robots
