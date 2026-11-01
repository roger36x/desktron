"""pkgstore 内核回归(段1):寻址解析、plan 只读审查、install 注册、uninstall 逆操作、catalog 浏览。

不依赖网络:用本地 git 仓当「远端」(git clone 支持本地路径)。REPO_ROOT / DATA_HOME 指临时目录隔离;
domains._ensure_domain_dir 被 stub 成 no-op,避免拉起重供给逻辑。
跑:python3 -m unittest discover -s tests
"""
import os, sys, json, types, shutil, atexit, tempfile, subprocess, unittest

# --- 进程级隔离:argv/env 必须在 import deskcfg/pkgstore 之前设好 ---
_BASE = tempfile.mkdtemp(prefix="pkgstore-test-")
_REPO = os.path.join(_BASE, "repo_root")        # REPO_ROOT(运行态 WORKSPACE 仿真)
_FRONT = os.path.join(_BASE, "frontend")
_DATA = os.path.join(_BASE, "share", "data")    # DATA_HOME → PKG_HOME = .../share/packages
for d in (_REPO, _FRONT, os.path.dirname(_DATA)):
    os.makedirs(d, exist_ok=True)
os.environ["DESKTRON_DATA_HOME"] = _DATA
sys.argv = ["test", _FRONT, "0", "0", _REPO]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# stub domains:install 注册工作区后会调 _ensure_domain_dir,这里 no-op 掉
_dm = types.ModuleType("domains")
_dm._ensure_domain_dir = lambda d: None
sys.modules["domains"] = _dm

import pkgstore                      # noqa: E402
import workbench as _workbench       # noqa: E402
from workbench import (              # noqa: E402
    _wb_domains_read, _wb_apps_read, _wb_apps_write, _wb_installed_read,
    _domain_record_normalize,
)

# discovery 会先导入整棵测试树再执行。workbench 可能已被前一个模块按它的临时根导入，
# 因而不能假设上面的 argv 能重建常量；执行本模块时显式绑定、结束后恢复。
_WB_BINDINGS = {
    "REPO_ROOT": _REPO,
    "DATA_HOME": _DATA,
    "FRONTEND": _FRONT,
    "WB_STATE": os.path.join(_REPO, ".workbench", "state.json"),
    "WB_APPS": os.path.join(_REPO, ".workbench", "apps.json"),
    "WB_DOMAINS": os.path.join(_REPO, ".workbench", "domains.json"),
    "WB_HIDDEN": os.path.join(_REPO, ".workbench", "hidden.json"),
    "WB_APPS_REMOVED": os.path.join(_REPO, ".workbench", "apps-removed.json"),
    "WB_WS_DIR": os.path.join(_REPO, ".workbench", "workspaces"),
    "WB_INSTALLED": os.path.join(_REPO, ".workbench", "installed.json"),
    "WB_KB_GIT": os.path.join(_REPO, ".workbench", "kb-git.json"),
    "KB_GIT_HOME": os.path.join(os.path.dirname(_DATA), "kb"),
}
_PKG_BINDINGS = {
    "REPO_ROOT": _REPO,
    "DATA_HOME": _DATA,
    "FRONTEND": _FRONT,
    "PKG_HOME": os.path.join(os.path.dirname(_DATA), "packages"),
    "_PAGES_DIR": os.path.join(_REPO, ".workbench", "pages"),
    "_APPS_DIR": os.path.join(_REPO, ".workbench", "apps"),
    "WB_WS_DIR": _WB_BINDINGS["WB_WS_DIR"],
    "KB_GIT_HOME": _WB_BINDINGS["KB_GIT_HOME"],
}
_OLD_WB_BINDINGS = {k: getattr(_workbench, k) for k in _WB_BINDINGS}
_OLD_PKG_BINDINGS = {k: getattr(pkgstore, k) for k in _PKG_BINDINGS}


def setUpModule():
    for key, value in _WB_BINDINGS.items():
        setattr(_workbench, key, value)
    for key, value in _PKG_BINDINGS.items():
        setattr(pkgstore, key, value)


def tearDownModule():
    for key, value in _OLD_WB_BINDINGS.items():
        setattr(_workbench, key, value)
    for key, value in _OLD_PKG_BINDINGS.items():
        setattr(pkgstore, key, value)

atexit.register(lambda: shutil.rmtree(_BASE, ignore_errors=True))

_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git_commit(path):
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-q", "-m", "init"], env=_GIT_ENV, check=True)


def _make_repo(path, files):
    os.makedirs(path, exist_ok=True)
    for rel, content in files.items():
        fp = os.path.join(path, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
    _git_commit(path)


_PKG_MANIFEST = """\
name: incident-toolkit
version: 0.1.0
author: tester
description: test pkg
provides:
  workspaces:
    - id: incident
      name: 应急
      desc: d
      desk_role: ops
      icon: zap
      spec: workspaces/incident.md
      imports: workspaces/incident.yaml
      apps: [noise-triage, diag-alert]
  apps:
    - id: noise-triage
      label: 噪声
      agentic: false
      agent_operability: none
      agent_operability_reason: 尚未提供页面观察与操作 adapter
      page: pages/noise.html
    - id: diag-alert
      label: 诊断
      agentic: true
      agent_operability: none
      agent_operability_reason: 通过会话执行，没有独立操作界面
      who: 你是诊断助手
      what: 输出诊断
      how: 用 ${psm} 查
      where: 目标 ${psm}
      params: [{name: psm, label: PSM, required: true}]
      skill: missing-skill
  kb:
    - {name: sre-kb, path: /tmp/sre-kb, mode: ro}
requires:
  skills: [missing-skill]
"""

_PKG_FILES = {
    "desktron-pkg.yaml": _PKG_MANIFEST,
    "workspaces/incident.md": "# 应急\n职责说明…\n",
    "workspaces/incident.yaml": "imports: []\n",
    "pages/noise.html": "<html>noise</html>\n",
}

_CATALOG_FILES = {
    "desktron-catalog.yaml": "name: official\nentries:\n  - {name: a, path: app/a}\n  - {name: b, path: workspace/b}\n",
}


def _clean_state():
    """每个用例前清运行态注册表 + 包 clone 落点,保证用例间不串。"""
    shutil.rmtree(os.path.join(_REPO, ".workbench"), ignore_errors=True)
    shutil.rmtree(os.path.join(_REPO, "domains"), ignore_errors=True)
    shutil.rmtree(pkgstore.PKG_HOME, ignore_errors=True)


class AddressParse(unittest.TestCase):
    def test_ssh_with_ref_and_subpath(self):
        u, r, s = pkgstore._parse_address("git@git.example.com:alice/repo.git@v1#app/foo")
        self.assertEqual(u, "git@git.example.com:alice/repo.git")
        self.assertEqual(r, "v1")
        self.assertEqual(s, "app/foo")

    def test_ssh_no_ref(self):
        u, r, s = pkgstore._parse_address("git@host:user/repo")
        self.assertEqual(u, "git@host:user/repo")
        self.assertEqual(r, "")
        self.assertEqual(s, "")

    def test_ssh_no_slash_no_ref(self):
        u, r, s = pkgstore._parse_address("git@host:repo")
        self.assertEqual(u, "git@host:repo")
        self.assertEqual(r, "")

    def test_https_with_ref(self):
        u, r, s = pkgstore._parse_address("https://h/u/r.git@main")
        self.assertEqual(u, "https://h/u/r.git")
        self.assertEqual(r, "main")

    def test_local_path(self):
        u, r, s = pkgstore._parse_address("/tmp/x/repo")
        self.assertEqual(u, "/tmp/x/repo")
        self.assertEqual((r, s), ("", ""))


class LegacyWorkspaceRole(unittest.TestCase):
    def test_旧_agent_diagnose_不迁成_desk_role(self):
        row = _domain_record_normalize({"id": "incident", "agent": "diagnose"})
        self.assertNotIn("agent", row)
        self.assertEqual(row["desk_role"], "")


class PlanInstallUninstall(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.src = os.path.join(_BASE, "incident-toolkit-src")
        shutil.rmtree(self.src, ignore_errors=True)
        _make_repo(self.src, _PKG_FILES)

    def test_plan_readonly(self):
        rev = pkgstore.plan(self.src)
        self.assertTrue(rev["ok"], rev.get("errors"))
        self.assertEqual(rev["kind"], "package")
        self.assertEqual(rev["provides"]["workspaces"], ["incident"])
        self.assertEqual(set(rev["provides"]["apps"]), {"noise-triage", "diag-alert"})
        self.assertEqual(rev["provides"]["kb"], ["sre-kb"])
        self.assertEqual(rev["unmet_skills"], ["missing-skill"])
        self.assertEqual(rev["write_apps"], ["diag-alert"])
        self.assertIn("pages/noise.html", rev["html_sources"])
        self.assertEqual(rev["app_refs_unresolved"], [])
        # plan 只读:不写任何注册表
        self.assertEqual(_wb_domains_read(), [])
        self.assertEqual(_wb_installed_read(), [])

    def test_install_without_confirm_is_plan(self):
        rev = pkgstore.install(self.src, confirm=False)
        self.assertTrue(rev["ok"])
        self.assertNotIn("installed", rev)
        self.assertEqual(_wb_installed_read(), [])

    def test_plan_rejects_app_without_operability_choice(self):
        broken = os.path.join(_BASE, "missing-operability-src")
        shutil.rmtree(broken, ignore_errors=True)
        manifest = _PKG_MANIFEST.replace("      agent_operability: none\n", "", 1)
        _make_repo(broken, {**_PKG_FILES, "desktron-pkg.yaml": manifest})
        rev = pkgstore.plan(broken)
        self.assertFalse(rev["ok"])
        self.assertTrue(any("agent_operability 缺失" in e for e in rev["errors"]), rev["errors"])

    def test_install_and_uninstall(self):
        rev = pkgstore.install(self.src, confirm=True)
        self.assertTrue(rev.get("installed"), rev.get("errors"))

        # 工作流已退出包模型:包声明的 workspaces 不再注册成域(本地主动创建才有域)
        self.assertEqual(_wb_domains_read(), [])

        apps = {a["id"]: a for a in _wb_apps_read()}
        self.assertIn("noise-triage", apps)
        self.assertIn("diag-alert", apps)
        # non-agentic page 归一为 app id,且物化成 .workbench/pages/<id>.html(软链可达)
        self.assertEqual(apps["noise-triage"]["page"], "noise-triage")
        page = os.path.join(_REPO, ".workbench", "pages", "noise-triage.html")
        self.assertTrue(os.path.isfile(page))
        # 缺 skill 不阻断:diag-alert 仍注册,skill 字段保留
        self.assertEqual(apps["diag-alert"].get("skill"), "missing-skill")
        # app 带 uid;targets 仍按包内 workspace 声明落位(app 归属知识保留,域本身不注册)
        self.assertTrue(apps["noise-triage"].get("uid"))
        self.assertEqual(apps["noise-triage"]["targets"], ["incident"])
        # 记账一条;provides.workspaces 恒空(退出包模型后不再登记)
        recs = _wb_installed_read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["name"], "incident-toolkit")
        self.assertEqual(recs[0]["provides"]["workspaces"], [])

        # 卸载:注册表 + 物化 + clone + 记账全清
        out = pkgstore.uninstall("incident-toolkit")
        self.assertTrue(out["ok"])
        self.assertEqual(_wb_domains_read(), [])
        self.assertEqual([a["id"] for a in _wb_apps_read()], [])
        self.assertFalse(os.path.lexists(page))
        self.assertEqual(_wb_installed_read(), [])
        self.assertFalse(os.path.isdir(os.path.join(pkgstore.PKG_HOME, "incident-toolkit-src")))

    def test_install_idempotent(self):
        pkgstore.install(self.src, confirm=True)
        pkgstore.install(self.src, confirm=True)
        self.assertEqual(len(_wb_installed_read()), 1)
        self.assertEqual(_wb_domains_read(), [])   # 包不再注册域,装几遍都不该出现
        self.assertEqual([a["id"] for a in _wb_apps_read()].count("noise-triage"), 1)


_SUBPKG_FILES = {
    "app/foo/desktron-pkg.yaml": (
        "name: foo-app\nversion: 0.1.0\nauthor: tester\n"
        "provides:\n  apps:\n    - {id: foo, label: Foo, agentic: false, page: foo.html, "
        "agent_operability: none, agent_operability_reason: 尚未提供页面 adapter}\n"
    ),
    "app/foo/foo.html": "<html>foo</html>\n",
    "workspace/bar/desktron-pkg.yaml": "name: bar-ws\n",   # 另一个子路径,装 foo 时不应被碰
}


class Subpath(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.src = os.path.join(_BASE, "content-monorepo-src")
        shutil.rmtree(self.src, ignore_errors=True)
        _make_repo(self.src, _SUBPKG_FILES)

    def test_install_subpath_package(self):
        rev = pkgstore.install(self.src + "#app/foo", confirm=True)
        self.assertTrue(rev.get("installed"), rev.get("errors"))
        self.assertEqual(rev["name"], "foo-app")
        apps = {a["id"]: a for a in _wb_apps_read()}
        self.assertIn("foo", apps)
        # 装 app/foo 不应注册 workspace/bar
        self.assertEqual(_wb_domains_read(), [])
        rec = _wb_installed_read()[0]
        self.assertEqual(rec["subpath"], "app/foo")


class UserAppCollision(unittest.TestCase):
    """用户自建 app(make-app)与官方包同 id 时:装包不覆盖、卸载不误删用户的。"""
    def setUp(self):
        _clean_state()
        self.src = os.path.join(_BASE, "incident-toolkit-src")
        shutil.rmtree(self.src, ignore_errors=True)
        _make_repo(self.src, _PKG_FILES)
        # 用户自建 app:同 id noise-triage,本机用户命名空间 uid
        _wb_apps_write([{"id": "noise-triage", "label": "我的噪声", "agentic": False,
                         "page": "noise-triage", "uid": "localuser.noise-triage.zzzzzz",
                         "targets": ["_desktop"]}])

    def test_install_does_not_clobber_user_app(self):
        pkgstore.install(self.src, confirm=True)
        ng = [a for a in _wb_apps_read() if a.get("id") == "noise-triage"]
        uids = {a["uid"] for a in ng}
        # 两个 noise-triage 并存:用户的(localuser.*)+ 包的(tester.*)
        self.assertIn("localuser.noise-triage.zzzzzz", uids)
        self.assertTrue(any(u.startswith("tester.noise-triage.") for u in uids), uids)

    def test_uninstall_keeps_user_app(self):
        pkgstore.install(self.src, confirm=True)
        pkgstore.uninstall("incident-toolkit")
        ng = [a for a in _wb_apps_read() if a.get("id") == "noise-triage"]
        # 包的被卸,用户的留下
        self.assertEqual([a["uid"] for a in ng], ["localuser.noise-triage.zzzzzz"])


class Catalog(unittest.TestCase):
    def setUp(self):
        _clean_state()
        self.src = os.path.join(_BASE, "official-catalog-src")
        shutil.rmtree(self.src, ignore_errors=True)
        _make_repo(self.src, _CATALOG_FILES)

    def test_plan_catalog(self):
        rev = pkgstore.plan(self.src)
        self.assertTrue(rev["ok"])
        self.assertEqual(rev["kind"], "catalog")
        self.assertEqual([e["name"] for e in rev["entries"]], ["a", "b"])

    def test_browse(self):
        out = pkgstore.browse(self.src)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
