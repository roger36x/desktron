"""Build native, home-screen and UI icons from the approved desk-lamp artwork.

Run with Python + Pillow. No installation, signing, activation or restart occurs.
Use --home-screen-only to leave the native ICNS untouched.
"""
from io import BytesIO
from pathlib import Path
import struct

from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
VARIANTS = {
    'ic04': 16, 'ic05': 32, 'ic11': 32, 'ic12': 64,
    'ic07': 128, 'ic08': 256, 'ic13': 256, 'ic09': 512,
    'ic14': 512, 'ic10': 1024,
}


def render(source):
    """Shorten native artwork vertically by 6.25%; keep its width and outer mask."""
    size, scale = 1024, 4
    mask = Image.new('L', (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (64 * scale, 64 * scale, 960 * scale - 1, 960 * scale - 1),
        radius=190 * scale, fill=255,
    )
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    # Keep the native plate unchanged; centre a 960px-high illustration inside it.
    # Extend only the charcoal edge rows, avoiding a new border or white padding.
    artwork = source.convert('RGBA').resize((size, 960), Image.Resampling.LANCZOS)
    plate = Image.new('RGBA', (size, size))
    plate.paste(artwork, (0, 32))
    plate.paste(artwork.crop((0, 0, size, 1)).resize((size, 32)), (0, 0))
    plate.paste(artwork.crop((0, 959, size, 960)).resize((size, 32)), (0, 992))
    plate.putalpha(ImageChops.multiply(plate.getchannel('A'), mask))
    return plate


def render_home_screen(source):
    """Keep the complete opaque charcoal canvas; mobile OS applies its own mask."""
    image = Image.new('RGBA', (1024, 1024), (49, 49, 49, 255))
    image.alpha_composite(source.convert('RGBA').resize(image.size, Image.Resampling.LANCZOS))
    return image


def render_sidebar_logo(source):
    """Extract the lamp for the brand row, without its charcoal plate or glow.

    Keep the approved white/yellow artwork and its aspect ratio. Thresholds
    isolate the solid illustration from the much darker background lighting.
    """
    image = source.convert('RGBA')
    r, g, b, _ = image.split()
    white = ImageChops.darker(ImageChops.darker(r, g), b).point(
        lambda value: round(255 * min(1, max(0, (value - 150) / 65))))
    yellow = ImageChops.multiply(
        r.point(lambda value: 255 if value > 220 else 0),
        b.point(lambda value: 255 if value < 150 else 0))
    # The approved H=40-degree bulb is warmer than the previous yellow.
    # Keep its solid fill opaque while the red/blue gates still reject the glow.
    yellow = ImageChops.multiply(yellow, g.point(
        lambda value: round(255 * min(1, max(0, (value - 130) / 45)))))
    alpha = ImageChops.lighter(white, yellow)
    image.putalpha(alpha)
    left, top, right, bottom = alpha.getbbox()
    # Four source pixels of transparent breathing room, not the app's canvas.
    image = image.crop((left - 4, top - 4, right + 4, bottom + 4))
    return image.resize((round(image.width * 256 / image.height), 256),
                        Image.Resampling.LANCZOS)


def build_sidebar_logo(source=None):
    if source is None:
        source = Image.open(HERE / 'icon.png')
    render_sidebar_logo(source).save(
        HERE.parents[1] / 'frontend/sidebar-brand-logo.png', format='PNG')
    print('Generated transparent, tightly cropped sidebar brand logo')


def render_welcome_logo(source, light=False):
    """Use the cropped lamp; on light backgrounds darken only the neutral frame."""
    image = render_sidebar_logo(source)
    if light:
        pixels = []
        for r, g, b, a in image.getdata():
            # Keep the warm bulb and its antialiased edge unchanged.
            pixels.append((r, g, b, a) if r - b > 40 and g - b > 20
                          else (65, 65, 65, a))
        image.putdata(pixels)
    return image


def build_welcome_logos(source=None):
    if source is None:
        source = Image.open(HERE / 'icon.png')
    for light, name in ((False, 'welcome-logo.png'), (True, 'welcome-logo-light.png')):
        render_welcome_logo(source, light=light).save(
            HERE.parents[1] / 'frontend' / name, format='PNG')
    print('Generated transparent welcome logos for dark and light backgrounds')


def build(home_screen_only=False):
    source = Image.open(HERE / 'icon.png')
    home = render_home_screen(source)
    frontend = HERE.parents[1] / 'frontend'
    for size in (180, 192, 512):
        home.resize((size, size), Image.Resampling.LANCZOS).save(
            frontend / f'icon-{size}.png', format='PNG')
    print('Generated frontend home-screen icons: approved artwork, opaque charcoal background')
    if home_screen_only:
        return
    build_sidebar_logo(source)
    build_welcome_logos(source)
    for name in ('brand-logo.png', 'brand-logo-light.png'):
        home.resize((256, 256), Image.Resampling.LANCZOS).save(frontend / name, format='PNG')
    windows = HERE.parent / 'windows' / 'icons'
    home.save(windows / 'desktron.ico', format='ICO',
              sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    with Image.open(windows / 'desktron.ico') as icon:
        (windows / 'desktron-64.rgba').write_bytes(
            icon.ico.getimage((64, 64)).convert('RGBA').tobytes())
    print('Generated matching UI logos and Windows executable/window icons')
    image = render(source)
    chunks = []
    for kind, size in VARIANTS.items():
        out = BytesIO()
        image.resize((size, size), Image.Resampling.LANCZOS).save(out, format='PNG')
        data = out.getvalue()
        chunks.append(kind.encode('ascii') + struct.pack('>I', len(data) + 8) + data)
    body = b''.join(chunks)
    (HERE / 'icon.icns').write_bytes(b'icns' + struct.pack('>I', len(body) + 8) + body)
    print('Generated desktop/icons/icon.icns: charcoal artwork, transparent exterior')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home-screen-only', action='store_true')
    parser.add_argument('--sidebar-only', action='store_true')
    parser.add_argument('--welcome-only', action='store_true')
    args = parser.parse_args()
    if args.welcome_only:
        build_welcome_logos()
    elif args.sidebar_only:
        build_sidebar_logo()
    else:
        build(home_screen_only=args.home_screen_only)
