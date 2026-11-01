"""Excalidraw app icon is exact fixed upstream artwork, not an untraced resize."""
import hashlib
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / 'frontend/app-assets/excalidraw'


def test_icon_matches_fixed_official_bytes_and_dimensions():
    row = json.loads((FOLDER / 'icon-source.json').read_text(encoding='utf-8'))
    assert row['schema'] == 1
    assert row['version'] == 'v0.18.1'
    assert row['commit'] == 'a2ec2889babf7d2295469c6d90ebe77fae57df84'
    assert row['upstream_file'] == 'public/apple-touch-icon.png'
    assert row['commit'] in row['source_url']
    assert row['sha256'] == 'b6a6c4464319a3b1faeb310bcb8ac24061b2301ce19ddc81453675bbd461ed8f'
    assert hashlib.sha256((FOLDER / row['file']).read_bytes()).hexdigest() == row['sha256']
    with Image.open(FOLDER / row['file']) as image:
        assert list(image.size) == row['dimensions'] == [180, 180]
        assert image.format == 'PNG'
    assert row['transformation'].startswith('None: exact upstream PNG bytes')


def test_full_mit_notice_is_bound_and_trademark_limits_are_separate():
    row = json.loads((FOLDER / 'icon-source.json').read_text(encoding='utf-8'))
    license_data = (FOLDER / row['license_file']).read_bytes()
    assert row['license'] == 'MIT'
    assert row['license_sha256'] == '1352d204fdb90d5e482c13139d848bc2c9300a9a7de358a0a558a23e72d0d8be'
    assert hashlib.sha256(license_data).hexdigest() == row['license_sha256']
    for text in (b'Copyright (c) 2020 Excalidraw', b'Permission is hereby granted',
                 b'THE SOFTWARE IS PROVIDED "AS IS"', b'SOFTWARE.'):
        assert text in license_data
    notices = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    assert '| Excalidraw application icon |' in notices
    assert 'excalidraw-v0.18.1-LICENSE.txt' in notices
    assert 'No trademark permission, affiliation or endorsement is asserted.' in row['limits']


def test_security_review_tracks_new_icon_not_retired_600px_image():
    reviews = json.loads((ROOT / 'scripts/public-tree-review.json').read_text(encoding='utf-8'))
    row, = [r for r in reviews['reviews'] if r['path'] == 'frontend/app-assets/excalidraw/icon.png']
    assert row['rules'] == ['binary-file-review']
    assert row['sha256'] == hashlib.sha256((FOLDER / 'icon.png').read_bytes()).hexdigest()
