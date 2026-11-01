"""Desktron 单仓发行边界回归。"""
import json
import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class SingleRepoDistribution(unittest.TestCase):
    def test_official_apps_are_complete_and_unique(self):
        data = json.loads(_read("frontend/workbench.json"))
        apps = data.get("desktopApps") or []
        expected = {
            "notepad", "excalidraw", "shell",
            "browser", "files", "file-browser", "taskpaper",
        }
        self.assertEqual({a.get("id") for a in apps}, expected)
        self.assertEqual(len(apps), len(expected))
        uids = [a.get("uid") for a in apps]
        self.assertTrue(all(uids))
        self.assertEqual(len(uids), len(set(uids)))

    def test_builtin_build_is_locked_and_release_owned(self):
        pkg = json.loads(_read("frontend/app-assets/package.json"))
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        for name in ("@excalidraw/excalidraw", "monaco-editor", "esbuild"):
            self.assertIn(name, deps)
            self.assertNotRegex(deps[name], r"^[~^><=*]")
        release = _read("scripts/dev/release.sh")
        self.assertIn("scripts/dev/build-builtin-apps.sh", release)
        self.assertIn('STAGE/payload/knowledge', release)
        self.assertIn("ls-files --cached --others --exclude-standard", release)
        self.assertIn("status --porcelain", release)
        excludes = _read("scripts/payload-excludes.txt")
        for path in ("/knowledge", "/.workbench", "/.agents", "/.codex", "/imp"):
            self.assertIn(path, excludes)
        # Pi 子代理携带薄适配层和固定上游锁文件，不携带发布机 node_modules。
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "desktron/lib/engine_adapters/pi_children.ts")))
        self.assertTrue(os.path.exists(os.path.join(ROOT, "desktron/lib/runtime/pi-subagents-package-lock.json")))
        self.assertIn("node_modules", excludes.splitlines())

    def test_old_automatic_sources_are_retired(self):
        for rel in (
            "desktron/lib/kbcache.py",
            "desktron/lib/provision_sources.py",
            "config/sources.yaml",
            "config/fallback-catalog.yaml",
            "imp/data/adapter/claude",
            "imp/data/adapter/claude/sessions.json",
            ".claude/skills/make-app/apps/incident-snapshot.json",
            ".claude/skills/make-app/apps/kb-gap-scan.json",
            ".claude/skills/make-app/apps/kb-trace.json",
            ".claude/skills/make-workspace/workspace-kb.json",
        ):
            self.assertFalse(os.path.exists(os.path.join(ROOT, rel)), rel)
        runtime = "\n".join(
            _read(rel) for rel in (
                "desktron/bin/desktron",
                "desktron/lib/deskcfg.py",
                "desktron/lib/pkgstore.py",
                "desktron/lib/ui_server.py",
            )
        )
        for token in (
            "desktron-apps.git", "desktron-workspaces.git",
            "desktron-skills.git", "desktron-kbs.git",
            "import kbcache", "provision_sources.py",
        ):
            self.assertNotIn(token, runtime)

    def test_builtin_build_installs_from_app_root(self):
        """内置 App 的依赖安装必须 cd 进 app-assets 跑,不能用 npm --prefix。

        npm 11 下 --prefix 只改安装落点,package.json 仍按当前目录解析,于是把目录名
        当成根包名,报 `Missing: app-assets@1.0.0 from lock file` 而中止——全新克隆
        按文档构建会当场失败(2026-09-01 干净克隆实测,修后一次通过)。
        这条守的是「拿得到」,不是「好不好」:构建断了,后面所有功能都无从谈起。"""
        src = _read("scripts/dev/build-builtin-apps.sh")
        self.assertNotRegex(src, r"npm\s+ci[^\n]*--prefix")
        self.assertRegex(src, r'cd "\$APP_ROOT"\s*&&\s*npm ci')

    def test_sync_home_builds_builtin_assets_before_touching_install(self):
        """干净 worktree 必须先重建内置 App，再触碰已安装 home。"""
        src = _read("desktron/bin/desktron")
        sync = src[src.index("  sync-home)"):src.index("  release)")]
        build = sync.index('bash "$PROJECT_ROOT/scripts/dev/build-builtin-apps.sh"')
        self.assertLess(build, sync.index('dest="$HOME/.local/share/desktron/home"'))
        self.assertLess(build, sync.index("rsync -a --delete"))
        self.assertNotIn("_missing_builtin", sync)

    def test_builtin_build_validates_every_runtime_asset(self):
        """构建成功必须覆盖启动期前端检查要求的全部内置 App 资源。"""
        build = _read("scripts/dev/build-builtin-apps.sh")
        for asset in (
            "excalidraw/vendor/app.bundle.js",
            "excalidraw/vendor/index.css",
            "excalidraw/vendor/fonts/Virgil/Virgil-Regular.woff2",
            "excalidraw/vendor/locales/zh-CN-*.js",
            "files/vendor/app.bundle.js",
            "files/vendor/app.bundle.css",
            "files/vendor/codicon.ttf",
            "files/vendor/editor.worker.js",
            "files/vendor/json.worker.js",
            "files/vendor/css.worker.js",
            "files/vendor/html.worker.js",
            "files/vendor/ts.worker.js",
        ):
            self.assertIn(asset, build)

    def test_dist_repo_is_retired_and_site_update_channel_is_configurable(self):
        """dist 仓 2026-09-01 下线:它是单仓化之前留下的派生物通道,而单仓之后源码
        本身就是完整可构建的一份。留着等于维护两条真相,且 update 会静默指向一个
        不再更新的副本——人对着不动的按钮会先怀疑网络,不会怀疑通道本身没了。"""
        manifest = _read("manifest.yaml")
        self.assertNotRegex(manifest, r'(?m)^dist_repo:')
        # 运行期不许再有取 dist_repo 或 clone dist 仓的动作
        for rel in ("desktron/bin/desktron", "desktron/lib/provision/api.py",
                    "scripts/dev/release.sh"):
            src = _read(rel)
            self.assertNotIn("desktron-dist.git", src, rel)
            self.assertNotIn("dist_repo", src, rel)
        # 自更新不复活 dist 仓；读取发布站点的 release.json，通过独立状态机
        # 检查、下载与应用，旧入口只作兼容触发。
        api = _read("desktron/lib/provision/api.py")
        self.assertIn('"/workbench/update-check"', api)
        self.assertIn('"/workbench/update/check"', api)
        self.assertIn('"/workbench/update/apply"', api)
        self.assertIn("self_update.status()", api)
        updater = _read("desktron/lib/self_update.py")
        self.assertIn("/release.json", updater)
        self.assertIn("DESKTRON_UPDATE_BASE", updater)
        # 未知参数明确报错而不是静默忽略:否则发布者以为推上去了。
        release = _read("scripts/dev/release.sh")
        self.assertIn("未知参数", release)
        self.assertIn("--site", release)

    def test_retired_design_drafts_are_gone(self):
        """kbkit 系列与两篇 refactor-prompt 是已落地/已废弃的方案稿,且 docs/ 随发行包走。
        它们各自只被同簇文档引用,删掉不留悬空链接。"""
        for name in ("kbkit-v2-positioning", "kbkit-v3-implementation", "kbkit-v3-desk-submodule",
                     "kbkit-current-state", "kbkit-gate-enhancements", "kbkit-refactor-prompt",
                     "kn-gate-gaps", "repo-issues", "asset-sharing-and-delivery",
                     "desk-refactor-prompt-1-runtime", "desk-refactor-prompt-2-skills"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, "docs", name + ".md")), name)

    def test_source_identity_and_bundled_docs(self):
        manifest = _read("manifest.yaml")
        self.assertIn("project: desktron", manifest)
        self.assertIn('source_repo: "https://github.com/roger36x/desktron.git"', manifest)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "desk-docs", "00-index.md")))


if __name__ == "__main__":
    unittest.main()
