"""curl 一条命令装机:发布脚本生成可静态托管的安装目录。

release.sh 默认只产本地 tarball;带 --site <base-url> 时额外生成 dist/site/:按架构命名的
tarball、release.json、注入好读地址的 install 引导脚本和 index.html。上传由发布者自行完成。
首次回源时部分 CDN 会先返回 404 而正文完整,重试即 200,所以引导脚本要带重试。
"""
import os
import re
import hashlib
import subprocess
import sys
import tempfile
import tarfile
import unittest
from pathlib import Path

# 全量套件里有用例会短暂替换共享 subprocess 模块的 run；这里包含真实子进程
# 断言，导入时保留原函数，避免后台用例撞进替换窗口。
_REAL_RUN = subprocess.run

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REL = Path(ROOT, "scripts", "dev", "release.sh").read_text(encoding="utf-8")
BOOT = Path(ROOT, "scripts", "dev", "bootstrap.sh").read_text(encoding="utf-8")
MAN = Path(ROOT, "manifest.yaml").read_text(encoding="utf-8")


class 发布通道(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'macOS tar 发布格式；Windows 发行使用 ZIP')
    def test_归档不携带苹果元数据且更新器可解包(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_archive_check", Path(ROOT, "desktron/lib/self_update_apply.py"))
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        command = next(line for line in REL.splitlines() if '-czf "$OUT"' in line)
        with tempfile.TemporaryDirectory() as td:
            package = Path(td, "desk"); package.mkdir()
            (package / "hello").write_text("release")
            (package / "._hello").write_text("metadata")
            (package / ".DS_Store").write_text("metadata")
            if sys.platform == "darwin":
                _REAL_RUN(["xattr", "-w", "com.apple.test", "metadata", str(package)], check=True)
            archive = Path(td, "release.tgz")
            env = dict(os.environ, OUT=str(archive), WORK=td, NAME="desk")
            _REAL_RUN(["bash", "-c", command], env=env, check=True, capture_output=True)
            with tarfile.open(archive) as tf:
                self.assertEqual({n.split('/')[0] for n in tf.getnames()}, {"desk"})
                self.assertFalse(any('/._' in n or n.endswith('.DS_Store') for n in tf.getnames()))
            extracted = updater.safe_extract(str(archive), str(Path(td, "out")))
            self.assertEqual(Path(extracted, "hello").read_text(), "release")

    def test_站点输出是可选参数且不上传(self):
        """默认只产本地 tarball;--site 生成可静态托管的目录,上传由发布者自行完成。"""
        self.assertIn("--site)", REL)
        self.assertIn('PUB="$ROOT/dist/site"', REL)
        self.assertNotIn("--publish-", REL)

    def test_所有发布输出要求干净树(self):
        """默认本地发行也必须对上干净快照，不能仅在生成站点时检查。"""
        i = REL.index("status --porcelain")
        head = REL[max(0, i - 200):i]
        self.assertNotIn('if [ -n "$SITE_BASE" ]', head)
        self.assertLess(i, REL.index('refactor-gate.sh" full'))
        self.assertLess(REL.index('build-builtin-apps.sh"\n'), REL.index('refactor-gate.sh" full'))

    def test_读地址注进_bootstrap(self):
        """bootstrap 仓库里存的是带占位符的模板,发布时统一注入。"""
        self.assertIn("__READ_BASE__", BOOT)
        self.assertIn("render_bootstrap", REL)
        self.assertIn('s#__READ_BASE__#$base#g', REL)

    def test_按架构命名(self):
        """一个入口显式携带两种架构的文件与校验和。"""
        for key in ("ARM64_FILE", "ARM64_SHA256", "X86_64_FILE", "X86_64_SHA256"):
            self.assertIn(key, BOOT)
        self.assertIn('case "$ARCH"', BOOT)


class 静态站点(unittest.TestCase):
    def test_目录里要有_index_html(self):
        """多数静态托管要求目录里有 index.html。"""
        i = REL.index('echo "[site] 已生成')
        head = REL[:i]
        self.assertIn('"$PUB/index.html"', head, "站点目录没有生成 index.html")

    def test_首页写着装机命令(self):
        """人点进这个地址时该看到怎么装,而不是目录列表或 404。"""
        i = REL.index('"$PUB/index.html"')
        seg = REL[i:i + 1200]
        self.assertIn("curl -fsSL $RBASE/install | bash", seg)


class 装完不能报失败(unittest.TestCase):
    """生成的 install.sh 在 set -e 下跑 git rev-parse,而 tarball 解开后没有 .git——
    git 返回 128,脚本在文件全都装好之后仍以失败退出。实测确认:退出码 128。
    这是最坏的一种失败:装成了,人看到的却是错误(2026-09-03 另一会话 review 指出)。"""

    def _install_sh(self):
        # heredoc 的结束标记是 INSTALL(cat > ... <<'INSTALL'),不是 SH。
        i = REL.index("cat > \"$STAGE/install.sh\"")
        return REL[i:REL.index("\nINSTALL\n", i)]

    def test_取版本号不会中断装机(self):
        seg = self._install_sh()
        self.assertIn("_rev=", seg)
        for m in re.finditer(r'_rev="\$\((.*?)\)"', seg):
            self.assertIn("|| true", m.group(1), "取版本号那一步没兜住,失败会让整个装机退出")

    def test_提交号写进包(self):
        """靠装机时 git rev-parse 只在「从 git clone 装」那条路有效,而 curl 装机
        恰恰不是那条路。发布时写进去,装的时候读。"""
        self.assertIn(".release-rev", REL)
        read = REL.index('_release_rev="$(git -C "$ROOT" rev-parse HEAD')
        write = REL.index('> "$STAGE/payload/.release-rev"')
        self.assertIn("|| true", REL[read:read + 140], "发布侧取不到提交号也不该中断打包")
        self.assertLess(read, REL.index("app-install swift"), "壳与 payload 必须使用同一提交号")
        self.assertLess(write, REL.index('echo "[3/4]'), "要在打包 install.sh 之前写进 payload")

    def test_装机优先读包里那份(self):
        seg = self._install_sh()
        self.assertLess(seg.index(".release-rev"), seg.index("rev-parse"),
                        "包里那份要排在 git 之前")


class 缺运行时能自己补(unittest.TestCase):
    """curl 装机的人是在一台全新的 Mac 上。python3 与 git 随系统/CLT 自带,
    实际缺的几乎总是 node——而缺了 node 引擎一个都装不上,Desk 也起不来。
    原来只打印一行「按上面命令补齐」就退出,等于把人挡在门外。"""

    BIN = open(os.path.join(ROOT, "desktron", "bin", "desktron"), encoding="utf-8").read()

    def test_体检失败先尝试补装(self):
        i = self.BIN.index("== 依赖体检 ==")
        seg = self.BIN[i:i + 1600]
        self.assertIn("--emit-missing-runtime", seg)
        self.assertIn("补装缺失的运行时", seg)

    def test_补完要复检(self):
        """装了不等于装成了:brew 不在时那条命令只会 echo 一句提示。"""
        i = self.BIN.index("补装缺失的运行时")
        seg = self.BIN[i:i + 900]
        self.assertIn("仍然缺失", seg)
        self.assertIn("exit 1", seg)

    def test_只补真的缺的(self):
        """全都打印出来的话,装机会把已经装好的 python3 再装一遍。"""
        src = Path(ROOT, "desktron", "lib", "check_deps.py").read_text(encoding="utf-8")
        i = src.index("def emit_missing_runtime")
        seg = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("probe(", seg)
        self.assertIn("ver_tuple", seg)   # 版本过低也算缺

    def test_node_下限对齐最高的那家(self):
        """写 18 的话体检说够了,装 pi 时 npm 才报不兼容——那时人已经走完大半流程。"""
        m = re.search(r"name: node\n\s+min: \"([^\"]+)\"", MAN)
        self.assertIsNotNone(m, "manifest 里找不到 node 的下限")
        self.assertGreaterEqual(tuple(int(x) for x in m.group(1).split(".")), (22, 19))


class 发布清单(unittest.TestCase):
    """六条 review 意见里有四条同一个根源:发布只是「把文件推上去」,没有清单。
    没有清单就没有版本、没有校验、没有架构索引、没有过期信息,于是每一样都要
    单独打补丁。清单是这四条的共同解(2026-09-03)。"""

    def test_发布产出清单(self):
        self.assertIn("release.json", REL)
        i = REL.index('"$PUB/release.json"')
        self.assertLess(i, REL.index('echo "[site] 已生成'), "清单要在站点完成之前生成")

    def test_清单里有四样(self):
        i = REL.index('cat > "$PUB/release.json"')
        seg = REL[i:i + 900]
        for k in ("rev", "expires_at", "sha256", "artifacts"):
            self.assertIn(k, seg, "清单里缺 " + k)

    def test_两个架构都发(self):
        """.app 是 universal、payload 不分架构,两份内容一样。不发的话 Intel 机器
        按 uname -m 取会 404,而那个错误指不出真正的原因。"""
        # 2026-09-03 起是有条件的:先验 .app 真是 universal,不是就只发实际有的那个
        # (见「发布前的自检」那一组)。所以断言两条:双架构那一路存在,且清单按实际登记。
        self.assertIn("for _a in arm64 x86_64", REL)
        self.assertIn("_pub_archs", REL)
        i = REL.index('cat > "$PUB/release.json"')
        self.assertIn("$_pub_archs", REL[i:i + 900], "清单没按实际发布的架构生成")

class 装机使用发布快照(unittest.TestCase):
    def test_不在用户机解析_json(self):
        """bootstrap 与包同批发布，版本和校验和直接注入；JSON 排版不再影响安装。"""
        self.assertNotIn('$BASE/release.json', BOOT)
        self.assertNotIn("MANIFEST_OK", BOOT)
        self.assertNotIn("sed -n", BOOT)
        for marker in ("__RELEASE_REV__", "__ARM64_FILE__", "__ARM64_SHA256__"):
            self.assertIn(marker, BOOT)

    def test_发布器一次注入全部字段(self):
        i = REL.index("render_bootstrap()")
        seg = REL[i:REL.index('if [ -n "$SITE_BASE" ]', i)]
        for marker in ("__READ_BASE__", "__RELEASE_REV__", "__RELEASE_CHANNEL__",
                       "__RELEASE_EXPIRES_AT__", "__ARM64_FILE__", "__ARM64_SHA256__",
                       "__X86_64_FILE__", "__X86_64_SHA256__"):
            self.assertIn(marker, seg)
        self.assertIn("仍有未注入字段", seg)

    def test_缺注入或校验和时失败关闭(self):
        self.assertIn('case "$BASE$RELEASE_REV$TARBALL$WANT_SHA"', BOOT)
        self.assertIn('if [ -z "$TARBALL" ] || [ -z "$WANT_SHA" ]', BOOT)
        self.assertNotIn("按老方式装", BOOT)

    def test_校验下载的包(self):
        """CDN 回源、代理、传输截断都可能给出一个能下完却不完整的文件,
        而 tar 未必报错——装到一半才出问题最难查。"""
        self.assertIn("shasum -a 256", BOOT)
        self.assertIn("校验失败", BOOT)
        self.assertLess(BOOT.index("shasum -a 256"), BOOT.index("tar xzf"),
                        "要在解包之前校验")

    def test_临近过期出声(self):
        """到期那天悄无声息地 404,比提前知道糟得多。"""
        self.assertIn("RELEASE_EXPIRES_AT", BOOT)
        self.assertIn("天到期", BOOT)

    def test_不支持的架构明说(self):
        """清单里没有这个架构 = 明确不支持,比下到 404 再猜清楚得多。"""
        self.assertIn("没有发布 $ARCH 的包", BOOT)

    @unittest.skipIf(os.name == 'nt', 'macOS bootstrap shell 与 tgz 使用契约')
    def test_渲染后的入口按架构下载并校验(self):
        """跑真实 shell 控制流，不只检查源码里是否出现了某个字符串。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            package_root = root / "package" / "desktron-test"
            package_root.mkdir(parents=True)
            (package_root / "install.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            package = root / "package.tgz"
            _REAL_RUN(["tar", "czf", str(package), "-C", str(root / "package"), "desktron-test"], check=True)
            sha = hashlib.sha256(package.read_bytes()).hexdigest()

            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "uname").write_text('#!/bin/bash\nprintf "%s\\n" "$FAKE_ARCH"\n', encoding="utf-8")
            (fake_bin / "curl").write_text(
                '#!/bin/bash\nout=""; url=""\nwhile [ "$#" -gt 0 ]; do\n'
                '  case "$1" in -o) out="$2"; shift 2;; -*) shift;; *) url="$1"; shift;; esac\n'
                'done\nprintf "%s" "$url" > "$FAKE_URL_LOG"\ncp "$FAKE_PACKAGE" "$out"\n',
                encoding="utf-8",
            )
            for tool in (fake_bin / "uname", fake_bin / "curl"):
                tool.chmod(0o755)

            replacements = {
                "__READ_BASE__": "https://preview.invalid",
                "__RELEASE_REV__": "abc123def456",
                "__RELEASE_CHANNEL__": "test",
                "__RELEASE_EXPIRES_AT__": "",
                "__ARM64_FILE__": "arm.tgz",
                "__ARM64_SHA256__": sha,
                "__X86_64_FILE__": "intel.tgz",
                "__X86_64_SHA256__": sha,
            }
            rendered = BOOT
            for old, new in replacements.items():
                rendered = rendered.replace(old, new)
            script = root / "install"
            script.write_text(rendered, encoding="utf-8")

            for arch, filename in (("arm64", "arm.tgz"), ("x86_64", "intel.tgz")):
                url_log = root / f"{arch}.url"
                env = os.environ.copy()
                env.update({
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "FAKE_ARCH": arch,
                    "FAKE_PACKAGE": str(package),
                    "FAKE_URL_LOG": str(url_log),
                })
                result = _REAL_RUN(["/bin/bash", str(script)], env=env, text=True,
                                   capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(url_log.read_text(encoding="utf-8"), f"https://preview.invalid/{filename}")
                self.assertIn("校验通过", result.stdout)


class 装不成要能退回去(unittest.TestCase):
    """rsync --delete 中途失败会留下一个既不是旧版也不是新版的目录,Desk 从那份起不来,
    人也不知道怎么退回去。.app 那处先删后拷,失败了连旧的都没了(2026-09-03 review)。"""

    def _install_sh(self):
        i = REL.index("cat > \"$STAGE/install.sh\"")
        return REL[i:REL.index("\nINSTALL\n", i)]

    def test_同步前先完成可恢复备份(self):
        seg = self._install_sh(); sync = seg.index("rsync -a --delete")
        self.assertLess(seg.index('BAK="$HOME/.local/share/desktron/home.prev"'), sync)
        self.assertLess(seg.index('cp -a "$DEST" "$_bn"'), sync)
        self.assertLess(seg.index('_home_backup=true'), sync)

    def test_同步失败由统一事务回滚(self):
        seg = self._install_sh(); sync = seg.index("rsync -a --delete")
        rollback = seg.index("rollback_install()")
        self.assertLess(rollback, sync)
        self.assertIn('mv "$BAK" "$DEST"', seg[rollback:sync])
        self.assertIn('trap rollback_install EXIT', seg[rollback:sync])

    def test_备份失败必须阻止覆盖(self):
        """没有完整备份时继续覆盖，会让后续 App 或验收失败无法恢复。"""
        seg = self._install_sh(); cp = seg.index('if ! cp -a "$DEST" "$_bn"')
        tail = seg[cp:seg.index('rsync -a --delete')]
        self.assertIn('拒绝覆盖', tail)
        self.assertIn('exit 1', tail)

    def test_app_先验签复制再三目录切换(self):
        seg = self._install_sh()
        copied = seg.index('cp -R "$_APP_SOURCE" "$_APP_NEW"')
        old_moved = seg.index('mv "$_APP_TARGET" "$_APP_PREV"')
        new_moved = seg.index('mv "$_APP_NEW" "$_APP_TARGET"')
        self.assertLess(copied, old_moved)
        self.assertLess(old_moved, new_moved)
        switch = seg.index('echo "→ 装 .app 到 ~/Applications"')
        self.assertNotIn('rm -rf "$_APP_TARGET"', seg[switch:new_moved])

    def test_提交后才清理旧名与回滚副本(self):
        """2026-08-16 之前的小写安装只在完整验收提交后删除。"""
        seg = self._install_sh(); committed = seg.index('_committed=true', seg.index('install-check'))
        cleanup = seg.index('rm -rf "$_APP_PREV"', committed)
        self.assertLess(committed, cleanup)
        self.assertIn('! [ "$_APP_LOWER" -ef "$_APP_TARGET" ]', seg[cleanup:])

    @unittest.skipUnless(os.name != 'nt' and os.path.isfile('/bin/bash'), 'POSIX shell required')
    def test_旧名清理保留新应用且只删除不同对象(self):
        seg = self._install_sh()
        start = seg.index('_committed=true', seg.index('install-check'))
        cleanup = seg[start:seg.index('\necho\n', start)]
        for mode in ('fresh', 'same-object', 'distinct-object'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                target = root / 'Desktron.app'
                target.mkdir()
                (target / 'installed').write_text('new', encoding='utf-8')
                previous = root / '.Desktron.app.prev'
                previous.mkdir()
                lower = root / 'desktron.app'
                if mode == 'same-object' and not lower.exists():
                    # 大小写敏感卷上通过别名复现相同 inode，APFS 默认卷则自然命中。
                    lower.symlink_to(target, target_is_directory=True)
                elif mode == 'distinct-object':
                    lower = root / 'Legacy.app'
                    lower.mkdir()
                env = {**os.environ, '_PLATFORM': 'Darwin', '_APP_TARGET': str(target),
                       '_APP_LOWER': str(lower), '_APP_PREV': str(previous)}
                result = _REAL_RUN(['/bin/bash', '-e', '-c', cleanup], env=env,
                                   capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((target / 'installed').read_text(encoding='utf-8'), 'new')
                self.assertFalse(previous.exists())
                if mode == 'distinct-object':
                    self.assertFalse(lower.exists())


class 装机不能预设环境(unittest.TestCase):
    """第二轮 review 的核心一条:bootstrap 在解包前用 python3 解析清单,而干净机器
    未必有 python3——取到空字段,被误报成「这一版没有发布该架构」,把人指向完全错误的
    方向(2026-09-03 实测确认)。装机的第一步不能依赖一个可能不在的东西。"""

    def test_解析清单不用_python(self):
        seg = BOOT[:BOOT.index("下载 $TARBALL")]
        self.assertNotIn("python3 ", seg, "下载前仍然执行 python3")
        self.assertNotIn('$BASE/release.json', seg, "下载前仍在读取远端 JSON")

    def test_日期比较也不用_python(self):
        i = BOOT.index("# 过期提醒")
        seg = BOOT[i:i + 700]
        self.assertNotIn("python3", seg)
        self.assertIn("date -j", seg)   # macOS 自带

    def test_架构结论来自发布时注入(self):
        i = BOOT.index("没有发布 $ARCH")
        self.assertIn('if [ -z "$TARBALL" ]', BOOT[:i])

    def test_探测认返回码(self):
        """which 命中只说明路径上有那个名字——链接断了、架构不对、依赖缺失时它照样在,
        而 --version 会非零退出。忽略 returncode 会把「装了但用不了」判成 ok,
        自动补装那一步根本不触发。"""
        src = Path(ROOT, "desktron", "lib", "check_deps.py").read_text(encoding="utf-8")
        i = src.index("def probe")
        seg = src[i:src.index("\ndef ", i + 10)]
        self.assertIn("r.returncode != 0", seg)
        self.assertIn("return False", seg)

    def test_最低版本读不到也算失败(self):
        src = Path(ROOT, "desktron", "lib", "check_deps.py").read_text(encoding="utf-8")
        self.assertIn("mn and (not ver or", src)

    @unittest.skipIf(os.name == 'nt', 'POSIX shebang 启动夹具；Windows 版本探测另测')
    def test_版本命令成功但无版本号仍阻断(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            node = fake_bin / "node"
            node.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            node.chmod(0o755)
            (root / "manifest.yaml").write_text(
                'requirements:\n  - kind: runtime\n    name: node\n    min: "22.19"\n    hint: "install node"\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
            script = str(Path(ROOT, "desktron", "lib", "check_deps.py"))
            # 全量套件里的 SIGCHLD 收割器可能先拿走 Python 子进程状态，使父进程读到
            # 假的 rc=0。让内层 bash 把真实状态写进 stdout，外层只读取文本。
            result = _REAL_RUN(
                ["/bin/bash", "-c", '"$1" "$2" "$3"; echo "RC=$?"',
                 "_", sys.executable, script, str(root)],
                env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertIn("RC=1", result.stdout)
            self.assertIn("版本读不到", result.stdout)


class 发布前的自检(unittest.TestCase):
    def test_发两个架构前先验_universal(self):
        """app-install 双架构编译失败时会退回本机单架构,而发布照样复制成两个名字——
        Intel 用户装到一个跑不起来的壳,且看不出问题在哪。"""
        i = REL.index("lipo -archs")
        self.assertLess(i, REL.index('cat > "$PUB/release.json"'), "校验要在生成清单之前")
        self.assertIn("x86_64", REL[i:i + 900])

    def test_不是_universal_就只登记实际架构(self):
        """宁可明说「只有 arm64」,也不要给一个装不起来的 x86_64 条目。"""
        self.assertIn("_pub_archs", REL)
        i = REL.index("lipo -archs")
        self.assertIn("只发这一个架构", REL[i:i + 900])

    def test_备份先拷后换(self):
        """先 rm 旧备份再 cp,中途失败会同时丢掉退路又留下半份目录。"""
        copied = REL.index('cp -a "$DEST" "$_bn"')
        removed = REL.index('rm -rf "$BAK"', copied)
        self.assertIn('BAK.new', REL[:copied])
        self.assertLess(copied, removed, "要先拷成功再删旧备份")


class 下载要重试(unittest.TestCase):
    def test_有重试(self):
        """CDN 首次回源未命中会返回 404 而正文完整(实测)。不重试的话第一个装的人
        莫名失败,而那个 404 指向完全错误的方向——他会以为包没发出去。"""
        self.assertIn("_get()", BOOT)
        self.assertRegex(BOOT, r"for i in 1 2 3")

    def test_重试之间有间隔(self):
        """连着打三次多半还是同一个结果,回源要时间。"""
        i = BOOT.index("_get()")
        self.assertIn("sleep", BOOT[i:i + 400])

    def test_仍会失败(self):
        """重试不是无限:三次都不成就如实退出,别把人挂在那儿。"""
        i = BOOT.index("_get()")
        seg = BOOT[i:i + 400]
        self.assertIn("return 1", seg)
        self.assertIn("下载失败", BOOT)


class 排除规则要真的生效(unittest.TestCase):
    """payload-excludes.txt 里的目录级规则必须挡得住,不能只是写着好看。

    2026-09-09 发现:--files-from 与 --exclude-from 写在同一条 rsync 上时,清单里点名的
    文件不再过排除规则。/docs 那条于是长期落空,docs/ 一直被打进发行包;它只有 1.6MB,
    所以谁也没看出来。文档重组让 docs/ 涨到 22MB,发行包从 23MB 跳到 39MB,两个架构
    合计 77MiB,超过静态托管常见的 50MiB 上限,这才露头。
    """

    def test_两个选项不同处一条命令(self):
        for line in REL.splitlines():
            if "rsync" not in line or line.strip().startswith("#"):
                continue
            self.assertFalse("--files-from" in line and "--exclude-from" in line,
                             "排除规则会对点名传输的文件失效:" + line.strip())

    def test_先取git可见再套排除(self):
        take = REL.index('--files-from="$FILES"')
        drop = REL.index('--exclude-from="$EXC"', take)
        self.assertLess(take, drop, "顺序反了:要先取 git 可见的文件,再套排除规则")
        self.assertIn('rsync -a --exclude-from="$EXC" "$RAW/" "$STAGE/payload/"', REL)

    def test_根目录与嵌套测试缓存都不进入载荷(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("rsync"):
            self.skipTest("本机没有 rsync")
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = (Path(tmp, x) for x in ("src", "dst"))
            for relative in (".pytest_cache", "desktron/.pytest_cache"):
                cache = src / relative
                cache.mkdir(parents=True)
                (cache / "nodeids").write_text("synthetic test cache", encoding="utf-8")
            (src / "keep.txt").write_text("payload", encoding="utf-8")
            dst.mkdir()
            subprocess.run(["rsync", "-a", f"--exclude-from={Path(ROOT, 'scripts/payload-excludes.txt')}",
                            f"{src}/", f"{dst}/"], check=True, capture_output=True)
            self.assertTrue((dst / "keep.txt").is_file())
            self.assertFalse((dst / ".pytest_cache").exists())
            self.assertFalse((dst / "desktron/.pytest_cache").exists())

    def test_rsync语义没变(self):
        """规则真的挡得住——这条不读源码,直接跑一次 rsync 验行为。"""
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("rsync"):
            self.skipTest("本机没有 rsync")
        tmp = tempfile.mkdtemp()
        try:
            src, raw, dst = (Path(tmp, x) for x in ("src", "raw", "dst"))
            Path(src, "docs").mkdir(parents=True)
            Path(src, "docs", "a.md").write_text("drop me", encoding="utf-8")
            Path(src, "keep.txt").write_text("keep me", encoding="utf-8")
            listing = Path(tmp, "list")
            listing.write_bytes(b"docs/a.md\0keep.txt\0")
            exc = Path(tmp, "exc")
            exc.write_text("/docs\n", encoding="utf-8")
            raw.mkdir()
            dst.mkdir()
            subprocess.run(["rsync", "-a", "--from0", f"--files-from={listing}",
                            f"{src}/", f"{raw}/"], check=True, capture_output=True)
            subprocess.run(["rsync", "-a", f"--exclude-from={exc}",
                            f"{raw}/", f"{dst}/"], check=True, capture_output=True)
            self.assertTrue(Path(dst, "keep.txt").is_file())
            self.assertFalse(Path(dst, "docs", "a.md").exists(), "两步走之后 /docs 仍然没挡住")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class 设计稿的第三方产物不入库(unittest.TestCase):
    """gitignore 那条原来写死 docs/design-samples/vendor/,设计稿一搬家就失配。"""

    def test_按目录名匹配而不是写死路径(self):
        rules = Path(ROOT, ".gitignore").read_text(encoding="utf-8")
        self.assertIn("**/design-samples/**/vendor/", rules)

    def test_归档后的产物没留在库里(self):
        import subprocess
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "--", "*design-samples*/vendor/*"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "", "设计稿的 vendor 产物又进库了:\n" + out[:400])


if __name__ == "__main__":
    unittest.main()
