"""Desk-lamp branding exports and theme/size contracts."""
import hashlib
from io import BytesIO
from pathlib import Path
import struct

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')


def _png(data):
    """Decode PNGs with the product's Pillow dependency, avoiding slow pixel loops."""
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    image = Image.open(BytesIO(data)).convert('RGBA')
    w, h = image.size
    pixels = list(image.getdata())
    return w, h, [pixels[y*w:(y+1)*w] for y in range(h)]


def test_both_theme_logos_use_identical_approved_lamp_palette():
    dark = (ROOT / 'frontend/brand-logo.png').read_bytes()
    light = (ROOT / 'frontend/brand-logo-light.png').read_bytes()
    assert dark == light
    assert Image.open(BytesIO(dark)).size == (256, 256)


def test_welcome_logos_are_transparent_with_theme_appropriate_frame_and_same_bulb():
    import runpy
    builder = runpy.run_path(str(ROOT / 'desktop/icons/build_macos_icon.py'))
    source = Image.open(ROOT / 'desktop/icons/icon.png')
    dark = Image.open(ROOT / 'frontend/welcome-logo.png').convert('RGBA')
    light = Image.open(ROOT / 'frontend/welcome-logo-light.png').convert('RGBA')
    assert dark.tobytes() == builder['render_welcome_logo'](source).tobytes()
    assert light.tobytes() == builder['render_welcome_logo'](source, light=True).tobytes()
    assert dark.size == light.size == (211, 256)
    assert dark.getchannel('A').tobytes() == light.getchannel('A').tobytes()
    assert dark.getchannel('A').getextrema() == (0, 255)
    assert dark.getpixel((0, 0))[3] == 0
    pole = (round(dark.width * .16), round(dark.height * .55))
    bulb = (round(dark.width * .70), round(dark.height * .41))
    assert min(dark.getpixel(pole)[:3]) > 220
    assert light.getpixel(pole) == (65, 65, 65, 255)
    assert dark.getpixel(bulb) == light.getpixel(bulb)
    assert dark.getpixel(bulb)[2] < 140
    assert '/welcome-logo.png' not in (ROOT / 'frontend/app-assets-apps.js').read_text(encoding='utf-8')
    assert 'content: url("/welcome-logo-light.png")' in CSS


def test_source_matches_approved_lamp_preview():
    source = (ROOT / 'desktop/icons/icon.png').read_bytes()
    assert hashlib.sha256(source).hexdigest() == (
        '0419cb003711710d26cbdd3e1aa6c1acad9c552ccc774fca6f9c4ef788c28025')


def test_native_icon_matches_approved_running_artifact():
    data = (ROOT / 'desktop/icons/icon.icns').read_bytes()
    assert hashlib.sha256(data).hexdigest() == (
        '0fc0533cd0f3772f6a09f88e9b0f8ee63322a1a0ab1e7fe61ed4c6d7fb6c15b1')


def test_lamp_top_arc_is_centred_over_shade():
    image = Image.open(ROOT / 'desktop/icons/icon.png').convert('RGB')

    def white_midpoint(y):
        xs = [x for x in range(450, 850)
              if min(image.getpixel((x, y))) > 210]
        assert xs
        return (min(xs) + max(xs)) / 2

    shade_centre = white_midpoint(400)
    # A artwork has a one-pixel raster offset after its 1254 -> 1024 export.
    # Sample the cap above the arm, keeping the approved one-pixel tolerance.
    for y in (175, 180, 190, 200):
        assert abs(white_midpoint(y) - shade_centre) <= 1.0


def test_sidebar_logo_asset_is_preserved_but_not_shown_in_brand_row():
    import runpy
    builder = runpy.run_path(str(ROOT / 'desktop/icons/build_macos_icon.py'))
    source = Image.open(ROOT / 'desktop/icons/icon.png').convert('RGBA')
    image = Image.open(ROOT / 'frontend/sidebar-brand-logo.png').convert('RGBA')
    assert image.tobytes() == builder['render_sidebar_logo'](source).tobytes()
    assert image.height == 256 and 200 <= image.width <= 215
    assert image.getchannel('A').getextrema() == (0, 255)
    assert image.getpixel((0, 0))[3] == 0
    # The pole, shade and bulb retain their original relative proportions.
    for x, y, yellow in ((.16, .55, False), (.70, .23, False), (.70, .41, True)):
        r, g, b, a = image.getpixel((round(image.width*x), round(image.height*y)))
        assert a == 255
        assert r > 220 and g > 160
        assert b < 140 if yellow else b > 220
    assert '.side-brand .brand-logo { height: 18px; width: auto;' in CSS
    index = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
    assert '/sidebar-brand-logo.png' not in index
    assert '<span class="brand">Desktron</span>' in index
    assert 'html[data-theme="light"] .side-brand .brand-logo' not in CSS


@pytest.mark.parametrize('path,size', [
    ('desktop/icons/icon.png', 1024),
    ('frontend/brand-logo.png', 256),
    ('frontend/brand-logo-light.png', 256),
    ('frontend/icon-180.png', 180),
    ('frontend/icon-192.png', 192),
    ('frontend/icon-512.png', 512),
])
def test_brand_assets_have_charcoal_background_white_lamp_and_yellow_bulb(path, size):
    image = Image.open(ROOT / path).convert('RGBA')
    assert image.size == (size, size)
    assert image.getchannel('A').getextrema() == (255, 255)
    background = image.getpixel((size // 20, size // 2))
    assert 30 <= min(background[:3]) <= max(background[:3]) <= 80
    assert max(background[:3]) - min(background[:3]) <= 8
    pole = image.getpixel((round(size * .30), round(size * .60)))
    assert min(pole[:3]) > 220
    bulb = image.getpixel((round(size * .63), round(size * .43)))
    assert bulb[0] > 220 and bulb[1] > 160 and bulb[2] < 140
    assert bulb[0] > bulb[1] + 10


@pytest.mark.parametrize('size', [180, 192, 512])
def test_home_screen_assets_keep_approved_artwork_without_white_plate(size):
    source = Image.open(ROOT / 'desktop/icons/icon.png').convert('RGBA')
    actual = Image.open(ROOT / f'frontend/icon-{size}.png').convert('RGBA')
    assert actual.tobytes() == source.resize((size, size), Image.Resampling.LANCZOS).tobytes()


def test_home_screen_generator_preserves_approved_source():
    import runpy
    builder = runpy.run_path(str(ROOT / 'desktop/icons/build_macos_icon.py'))
    source = Image.open(ROOT / 'desktop/icons/icon.png').convert('RGBA')
    assert builder['render_home_screen'](source).tobytes() == source.tobytes()


def test_native_icns_preserves_artwork_and_contains_small_and_retina_variants():
    data = (ROOT / 'desktop/icons/icon.icns').read_bytes()
    assert data[:4] == b'icns' and struct.unpack('>I', data[4:8])[0] == len(data)
    pos, members = 8, {}
    while pos < len(data):
        kind = data[pos:pos+4].decode('ascii')
        length = struct.unpack('>I', data[pos+4:pos+8])[0]
        assert length >= 8
        members[kind] = data[pos+8:pos+length]
        pos += length
    assert {'ic04','ic05','ic11','ic12','ic07','ic08','ic13','ic09','ic14','ic10'} <= members.keys()
    import runpy
    builder = runpy.run_path(str(ROOT / 'desktop/icons/build_macos_icon.py'))
    source = Image.open(ROOT / 'desktop/icons/icon.png').convert('RGBA')
    native = Image.open(BytesIO(data)).convert('RGBA')
    approved = builder['render'](source)
    assert native.size == approved.size == (1024, 1024)
    assert native.tobytes() == approved.tobytes()
    assert native.getpixel((0, 0))[3] == 0
    # The native illustration is 6.25% shorter and centred; width and plate stay fixed.
    shortened = source.resize((1024, 960), Image.Resampling.LANCZOS)
    assert native.crop((200, 172, 830, 866)).tobytes() == shortened.crop((200, 140, 830, 834)).tobytes()
    # The mask's top and bottom midpoints retain the native plate extent.
    assert native.getpixel((512, 64))[3] > 230  # LANCZOS-antialiased mask edge
    assert native.getpixel((512, 959))[3] > 230
    def white_vertical_bounds(image):
        ys = [y for y in range(image.height)
              if any(min(image.getpixel((x, y))[:3]) > 220
                     for x in range(210, 370))]
        return min(ys), max(ys)
    before_top, before_bottom = white_vertical_bounds(source)
    after_top, after_bottom = white_vertical_bounds(native)
    assert after_top > before_top + 15
    assert after_bottom < before_bottom - 15
    assert .93 < (after_bottom - after_top) / (before_bottom - before_top) < .95
    for kind, size in builder['VARIANTS'].items():
        variant = Image.open(BytesIO(members[kind])).convert('RGBA')
        assert variant.size == (size, size)
        assert variant.tobytes() == approved.resize((size, size), Image.Resampling.LANCZOS).tobytes()
        if size <= 32:
            assert any(r > 150 and g > 110 and r > b + 50 and g > b + 40
                       for r, g, b, _ in variant.getdata())


def test_full_build_reproduces_all_platform_assets_and_preserves_home_only(tmp_path):
    import runpy
    from shutil import copyfile
    builder = runpy.run_path(str(ROOT / 'desktop/icons/build_macos_icon.py'))
    here = tmp_path / 'desktop/icons'
    here.mkdir(parents=True)
    (tmp_path / 'desktop/windows/icons').mkdir(parents=True)
    (tmp_path / 'frontend').mkdir()
    copyfile(ROOT / 'desktop/icons/icon.png', here / 'icon.png')
    copyfile(ROOT / 'desktop/icons/icon.icns', here / 'icon.icns')
    builder['build'].__globals__['HERE'] = here
    builder['build'](home_screen_only=True)
    assert (here / 'icon.icns').read_bytes() == (ROOT / 'desktop/icons/icon.icns').read_bytes()
    builder['build']()
    paths = ['desktop/icons/icon.png', 'desktop/icons/icon.icns',
             'desktop/windows/icons/desktron.ico', 'desktop/windows/icons/desktron-64.rgba']
    paths += ['frontend/' + name for name in (
        'brand-logo.png', 'brand-logo-light.png', 'sidebar-brand-logo.png',
        'welcome-logo.png', 'welcome-logo-light.png', 'icon-180.png', 'icon-192.png', 'icon-512.png')]
    for path in paths:
        assert (tmp_path / path).read_bytes() == (ROOT / path).read_bytes(), path


@pytest.mark.parametrize('width', [320,1280])
@pytest.mark.parametrize('initial_theme', ['cyan','light'])
@pytest.mark.desktop
def test_webkit_theme_switch_replaces_pixels_without_changing_logo_geometry(width,initial_theme):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as pw:
        browser = pw.webkit.launch()
        page = browser.new_page(viewport={'width':width,'height':600})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(request):
            name = request.request.url.rsplit('/',1)[-1]
            if name in ('welcome-logo.png','welcome-logo-light.png','sidebar-brand-logo.png'):
                request.fulfill(content_type='image/png',body=(ROOT/'frontend'/name).read_bytes())
            else:
                request.fulfill(content_type='text/html',body=f'''<!doctype html><html data-theme="{initial_theme}"><style>{CSS}</style>
<style>body{{display:block;background:white;margin:0}}.side-brand{{padding:0;margin:0;display:block;height:28px}}
.frw-title-logo{{margin:0}}#reference{{display:block;margin:0}}</style>
<div class="side-brand"><img id="side" class="brand-logo" src="/sidebar-brand-logo.png" alt=""></div>
<img id="welcome" class="frw-title-logo" src="/welcome-logo.png" alt="">
<img id="reference" src="/welcome-logo.png" width="23" height="23" alt=""></html>''')
        page.route('https://desk.test/**',route)
        page.goto('https://desk.test/')
        for theme in (initial_theme,'light','cyan','light'):
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
            expected = 'welcome-logo-light.png' if theme == 'light' else 'welcome-logo.png'
            page.locator('#reference').evaluate('(img,src)=>img.src=src','/'+expected)
            page.evaluate('Promise.all([...document.images].map(img=>img.decode()))')
            for name,size in (('side',18),('welcome',26 if width == 320 else 32)):
                logo = page.locator('#'+name)
                is_side = name == 'side'
                asset = 'sidebar-brand-logo.png' if is_side else expected
                assert logo.get_attribute('src') == ('/sidebar-brand-logo.png' if is_side else '/welcome-logo.png')
                content = logo.evaluate('(img)=>getComputedStyle(img).content')
                if theme == 'light' and not is_side:
                    assert expected in content
                else:
                    assert content in ('normal','none')
                bounds = logo.bounding_box()
                assert bounds['height'] == size
                assert abs(bounds['width'] - size * 211 / 256) < .1
                page.locator('#reference').evaluate(
                    '(img,args)=>{img.src=args.src;img.style.width=args.width+"px";img.style.height=args.height+"px"}',
                    {'src':'/'+asset, 'width':bounds['width'], 'height':size})
                # WebKit actually paints the replacement, not merely its CSS declaration.
                actual_pixels = _png(logo.screenshot())
                reference_pixels = _png(page.locator('#reference').screenshot())
                assert actual_pixels == reference_pixels
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()
