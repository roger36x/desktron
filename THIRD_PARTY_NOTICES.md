# Third-party inventory

Desktron's own code is licensed under Apache-2.0 (see `LICENSE`). The components below are either committed to this repository or fetched at build/install time. Their licenses and notices apply to the corresponding files; this inventory does not replace them.

## Committed to the repository

| Component | Version | Origin | License | Files |
| --- | --- | --- | --- | --- |
| highlight.js | 11.11.1 | https://github.com/highlightjs/highlight.js | BSD-3-Clause | `frontend/vendor/highlight.min.js`, `frontend/vendor/github-dark.min.css` |
| markdown-it | 14.2.0 | https://github.com/markdown-it/markdown-it | MIT | `frontend/vendor/markdown-it.min.js` |
| qrcode-generator | 1.4.4 + local UTF-8 adapter | https://github.com/kazuhikoarase/qrcode-generator | MIT | `frontend/vendor/qrcode.js`, full MIT notice and upstream package checksum in `frontend/vendor/qrcode-LICENSE.txt`; embedded UTF-8 snippet replaced with platform `TextEncoder`, patched digest in `frontend/vendor/qrcode-source.json` |
| xterm.js | @xterm/xterm 5.5.0 | https://github.com/xtermjs/xterm.js | MIT | `frontend/vendor/xterm/xterm.js`, `frontend/vendor/xterm/xterm.css` |
| xterm.js fit addon | @xterm/addon-fit 0.10.0 | https://github.com/xtermjs/xterm.js | MIT | `frontend/vendor/xterm/addon-fit.js` |
| JetBrains Mono | see font file | https://github.com/JetBrains/JetBrainsMono | SIL OFL 1.1 | `frontend/vendor/fonts/JetBrainsMono-Regular.woff2`, license in `frontend/vendor/fonts/LICENSE-JetBrainsMono.txt` |
| Retired Lucide compass (historical notice only) | — | https://lucide.dev | ISC | `_BR_ICON_COMPASS` is no longer defined; retained notice in `frontend/vendor/ICONS-LICENSE.txt` does not establish provenance for current browser graphics. |
| Feather legacy workflow icon | feather-icons 4.29.2 | https://github.com/feathericons/feather | MIT | `ICONS.zap`; source in `frontend/vendor/inline-icon-sources.json`, full license in `frontend/vendor/licenses/feather-LICENSE.txt` |
| Pi pixel identifier | Pi Coding Agent 1.2.0, tag v1.2.0, commit 06a466935536f8c9bcacb2d5efc0fc021be1bdff | https://github.com/earendil-works/pi | MIT; trademarks separate | `BRAND_ICONS.pi` is a monochrome rectangle adaptation of the source logo's 4×4 occupancy geometry, not an SVG byte match. See `frontend/vendor/brand-icon-sources.json` and full notice `frontend/vendor/licenses/pi-1.2.0-LICENSE.txt`. The CLI itself is not bundled. |
| Excalidraw application icon | v0.18.1, commit a2ec2889babf7d2295469c6d90ebe77fae57df84 | https://github.com/excalidraw/excalidraw | MIT; trademarks separate | `frontend/app-assets/excalidraw/icon.png` is byte-identical to `public/apple-touch-icon.png`. Fixed source and digests: `icon-source.json` beside the icon; full notice: `frontend/vendor/licenses/excalidraw-v0.18.1-LICENSE.txt`. |
| Seti UI icons | — | https://github.com/jesseweed/seti-ui | MIT | `frontend/app-assets/files/src/`, license in `SETI-LICENSE.txt` |

## Reviewed remaining inline artwork

The 25 remaining browser, chat and arrangement symbols are independently constructed project artwork, recorded separately in `frontend/vendor/remaining-inline-icon-manifest.json`. Approved SVGs and digests are included in `frontend/icons/reviewed-remaining/`. Original display dimensions and the existing fork mask are preserved; the browser globe uses two straight latitude lines 7.8 canvas units apart. No external icon library is attributed to these new drawings. These records do not settle project-wide ownership or grant trademark rights.

## Reviewed scattered inline artwork

The 24 approved reuses and two new drawings (password visibility and save-as) are recorded in `frontend/vendor/scattered-inline-icon-manifest.json`. The reuses retain the geometry of already reviewed project drawings; only display sizing, placement, accessibility metadata and context paint are adapted. The two new SVG sources are in `frontend/icons/reviewed-scattered/`. TaskNote V3 (warm-white background and three small red checks) and the note tile V19 (no lines, orange-yellow upper third, original warm-white paper gradient on the lower two thirds and rounded warm-light-gray fold) were separately adopted on 2026-10-27. Their complete AI-assisted project SVG drawings and source digests are recorded under `adopted_tiles` in the same manifest; they replace the earlier tiles rather than attributing those earlier macOS-inspired designs to an external library. No Apple or TaskPaper artwork is embedded by this change; visual adoption is not a trademark clearance. Timeline-only artwork is excluded. These records do not settle project-wide ownership.

## Product branding

The current white desk lamp with a warm yellow bulb on a charcoal background is
user-approved, AI-assisted product artwork. `desktop/icons/icon.png` is the
selected source. `desktop/icons/build_macos_icon.py` generates the macOS ICNS,
Windows ICO/window RGBA, UI logos and home-screen PNGs from it.

The earlier circle/pencil trial, derived from **Pencil 564229** by
**Magnific / Flaticon**, has been replaced in the current files. Its attribution
and restrictions remain recorded in `frontend/vendor/ICONS-LICENSE.txt` for the
historical assets. Replacing current files does not remove earlier Git blobs;
public-source preparation must still exclude that trial from published history.

## Fetched at build time (not committed)

`scripts/dev/build-builtin-apps.sh` installs these from the public npm registry according to `frontend/app-assets/package-lock.json` and bundles them into `frontend/app-assets/*/vendor/`:

| Component | Version | License |
| --- | --- | --- |
| @excalidraw/excalidraw | 0.18.1 | MIT |
| monaco-editor | 0.52.2 | MIT |
| react / react-dom | 18.3.1 | MIT |
| esbuild (build tool only) | 0.25.9 | MIT |

## Installed on the user's machine (not bundled)

Engine CLIs and SDKs are installed by the user or by the setup wizard from their public sources, under their own licenses and terms: Claude Code and the Claude Agent SDK (lock file `desktron/lib/shepherd/runtime/claude-agent-sdk-package-lock.json`), Codex CLI, Pi Coding Agent and its subagent packages (lock file `desktron/lib/runtime/pi-subagents-package-lock.json`), and optional tools such as lark-cli. Desktron does not redistribute them.

The Apache-2.0 license text in `LICENSE` originates from https://www.apache.org/licenses/LICENSE-2.0.txt. No affiliation with the Apache Software Foundation, or with any engine vendor named above, is implied.
