"""Interface language module for frontend tests that load single scripts outside index.html.

The server delivers app-i18n.js and the language packs inside the /app-core.js bundle, ahead
of every other script, so all scripts can use DeskI18n. Tests that evaluate one script on its
own prepend NODE_PRELUDE (Node) or BROWSER_SCRIPT (page). PACK_FILES follows the bundle order.
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
from frontend_bundle import ASSET_BUNDLES  # noqa: E402

PACK_FILES = [name for name in ASSET_BUNDLES['/app-core.js'] if name.startswith('app-i18n')]
SOURCE = '\n'.join((ROOT / 'frontend' / name).read_text(encoding='utf-8') for name in PACK_FILES)
BROWSER_SCRIPT = SOURCE
# Load the same ordered files without copying whole language packs into argv:
# Windows CreateProcess limits the complete command line to 32767 characters.
# runInThisContext preserves the shared global lexical DeskI18n binding.
NODE_PRELUDE = (
    'globalThis.document ??= {documentElement: {}, querySelectorAll: () => []};\n'
    'globalThis.localStorage ??= {getItem() { return null; }, setItem() {}};\n'
    + '\n'.join(
        "require('node:vm').runInThisContext(require('node:fs').readFileSync("
        + json.dumps(str(ROOT / 'frontend' / name)) + ", 'utf8'));"
        for name in PACK_FILES
    ) + '\n'
)
