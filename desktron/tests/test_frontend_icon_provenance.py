"""Retained Pi geometry and retired Lucide notices have explicit source boundaries."""
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / 'frontend'


def test_pi_occupancy_matches_fixed_upstream_record_without_changing_rendering():
    sources = json.loads((FRONT / 'vendor/brand-icon-sources.json').read_text(encoding='utf-8'))
    assert sources['matched'] == []  # Occupancy correspondence is not a path/byte match.
    row, = sources['geometry_correspondence']
    assert row['symbol'] == 'BRAND_ICONS.pi'
    assert row['upstream'] == '@earendil-works/pi-coding-agent@1.2.0'
    assert row['commit'] == '06a466935536f8c9bcacb2d5efc0fc021be1bdff'
    assert row['upstream_source_sha256'] == 'bbf3b354e774466a1a4df545d66ed929c0834ab1b469cd84c0362c567ffc5618'
    text = (ROOT / row['file']).read_text(encoding='utf-8')
    block = text.split('const BRAND_ICONS = {', 1)[1].split('\n};', 1)[0]
    expression = re.search(r'^  pi: (.*),$', block, re.M).group(1)
    assert expression.startswith("'") and expression.endswith("'")
    svg = expression[1:-1]
    assert hashlib.sha256(svg.encode()).hexdigest() == row['svg_sha256']
    root = ET.fromstring(svg)
    assert root.attrib['viewBox'] == '0 0 24 24'
    assert root.attrib['fill'] == 'currentColor'
    assert len(root) == 7
    occupied = set()
    for rect in root:
        assert rect.tag == 'rect'
        x, y, width, height = (float(rect.attrib[key]) for key in ('x', 'y', 'width', 'height'))
        values = ((x - 5) / 3.5, (y - 5) / 3.5, width / 3.5, height / 3.5)
        assert all(value == int(value) for value in values)
        col, line, cols, lines = map(int, values)
        for cy in range(line, line + lines):
            for cx in range(col, col + cols):
                assert 0 <= cx < 4 and 0 <= cy < 4
                assert (cx, cy) not in occupied
                occupied.add((cx, cy))
    grid = [''.join('#' if (x, y) in occupied else '.' for x in range(4)) for y in range(4)]
    assert grid == row['occupancy'] == ['###.', '#.#.', '##.#', '#..#']


def test_pi_license_is_complete_fixed_version_notice_and_separate_from_cli():
    sources = json.loads((FRONT / 'vendor/brand-icon-sources.json').read_text(encoding='utf-8'))
    row, = sources['geometry_correspondence']
    data = (FRONT / 'vendor' / row['license_file']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == row['license_sha256']
    assert row['license_sha256'] == '0457f5bcec3b3b211605dfb5d1a49042fd638f3686a410fe099c24a25af13c48'
    for phrase in (b'MIT License', b'Copyright (c) 2025 Mario Zechner',
                   b'Permission is hereby granted', b'THE SOFTWARE IS PROVIDED', b'SOFTWARE.'):
        assert phrase in data
    notices = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    assert '| Pi pixel identifier |' in notices
    assert row['license_file'].split('/')[-1] in notices
    assert 'The CLI itself is not bundled.' in notices
    assert 'No trademark permission or endorsement is asserted' in row['limits']


def test_retired_compass_notice_does_not_claim_current_globe_provenance():
    browser = (FRONT / 'app-browser.js').read_text(encoding='utf-8')
    assert '_BR_ICON_COMPASS' not in browser
    assert 'const _BR_ICON_GLOBE' in browser
    license_text = (FRONT / 'vendor/ICONS-LICENSE.txt').read_text(encoding='utf-8')
    assert '历史 Lucide' in license_text
    assert '不将该许可自动套用于当前 _BR_ICON_GLOBE' in license_text
    notices = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    assert '| Retired Lucide compass (historical notice only) |' in notices
    assert '| Lucide icons (individual icons) |' not in notices
