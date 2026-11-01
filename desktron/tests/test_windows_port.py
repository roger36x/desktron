"""Windows 移植的跨平台契约测试。

这些测试在 macOS / Linux 上运行，验证：POSIX 行为未被兼容层改变；Windows 专用逻辑(命令解析、
依赖声明、payload 边界、壳脚本同步、终端读线程)在可模拟的范围内正确。真实 Windows 行为见
test_windows_native.py(仅在 Windows 上运行)。
"""
import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "desktron" / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(ROOT / "scripts" / "dev"))
# deskcfg 在导入时读取 ui_server 的 argv 契约(前端目录、端口、占位、工作区)。
if len(sys.argv) < 5 or not sys.argv[2].isdigit():
    sys.argv = ["test", str(ROOT / "frontend"), "0", "0", tempfile.mkdtemp()]

import oscompat  # noqa: E402


class PosixUnchanged(unittest.TestCase):
    """兼容层在 POSIX 上只做透传。"""

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_install_is_noop_on_posix(self):
        before = (os.open, os.replace, os.kill, getattr(os, "killpg", None))
        oscompat.install()
        self.assertEqual(before, (os.open, os.replace, os.kill, getattr(os, "killpg", None)))
        self.assertNotIn("fcntl", getattr(sys.modules.get("fcntl"), "__name__", "") and [] or [])

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_mode_and_uid_helpers(self):
        self.assertEqual(oscompat.loose_mode(0o100644), 0o044)
        self.assertEqual(oscompat.loose_mode(0o100600), 0)
        self.assertEqual(oscompat.getuid(), os.getuid())

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_killpg_and_getpgid_delegate(self):
        with mock.patch.object(os, "killpg") as killpg, mock.patch.object(os, "getpgid", return_value=77) as getpgid:
            oscompat.killpg(12, 9)
            self.assertEqual(oscompat.getpgid(5), 77)
        killpg.assert_called_once_with(12, 9)
        getpgid.assert_called_once_with(5)

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_session_kwargs(self):
        self.assertEqual(oscompat.session_kwargs(), {"start_new_session": True})
        self.assertEqual(oscompat.no_window_kwargs(), {})

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_success_marker_posix(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = os.path.join(tmp, "ok")
            oscompat.run_with_success_marker([sys.executable, "-c", "pass"], "MARK", ok, capture_output=True)
            self.assertTrue(os.path.exists(ok))
            bad = os.path.join(tmp, "bad")
            oscompat.run_with_success_marker([sys.executable, "-c", "raise SystemExit(3)"], "MARK", bad,
                                             capture_output=True)
            self.assertFalse(os.path.exists(bad))

    def test_lock_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            first = os.open(path, os.O_CREAT | os.O_RDWR)
            second = os.open(path, os.O_CREAT | os.O_RDWR)
            try:
                oscompat.lock_file(first, exclusive=True, blocking=False)
                with self.assertRaises(BlockingIOError):
                    oscompat.lock_file(second, exclusive=True, blocking=False)
                oscompat.unlock_file(first)
                oscompat.lock_file(second, exclusive=True, blocking=False)
                oscompat.unlock_file(second)
            finally:
                os.close(first)
                os.close(second)

    def test_process_alive(self):
        self.assertTrue(oscompat.process_alive(os.getpid()))
        self.assertFalse(oscompat.process_alive(0))
        self.assertFalse(oscompat.process_alive(-5))


class DirectorySync(unittest.TestCase):
    def test_windows_does_not_open_directory(self):
        with mock.patch.object(oscompat, "IS_WINDOWS", True), \
                mock.patch.object(oscompat.os, "open") as opened, \
                mock.patch.object(oscompat.os, "fsync") as synced:
            oscompat.fsync_directory("directory")
        opened.assert_not_called()
        synced.assert_not_called()

    def test_posix_syncs_and_closes_directory(self):
        with mock.patch.object(oscompat, "IS_WINDOWS", False), \
                mock.patch.object(oscompat.os, "open", return_value=123) as opened, \
                mock.patch.object(oscompat.os, "fsync") as synced, \
                mock.patch.object(oscompat.os, "close") as closed:
            oscompat.fsync_directory("directory")
        opened.assert_called_once_with("directory", os.O_RDONLY)
        synced.assert_called_once_with(123)
        closed.assert_called_once_with(123)

    def test_posix_sync_error_is_fatal_and_closes_directory(self):
        with mock.patch.object(oscompat, "IS_WINDOWS", False), \
                mock.patch.object(oscompat.os, "open", return_value=123), \
                mock.patch.object(oscompat.os, "fsync", side_effect=OSError("sync failed")), \
                mock.patch.object(oscompat.os, "close") as closed:
            with self.assertRaises(OSError):
                oscompat.fsync_directory("directory")
        closed.assert_called_once_with(123)


class NoFollowOpen(unittest.TestCase):
    """Windows CRT has no O_NOFOLLOW; oscompat enforces the final-component check."""

    @staticmethod
    def _info(tag=0, ino=1):
        from types import SimpleNamespace
        return SimpleNamespace(st_reparse_tag=tag, st_dev=7, st_ino=ino)

    def test_link_rejected_before_open_so_create_cannot_touch_target(self):
        opener = mock.Mock()
        with mock.patch.object(oscompat.os, "lstat", return_value=self._info(tag=0xA000000C)):
            with self.assertRaises(OSError):
                oscompat._open_nofollow(opener, "link", os.O_WRONLY, 0o600)
        opener.assert_not_called()

    def test_link_swapped_in_during_open_is_closed_and_rejected(self):
        opener = mock.Mock(return_value=55)
        with mock.patch.object(oscompat.os, "lstat", side_effect=[self._info(ino=1), self._info(ino=1)]), \
                mock.patch.object(oscompat.os, "fstat", return_value=self._info(ino=2)), \
                mock.patch.object(oscompat.os, "close") as closed:
            with self.assertRaises(OSError):
                oscompat._open_nofollow(opener, "file", os.O_RDONLY, 0o777)
        closed.assert_called_once_with(55)

    def test_missing_file_may_be_created(self):
        opener = mock.Mock(return_value=9)
        with mock.patch.object(oscompat.os, "lstat", side_effect=[FileNotFoundError(), self._info(ino=3)]), \
                mock.patch.object(oscompat.os, "fstat", return_value=self._info(ino=3)):
            self.assertEqual(oscompat._open_nofollow(opener, "new", os.O_CREAT | os.O_WRONLY, 0o600), 9)
        opener.assert_called_once_with("new", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def test_truncation_waits_for_handle_validation(self):
        opener = mock.Mock(return_value=55)
        with mock.patch.object(oscompat.os, "lstat", return_value=self._info()), \
                mock.patch.object(oscompat.os, "fstat", return_value=self._info()), \
                mock.patch.object(oscompat.os, "ftruncate") as truncated:
            self.assertEqual(oscompat._open_nofollow(
                opener, "file", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 55)
        opener.assert_called_once_with("file", os.O_WRONLY, 0o600)
        truncated.assert_called_once_with(55, 0)

    def test_swapped_file_is_not_truncated_even_if_path_matches_opened_handle(self):
        opener = mock.Mock(return_value=55)
        with mock.patch.object(oscompat.os, "lstat", side_effect=[self._info(ino=1), self._info(ino=2)]), \
                mock.patch.object(oscompat.os, "fstat", return_value=self._info(ino=2)), \
                mock.patch.object(oscompat.os, "ftruncate") as truncated, \
                mock.patch.object(oscompat.os, "close") as closed:
            with self.assertRaises(OSError):
                oscompat._open_nofollow(opener, "file", os.O_WRONLY | os.O_TRUNC, 0o600)
        opener.assert_called_once_with("file", os.O_WRONLY, 0o600)
        truncated.assert_not_called()
        closed.assert_called_once_with(55)

    @unittest.skipIf(oscompat.IS_WINDOWS, "Synthetic symlink race uses POSIX test privileges")
    def test_link_swap_cannot_truncate_or_create_an_outside_target(self):
        # Exercise the Windows helper with real descriptors, without depending
        # on Windows developer-mode symlink privileges on CI.
        for existed, target_exists in ((True, True), (True, False), (False, False)):
            with self.subTest(existed=existed, target_exists=target_exists), tempfile.TemporaryDirectory() as root:
                entry = Path(root) / "entry"
                target = Path(root) / "outside"
                if existed:
                    entry.write_bytes(b"entry")
                if target_exists:
                    target.write_bytes(b"keep")
                original_open = os.open
                def swapped_open(path, flags, mode):
                    if entry.exists():
                        entry.unlink()
                    entry.symlink_to(target)
                    return original_open(path, flags, mode)
                with self.assertRaises(OSError):
                    oscompat._open_nofollow(swapped_open, str(entry),
                                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                if target_exists:
                    self.assertEqual(target.read_bytes(), b"keep")
                else:
                    self.assertFalse(target.exists())

    @unittest.skipUnless(oscompat.IS_WINDOWS, "Windows os.open wrapper")
    def test_windows_open_rejects_junction_and_keeps_regular_files(self):
        self.assertEqual(os.O_NOFOLLOW, oscompat.WINDOWS_O_NOFOLLOW)
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            os.mkdir(target)
            regular = os.path.join(target, "regular")
            fd = os.open(regular, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.write(fd, b"ok")
            os.close(fd)
            link = os.path.join(root, "link")
            oscompat._create_junction(target, link)
            with self.assertRaises(OSError) as raised:
                os.open(link, os.O_RDONLY | os.O_NOFOLLOW)
            self.assertNotIsInstance(raised.exception, PermissionError)
            # Only the final component is checked, matching POSIX O_NOFOLLOW.
            fd = os.open(os.path.join(link, "regular"), os.O_RDONLY | os.O_NOFOLLOW)
            self.assertEqual(os.read(fd, 8), b"ok")
            os.close(fd)
            os.rmdir(link)


class ImportsCompatLayer(unittest.TestCase):
    """用到 POSIX 专有接口的后端模块必须先导入 oscompat，Windows 上才有对应实现。"""

    PATTERN = re.compile(r"import fcntl|fcntl\.|os\.O_NOFOLLOW|os\.O_NONBLOCK|os\.O_DIRECTORY|os\.fchmod|"
                         r"os\.killpg|os\.getpgid|os\.getpgrp|os\.getsid|signal\.SIGKILL|os\.getuid|os\.kill\(")

    def test_modules_import_oscompat(self):
        missing = []
        for path in list(LIB.rglob("*.py")) + [p for p in (ROOT / "desktron" / "bin").iterdir() if p.is_file()]:
            if path.name == "oscompat.py" or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, IsADirectoryError):
                continue
            if not text.startswith("#!/usr/bin/env python3") and path.suffix != ".py":
                continue
            if self.PATTERN.search(text) and not re.search(r"(?m)^\s*import oscompat\b", text):
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], "这些模块用到 POSIX 专有接口但未导入 oscompat")

    def test_mode_checks_use_helper(self):
        offenders = []
        for path in LIB.rglob("*.py"):
            if path.name == "oscompat.py":
                continue
            if re.search(r"st_mode\)? ?& ?0o077", path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "权限位检查请用 oscompat.loose_mode()")


class CommandResolution(unittest.TestCase):
    def test_windows_candidates_follow_pathext(self):
        import cli_path
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "codex.cmd").write_text("@echo off\r\n")
            Path(tmp, "claude.exe").write_bytes(b"MZ\x90\x00")
            Path(tmp, "broken.exe").write_bytes(b"#!/bin/sh\n")
            stub = os.path.join(tmp, "WindowsApps")
            os.makedirs(stub)
            Path(stub, "python3.exe").write_bytes(b"MZ")
            with mock.patch.object(cli_path.sys, "platform", "win32"), \
                    mock.patch.dict(os.environ, {"PATHEXT": ".COM;.EXE;.BAT;.CMD"}), \
                    mock.patch.object(cli_path, "_report_skip"):
                search = os.pathsep.join([stub, tmp])
                self.assertEqual(cli_path.which("codex", path=search), os.path.join(tmp, "codex.cmd"))
                self.assertEqual(cli_path.which("claude", path=search), os.path.join(tmp, "claude.exe"))
                self.assertIsNone(cli_path.which("broken", path=search))
                self.assertIsNone(cli_path.which("python3", path=search))   # 商店占位程序不算


class NpmShims(unittest.TestCase):
    SHIM = ('@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n'
            'CALL :find_dp0\r\nendLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
            '"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')

    def test_npm_cmd_resolves_to_node_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "codex.cmd").write_text(self.SHIM)
            script = Path(tmp, "node_modules", "@openai", "codex", "bin", "codex.js")
            script.parent.mkdir(parents=True)
            script.write_text("")
            Path(tmp, "node.exe").write_bytes(b"MZ")
            argv = oscompat.npm_shim_argv(os.path.join(tmp, "codex.cmd"))
            self.assertEqual(argv, [os.path.join(tmp, "node.exe"), str(script)])
            kwargs = {}
            args = oscompat._resolve_command(([os.path.join(tmp, "codex.cmd"), "app-server", "-c", 'a="b"'],), kwargs)
            self.assertEqual(args[0], [os.path.join(tmp, "node.exe"), str(script), "app-server", "-c", 'a="b"'])

    @unittest.skipUnless(oscompat.IS_WINDOWS, "原生 Windows Node 包装入口")
    def test_managed_pi_launcher_preserves_unicode_and_shell_metacharacters(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        with tempfile.TemporaryDirectory(prefix='pi shim 中文 ') as tmp:
            shim = Path(tmp, 'pi.cmd')
            shim.write_text('@echo off\nnode "%~dp0pi-launcher.js" %*\n', encoding='utf-8')
            script = Path(tmp, 'pi-launcher.js')
            script.write_text('console.log(JSON.stringify(process.argv.slice(2)));', encoding='utf-8')
            with mock.patch('cli_path.which', return_value=node):
                argv = oscompat.npm_shim_argv(str(shim))
                self.assertEqual(argv, [node, str(script)])
                arguments = ['中文与 emoji 🐑', 'literal %UNEXPANDED% & | < >', 'space "quote"']
                result = subprocess.run([str(shim), *arguments], capture_output=True,
                                        text=True, encoding='utf-8', timeout=10, check=True)
            self.assertEqual(json.loads(result.stdout), arguments)

    def test_non_npm_cmd_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "tool.cmd").write_text("@echo off\r\necho hi\r\n")
            self.assertIsNone(oscompat.npm_shim_argv(os.path.join(tmp, "tool.cmd")))
            original = ([os.path.join(tmp, "tool.cmd"), "x"],)
            self.assertIs(oscompat._resolve_command(original, {}), original)


class DependencyDeclarations(unittest.TestCase):
    def test_windows_install_commands_selected(self):
        import check_deps
        with mock.patch.object(check_deps.sys, "platform", "win32"):
            rows = {r["name"]: r for r in check_deps.parse_reqs(str(ROOT))}
        self.assertIn("irm https://claude.ai/install.ps1", rows["claude"]["install"])
        self.assertIn("winget install --id Git.Git", rows["git"]["install"])
        self.assertEqual(rows["node"]["install"], "desktron prepare")
        self.assertIn(".local\\bin", rows["codex"]["install"])
        self.assertIn("cargo", rows)
        self.assertNotIn("swiftc", rows)

    def test_posix_rows_unchanged(self):
        import check_deps
        with mock.patch.object(check_deps.sys, "platform", "darwin"):
            rows = {r["name"]: r for r in check_deps.parse_reqs(str(ROOT))}
        self.assertEqual(rows["claude"]["install"], "curl -fsSL https://claude.ai/install.sh | bash")
        self.assertIn("swiftc", rows)
        self.assertNotIn("cargo", rows)
        self.assertFalse(any(k.endswith("_windows") for r in rows.values() for k in r))


class PayloadBoundary(unittest.TestCase):
    @unittest.skipUnless(shutil.which("rsync"), "需要 rsync 对照")
    def test_python_copy_matches_rsync(self):
        import payload
        out = subprocess.run(["rsync", "-an", "--out-format=%n", "--exclude-from",
                              str(ROOT / "scripts" / "payload-excludes.txt"), str(ROOT) + "/", "/nonexistent-desk/"],
                             capture_output=True, text=True).stdout
        expected = sorted({line.rstrip("/") for line in out.splitlines() if line.strip() and line != "./"})
        actual = sorted(rel for rel, _ in payload.iter_payload(str(ROOT), payload.load_patterns()))
        self.assertEqual(actual, expected)

    def test_windows_shell_sources_excluded(self):
        import payload
        patterns = payload.load_patterns()
        self.assertTrue(payload.excluded("desktop/windows", patterns))
        self.assertTrue(payload.excluded("desktop/shared", patterns))
        self.assertFalse(payload.excluded("desktop/native", patterns))
        self.assertFalse(payload.excluded("scripts/windows", patterns))


class WindowBackdrop(unittest.TestCase):
    def test_main_window_and_webview_share_explicit_material_capability(self):
        source = (ROOT / 'desktop/windows/src/app.rs').read_text(encoding='utf-8')
        builder = source.split('let window = WindowBuilder::new()', 1)[1].split('.build(&event_loop)', 1)[0]
        self.assertIn('.with_transparent(true)', builder)
        self.assertIn('let backdrop_result = backdrop::apply(&window, false);', source)
        self.assertIn('.with_background_color(backdrop::background(false, false))', source)
        self.assertIn('app.sync_backdrop_background();', source)
        self.assertIn('.with_initialization_script_for_main_only(backdrop::PENDING_SCRIPT, true)', source)
        self.assertIn('window_backdrop_mode=solid', source)
        self.assertIn('app.startup_log(&error)', source)

    def test_theme_switch_keeps_transparency_and_reports_failed_fallback(self):
        source = (ROOT / 'desktop/windows/src/app.rs').read_text(encoding='utf-8')
        appearance = source.split('pub fn set_appearance', 1)[1].split('fn attach_main_events', 1)[0]
        self.assertIn('backdrop::apply(&self.window, light)', appearance)
        self.assertIn('backdrop::background(self.acrylic, self.light)', appearance)
        self.assertIn('backdrop::disable(&self.window)', appearance)
        self.assertIn('window_backdrop_webview_failed', appearance)
        self.assertIn('window_backdrop_solid_webview_failed', appearance)
        self.assertIn('backdrop::script(self.acrylic)', appearance)

    def test_material_uses_public_dwm_api_not_bridge_as_proof(self):
        source = (ROOT / 'desktop/windows/src/backdrop.rs').read_text(encoding='utf-8')
        self.assertIn('let material = DWMSBT_TRANSIENTWINDOW;', source)
        self.assertIn('DwmExtendFrameIntoClientArea(hwnd, &margins)', source)
        self.assertIn('(0, 0, 0, 0)', source)
        self.assertNotRegex(source, r'SetWindowCompositionAttribute\s*\(')
        index = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
        self.assertIn("['solid', 'pending'].includes(window.__deskWindowsBackdrop)", index)
        self.assertIn("classList.add('no-vibrancy')", index)


    def test_reloads_project_latest_native_state_not_startup_state(self):
        source = (ROOT / 'desktop/windows/src/app.rs').read_text(encoding='utf-8')
        event = source.split('UserEvent::MainContentLoading => {', 1)[1].split('}', 1)[0]
        self.assertIn('self.sync_backdrop_marker();', event)
        self.assertNotIn('with_initialization_script_for_main_only(backdrop::script(acrylic)', source)
        marker = source.split('fn sync_backdrop_marker', 1)[1].split('fn attach_main_events', 1)[0]
        self.assertIn('backdrop::script(self.acrylic)', marker)
        backdrop = (ROOT / 'desktop/windows/src/backdrop.rs').read_text(encoding='utf-8')
        self.assertIn("__deskWindowsBackdrop='pending'", backdrop)
        self.assertIn("document.addEventListener('DOMContentLoaded',mark,{once:true})", backdrop)


    def test_material_clears_native_surface_and_uses_production_chrome_in_desktop_check(self):
        chrome = (ROOT / 'desktop/windows/src/chrome.rs').read_text(encoding='utf-8')
        clear = chrome.split('unsafe fn clear_backdrop', 1)[1].split('unsafe fn draw', 1)[0]
        self.assertIn('if self.acrylic.get()', clear)
        self.assertIn('bottom: g.height', clear)
        self.assertIn('rgb(0, 0, 0)', clear)
        self.assertIn('self.clear_backdrop(dc)', chrome)
        self.assertIn('ExcludeClipRect(dc, g.left, 0, g.width, g.caption)', clear)
        self.assertIn('state.clear_backdrop(dc)', chrome)
        app = (ROOT / 'desktop/windows/src/app.rs').read_text(encoding='utf-8')
        self.assertIn('self.chrome.set_acrylic(self.acrylic);', app)
        self.assertIn('window_chrome_web_bounds_failed', app)
        test = (ROOT / 'desktop/windows/src/backdrop.rs').read_text(encoding='utf-8')
        self.assertIn('crate::chrome::Chrome::new(&window, &web)', test)
        self.assertIn('window.set_maximized(maximized)', test)
        self.assertIn('window.set_outer_position', test)
        self.assertIn('gray native surface', test)
        self.assertIn('chrome.set_acrylic(false)', test)

    def test_caption_is_separate_opaque_surface_without_system_caption(self):
        chrome = (ROOT / 'desktop/windows/src/chrome.rs').read_text(encoding='utf-8')
        self.assertIn('Some(caption_callback)', chrome)
        self.assertIn('WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS', chrome)
        self.assertIn('pixel[3] = 255', chrome)
        self.assertIn('GdiFlush().as_bool()', chrome)
        self.assertIn('WM_STYLECHANGING', chrome)
        self.assertIn('styles.styleNew &= !WS_CAPTION.0', chrome)
        self.assertIn('WM_NCHITTEST => return LRESULT(HTTRANSPARENT as isize)', chrome)
        self.assertIn('WM_PRINT | WM_PRINTCLIENT', chrome)
        self.assertNotIn('DWMNCRP_DISABLED', chrome)
        geometry = chrome.split('unsafe fn geometry(&self)', 1)[1].split('unsafe fn resize_hit', 1)[0]
        self.assertNotIn('GetSystemMetricsForDpi(', geometry)  # no extra full-width strip
        self.assertIn('WM_GETMINMAXINFO', chrome)
        self.assertIn('WM_NCCALCSIZE', chrome)
        self.assertIn('work.left - monitor.left', chrome)
        self.assertIn('work.bottom - work.top', chrome)
        self.assertIn('DWMWA_BORDER_COLOR', chrome)
        self.assertIn('ABM_GETAUTOHIDEBAREX', chrome)
        self.assertIn('maximized OUTER rectangle covers taskbar', (ROOT / 'desktop/windows/src/backdrop.rs').read_text(encoding='utf-8'))
        draw = chrome.split('unsafe fn draw(&self', 1)[1].split('// 扩展玻璃', 1)[0]
        self.assertNotIn('clear_backdrop', draw)
        test = (ROOT / 'desktop/windows/src/backdrop.rs').read_text(encoding='utf-8')
        self.assertIn('for _ in 0..10', test)
        self.assertIn('caption is not opaque theme color', test)
        self.assertIn('ClientToScreen(hwnd, &mut origin)', test)
        self.assertIn('WebView composition absent', test)


class WindowIcons(unittest.TestCase):
    def test_window_and_taskbar_share_loaded_icon(self):
        source = (ROOT / "desktop" / "windows" / "src" / "app.rs").read_text(encoding="utf-8")
        builder = source.split("let window = WindowBuilder::new()", 1)[1].split(".build(&event_loop)", 1)[0]
        self.assertRegex(source, r"let icon = load_icon\(\);\s*let window = WindowBuilder::new\(\)")
        self.assertIn("use tao::platform::windows::WindowBuilderExtWindows;", source)
        self.assertIn(".with_window_icon(icon.clone())", builder)
        self.assertIn(".with_taskbar_icon(icon)", builder)

    def test_window_rgba_matches_executable_icon(self):
        from PIL import Image
        icons = ROOT / "desktop" / "windows" / "icons"
        rgba = (icons / "desktron-64.rgba").read_bytes()
        self.assertEqual(len(rgba), 64 * 64 * 4)
        with Image.open(icons / "desktron.ico") as image:
            self.assertEqual(rgba, image.ico.getimage((64, 64)).convert("RGBA").tobytes())
        source = (ROOT / "desktop" / "windows" / "src" / "app.rs").read_text(encoding="utf-8")
        self.assertIn('include_bytes!("../icons/desktron-64.rgba")', source)
        self.assertIn("Icon::from_rgba(bytes.to_vec(), 64, 64)", source)


class ShellScripts(unittest.TestCase):
    def test_shared_scripts_match_swift(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "dev" / "extract_native_scripts.py"), "--check"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bridge_names_cover_swift_handlers(self):
        swift = (ROOT / "desktop" / "native" / "main.swift").read_text(encoding="utf-8")
        handlers = set(re.findall(r'userContentController\.add\(self, name: "(\w+)"\)', swift))
        bridge = (ROOT / "desktop" / "windows" / "js" / "main-bridge.js").read_text(encoding="utf-8")
        names = set(re.findall(r"'(\w+)'", bridge.split("const names = [", 1)[1].split("]", 1)[0]))
        # nativeControl(macOS 原生应用观察)有意不在 Windows 暴露；appControlUser 属于浏览器面板。
        self.assertEqual(handlers - names, {"nativeControl", "appControlUser"})

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_bridge_shim_and_shortcut_mapping(self):
        bridge = (ROOT / "desktop" / "windows" / "js" / "main-bridge.js").read_text(encoding="utf-8")
        harness = r"""
const vm = require('vm');
const sent = []; const listeners = {}; const dispatched = [];
class KeyboardEvent { constructor(type, init) { Object.assign(this, init, {type, isTrusted: false, defaultPrevented: false}); }
  preventDefault() { this.defaultPrevented = true; } }
const target = { dispatchEvent(ev) { dispatched.push(ev); if (ev.key === 't') ev.preventDefault(); return true; } };
const window = { ipc: { postMessage: (m) => sent.push(JSON.parse(m)) },
  addEventListener: (t, f) => { (listeners['w' + t] = listeners['w' + t] || []).push(f); } };
window.top = window;
const document = { documentElement: { classList: { add() {} } },
  addEventListener: (t, f) => { (listeners['d' + t] = listeners['d' + t] || []).push(f); } };
const context = { window, document, KeyboardEvent, JSON, Object };
vm.runInNewContext(SCRIPT, context);
window.webkit.messageHandlers.browser.postMessage({action: 'open', id: 'x'});
let stopped = false, prevented = false;
const original = { isTrusted: true, ctrlKey: true, shiftKey: true, altKey: false, metaKey: false, code: 'KeyT',
  target, preventDefault() { prevented = true; }, stopImmediatePropagation() { stopped = true; } };
for (const f of listeners.wkeydown) f(original);
const passthrough = { isTrusted: true, ctrlKey: true, shiftKey: true, altKey: true, metaKey: false, code: 'Digit1',
  target, preventDefault() { throw new Error('should not prevent'); }, stopImmediatePropagation() {} };
for (const f of listeners.wkeydown) f(passthrough);
console.log(JSON.stringify({sent, dispatched: dispatched.map(e => ({key: e.key, meta: e.metaKey, shift: e.shiftKey, ctrl: e.ctrlKey})),
  prevented, stopped, hasNative: 'nativeControl' in window.webkit.messageHandlers}));
""".replace("SCRIPT", json.dumps(bridge))
        out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual(result["sent"], [{"name": "browser", "body": {"action": "open", "id": "x"}}])
        self.assertEqual(result["dispatched"][0], {"key": "t", "meta": True, "shift": False, "ctrl": False})
        self.assertEqual(result["dispatched"][1], {"key": "1", "meta": True, "shift": True, "ctrl": False})
        self.assertTrue(result["prevented"] and result["stopped"])
        self.assertFalse(result["hasNative"])


class FakePty:
    """模拟 pywinpty.PtyProcess：依次返回给定输出，读完后 EOFError。"""

    def __init__(self, chunks, code=0):
        self.chunks = list(chunks)
        self.exitstatus = None
        self.code = code
        self.pid = 4242
        self.written = []

    def read(self, _size):
        if self.chunks:
            return self.chunks.pop(0)
        self.exitstatus = self.code
        raise EOFError

    def isalive(self):
        return self.exitstatus is None

    def write(self, text):
        self.written.append(text)

    def setwinsize(self, rows, cols):
        self.size = (rows, cols)


class WindowsTerminalReader(unittest.TestCase):
    def test_reader_buffers_titles_and_exit(self):
        import termpty
        proc = FakePty(["\x1b]0;构建中\x07hello ", "世界\x1b[?1049h"], code=2)
        closed = threading.Event()
        info = {}
        sess = {"id": "win-test", "pid": proc.pid, "master": None, "proc": proc, "buf": bytearray(),
                "sink": None, "on_close": lambda i: (info.update(i), closed.set()), "alive": True,
                "osc_title": "", "modes": {}}
        termpty._PTY["win-test"] = sess
        with mock.patch("provision.setup_tasks.finish_terminal"):
            termpty._win_reader(sess)
            self.assertTrue(closed.wait(5))
        self.assertIn("hello 世界".encode(), bytes(sess["buf"]))
        self.assertEqual(sess["osc_title"], "构建中")
        self.assertTrue(sess["modes"].get(1049))
        self.assertFalse(sess["alive"])
        self.assertNotIn("win-test", termpty._PTY)
        self.assertEqual(info["code"], 2)

    def test_input_decodes_split_utf8(self):
        import codecs
        import termpty
        proc = FakePty([])
        sess = {"id": "win-input", "master": None, "proc": proc, "decoder": codecs.getincrementaldecoder("utf-8")("replace")}
        termpty._PTY["win-input"] = sess
        try:
            data = "中".encode()
            self.assertTrue(termpty.pty_input("win-input", data[:2]))
            self.assertTrue(termpty.pty_input("win-input", data[2:]))
            self.assertEqual("".join(proc.written), "中")
            termpty.pty_resize("win-input", 100, 30)
            self.assertEqual(proc.size, (30, 100))
        finally:
            termpty._PTY.pop("win-input", None)


class Launcher(unittest.TestCase):
    def test_secrets_parsing_keeps_launcher_path(self):
        import desk_launcher
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp, "env")
            secrets.write_text("# comment\nexport A_TOKEN='x y'\nPATH=/evil\nB=2\nbad line\n")
            env = {"PATH": "keep"}
            with mock.patch.object(desk_launcher, "SECRETS", str(secrets)):
                desk_launcher.load_secrets(env)
        self.assertEqual(env, {"PATH": "keep", "A_TOKEN": "x y", "B": "2"})

    def test_tool_shims(self):
        import desk_launcher
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(desk_launcher, "LOCAL_BIN", tmp):
                desk_launcher.install_tool_shims()
            names = sorted(os.listdir(tmp))
            self.assertIn("desk-task", names)
            self.assertIn("desk-task.cmd", names)
            self.assertIn("shepherd.cmd", names)
            sh = Path(tmp, "shepherd").read_text(encoding="utf-8")
            self.assertTrue(sh.startswith("#!/bin/sh"))
            self.assertIn('"-m" "shepherd.cli"', sh)
            cmd = Path(tmp, "desk-task.cmd").read_bytes()
            self.assertIn(b"\r\n", cmd)
            self.assertIn(b"%*", cmd)

    def test_windows_runtime_spec_matches_posix_installer(self):
        spec = json.loads((LIB / "provision" / "python-runtime.json").read_text(encoding="utf-8"))
        shell = (LIB / "provision" / "python_runtime.sh").read_text(encoding="utf-8")
        self.assertIn('DESK_PYTHON_VERSION="%s"' % spec["version"], shell)
        self.assertIn('DESK_PYTHON_TAG="%s"' % spec["tag"], shell)

    def test_installer_is_utf8_with_bom(self):
        raw = (ROOT / "scripts" / "windows" / "install.ps1").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "Windows PowerShell 5.1 需要 BOM 才按 UTF-8 读取")

    def test_batch_entry_is_ascii_crlf(self):
        raw = (ROOT / "desktron" / "bin" / "desktron.cmd").read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


class AppControlChannel(unittest.TestCase):
    def test_channel_address(self):
        from app_control import capability
        with mock.patch.object(capability.oscompat, "IS_WINDOWS", False):
            self.assertTrue(capability.channel_address("/d").endswith("app-control.sock"))
        with mock.patch.object(capability.oscompat, "IS_WINDOWS", True):
            capability.set_loopback_port(0)
            self.assertEqual(capability.channel_address("/d"), "")
            capability.set_loopback_port(54321)
            self.assertEqual(capability.channel_address("/d"), "tcp:127.0.0.1:54321")
            capability.set_loopback_port(0)

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_node_client_channel_target(self):
        script = ("import('%s').then(m => { console.log(JSON.stringify([m.channelTarget('tcp:127.0.0.1:5000'), "
                  "m.channelTarget('/tmp/a.sock')])); try { m.channelTarget('tcp:example.invalid:1'); } "
                  "catch (e) { console.log(e.code || e.message); } })") % (LIB / "web_tools" / "app_control_client.mjs").as_uri()
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(json.loads(lines[0]), [{"host": "127.0.0.1", "port": 5000}, {"socketPath": "/tmp/a.sock"}])
        self.assertIn("browser_unavailable", lines[1])


if __name__ == "__main__":
    unittest.main()
