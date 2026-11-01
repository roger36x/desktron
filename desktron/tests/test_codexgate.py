"""codexgate provisioning regressions.

Run: python3 -m unittest desktron/tests/test_codexgate.py
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

_BASE = tempfile.mkdtemp(prefix="codexgate-test-")
atexit.register(lambda: shutil.rmtree(_BASE, ignore_errors=True))
_FRONT = os.path.join(_BASE, "frontend")
_REPO = os.path.join(_BASE, "repo")
_DATA = os.path.join(_BASE, "share", "data")
for _d in (_FRONT, _REPO, os.path.dirname(_DATA)):
    os.makedirs(_d, exist_ok=True)

_ARGV = sys.argv[:]
_DATA_HOME = os.environ.get("DESKTRON_DATA_HOME")
_MODULES = ("deskcfg", "codexgate")
_OLD_MODULES = {name: sys.modules.get(name) for name in _MODULES}

for _name in _MODULES:
    sys.modules.pop(_name, None)

try:
    os.environ["DESKTRON_DATA_HOME"] = _DATA
    sys.argv = ["test", _FRONT, "0", "0", _REPO]
    import codexgate  # noqa: E402
finally:
    sys.argv = _ARGV
    if _DATA_HOME is None:
        os.environ.pop("DESKTRON_DATA_HOME", None)
    else:
        os.environ["DESKTRON_DATA_HOME"] = _DATA_HOME
    for _name in _MODULES:
        if _OLD_MODULES[_name] is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _OLD_MODULES[_name]


class EnsureCodexHome(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="codexgate-case-")
        self.user_codex = os.path.join(self.base, "user-codex")
        self.managed_codex = os.path.join(self.base, "desk", "codex-home")
        os.makedirs(os.path.join(self.user_codex, "skills", "demo-skill"), exist_ok=True)
        with open(os.path.join(self.user_codex, "skills", "demo-skill", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo-skill\ndescription: demo\n---\n")

        self.old_user_codex = codexgate.USER_CODEX
        self.old_codex_home = codexgate.CODEX_HOME
        codexgate.USER_CODEX = self.user_codex
        codexgate.CODEX_HOME = self.managed_codex

    def tearDown(self):
        codexgate.USER_CODEX = self.old_user_codex
        codexgate.CODEX_HOME = self.old_codex_home
        shutil.rmtree(self.base, ignore_errors=True)

    def test_links_user_skills_directory_into_managed_home(self):
        result = codexgate.ensure_codex_home()

        link = os.path.join(self.managed_codex, "skills")
        self.assertEqual(result, self.managed_codex)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.user_codex, "skills")))
        self.assertTrue(os.path.isfile(os.path.join(link, "demo-skill", "SKILL.md")))

    def test_replaces_system_only_skills_directory_with_user_skills_link(self):
        os.makedirs(os.path.join(self.managed_codex, "skills", ".system"), exist_ok=True)

        result = codexgate.ensure_codex_home()

        link = os.path.join(self.managed_codex, "skills")
        self.assertEqual(result, self.managed_codex)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isfile(os.path.join(link, "demo-skill", "SKILL.md")))

    def test_ds_store_does_not_block_skills_migration(self):
        os.makedirs(os.path.join(self.managed_codex, "skills", ".system"), exist_ok=True)
        with open(os.path.join(self.managed_codex, "skills", ".DS_Store"), "wb") as f:
            f.write(b"\x00")

        codexgate.ensure_codex_home()

        self.assertTrue(os.path.islink(os.path.join(self.managed_codex, "skills")))


class ConfigRefresh(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="codexgate-cfg-")
        self.user_codex = os.path.join(self.base, "user-codex")
        self.managed_codex = os.path.join(self.base, "desk", "codex-home")
        os.makedirs(self.user_codex, exist_ok=True)
        os.makedirs(self.managed_codex, exist_ok=True)

        self.old_user_codex = codexgate.USER_CODEX
        self.old_codex_home = codexgate.CODEX_HOME
        codexgate.USER_CODEX = self.user_codex
        codexgate.CODEX_HOME = self.managed_codex

    def tearDown(self):
        codexgate.USER_CODEX = self.old_user_codex
        codexgate.CODEX_HOME = self.old_codex_home
        shutil.rmtree(self.base, ignore_errors=True)

    def _write(self, base, text):
        path = os.path.join(base, "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _backups(self):
        return [n for n in os.listdir(self.managed_codex) if ".backup-stale-" in n]

    def _read_cfg(self):
        with open(os.path.join(self.managed_codex, "config.toml"), encoding="utf-8") as f:
            return f.read()

    def test_inherits_only_explicit_current_workspace_trust(self):
        workspace = os.path.realpath(codexgate.REPO_ROOT)
        self._write(self.user_codex, f'[projects.{json.dumps(workspace)}]\ntrust_level="trusted"\n[projects."/unrelated"]\ntrust_level="trusted"\n')
        path = self._write(self.managed_codex, 'model="keep-this-model"\n')
        codexgate._inherit_workspace_trust(path)
        first = self._read_cfg()
        data = codexgate.tomllib.loads(first)
        self.assertEqual(data['projects'], {workspace: {'trust_level': 'trusted'}})
        self.assertEqual(data['model'], 'keep-this-model')
        codexgate._inherit_workspace_trust(path)
        self.assertEqual(self._read_cfg(), first)

    def test_does_not_override_managed_untrusted_decision(self):
        workspace = os.path.realpath(codexgate.REPO_ROOT)
        self._write(self.user_codex, f'[projects.{json.dumps(workspace)}]\ntrust_level="trusted"\n')
        original = f'[projects.{json.dumps(workspace)}]\ntrust_level="untrusted"\n'
        path = self._write(self.managed_codex, original)
        codexgate._inherit_workspace_trust(path)
        self.assertEqual(self._read_cfg(), original)

    def test_missing_or_invalid_global_trust_does_not_grant_access(self):
        for source in ('', '[projects."/unrelated"]\ntrust_level="trusted"\n', 'invalid=[['):
            self._write(self.user_codex, source)
            path = self._write(self.managed_codex, 'model="keep"\n')
            codexgate._inherit_workspace_trust(path)
            self.assertEqual(self._read_cfg(), 'model="keep"\n')

    def test_inherits_explicit_untrusted_without_escalating(self):
        workspace = os.path.realpath(codexgate.REPO_ROOT)
        self._write(self.user_codex, f'[projects.{json.dumps(workspace)}]\ntrust_level="untrusted"\n')
        path = self._write(self.managed_codex, '')
        codexgate._inherit_workspace_trust(path)
        self.assertEqual(codexgate.tomllib.loads(self._read_cfg())['projects'][workspace]['trust_level'], 'untrusted')

    def test_refreshes_stale_catalog_from_healthy_user_config(self):
        self._write(self.managed_codex, 'model_catalog_json = "%s"\n' % os.path.join(self.base, "gone.json"))
        self._write(self.user_codex, 'model = "gpt-5"\n')

        codexgate.ensure_codex_home()

        cfg = self._read_cfg()
        self.assertIn('model = "gpt-5"', cfg)
        self.assertEqual(len(self._backups()), 1)

    def test_equally_stale_user_config_not_copied_and_no_backup_churn(self):
        stale = 'model_catalog_json = "%s"\n' % os.path.join(self.base, "gone.json")
        self._write(self.managed_codex, 'x = 1\n' + stale)
        self._write(self.user_codex, stale)

        codexgate.ensure_codex_home()
        codexgate.ensure_codex_home()

        cfg = self._read_cfg()
        self.assertIn("x = 1", cfg)                    # 本地内容未被覆盖
        self.assertEqual(self._backups(), [])          # 不反复堆备份

    def test_regex_fallback_when_tomllib_unavailable(self):
        old = codexgate.tomllib
        codexgate.tomllib = None
        try:
            self._write(self.managed_codex, 'model_catalog_json = "%s"\n' % os.path.join(self.base, "gone.json"))
            self._write(self.user_codex, 'model = "gpt-5"\n')

            codexgate.ensure_codex_home()

            cfg = self._read_cfg()
            self.assertIn('model = "gpt-5"', cfg)
        finally:
            codexgate.tomllib = old

    def test_replaces_stale_node_repl_with_desk_app_control_only(self):
        stale = '''model = "gpt-5"
[mcp_servers.node_repl]
command = "/Applications/Codex.app/old/node_repl"
[mcp_servers.node_repl.env]
CODEX_CLI_PATH = "/Applications/Codex.app/old/codex"
[mcp_servers.keep_me]
command = "keep"
[plugins."browser@openai-bundled"]
enabled = true
'''
        self._write(self.managed_codex, stale)

        codexgate.ensure_codex_home()

        cfg = self._read_cfg()
        self.assertIn('model = "gpt-5"', cfg)
        self.assertIn("[mcp_servers.keep_me]", cfg)
        self.assertNotIn("Applications/Codex.app", cfg)
        self.assertNotIn("[mcp_servers.node_repl]", cfg)
        self.assertIn("[mcp_servers.desk_app_control]", cfg)
        self.assertIn('[plugins."browser@openai-bundled"]\nenabled = false', cfg)

    def test_app_control_config_is_idempotent(self):
        self._write(self.managed_codex, 'model = "gpt-5"\n')
        codexgate.ensure_codex_home()
        first = self._read_cfg()
        codexgate.ensure_codex_home()
        self.assertEqual(self._read_cfg(), first)

    def test_app_control_uses_runtime_approval_and_current_desk_session(self):
        self._write(self.managed_codex, 'model = "gpt-5"\n')

        codexgate.ensure_codex_home()

        cfg = self._read_cfg()
        self.assertIsNotNone(codexgate.tomllib, "本测试需要 TOML 解析器验证配置结构")
        app_control = codexgate.tomllib.loads(cfg)["mcp_servers"]["desk_app_control"]
        self.assertEqual(app_control["default_tools_approval_mode"], "approve")
        self.assertEqual(app_control["env_vars"], [
            "DESKTRON_SESSION_ID",
            "DESKTRON_APP_CONTROL_SESSION_CAPABILITY",
            "DESKTRON_APP_CONTROL_SOCKET",
            "DESKTRON_UI_PORT",
            "DESKTRON_UI_URL",
        ])


if __name__ == "__main__":
    unittest.main()

class CleansStaleCodeModeKeys(unittest.TestCase):
    """清掉 Desk 曾经写坏的 code_mode 开关(2026-09-02)。

    d85fe97 把 code_mode/code_mode_host 写成 false,想让 MCP 工具改走 direct 面;
    实测证伪——code_mode_only 是模型自身属性,关掉只会掐断唯一通路,codex 每轮直接
    回 "code-mode host is disabled"。当天 d8a95c4 撤回了写入,但已经落盘的那两行不会
    自己消失:撤回后的代码既不写也不删,它们会永久留在各机器的 CODEX_HOME 里。
    """

    def _run(self, body):
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        codexgate._configure_app_control(path)
        with open(path, encoding="utf-8") as f:
            return path, f.read()

    def test_features表头带注释时不重复创建(self):
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('model = "x"\n\n[features] # local settings\nhooks = false\n')
        codexgate._enable_hooks_config(path)
        with open(path, encoding="utf-8") as f:
            cfg = f.read()
        self.assertEqual(cfg.count('[features]'), 1)
        self.assertTrue(codexgate.tomllib.loads(cfg)['features']['hooks'])

    def test_多行字符串中的伪表头不参与清理或hooks改写(self):
        body = ('notes = """\n[plugins."browser@openai-bundled"]\n'
                '[features]\nhooks = false\n"""\nmodel = "keep"\n')
        path, cfg = self._run(body)
        parsed = codexgate.tomllib.loads(cfg)
        self.assertIn('[plugins."browser@openai-bundled"]', parsed['notes'])
        self.assertEqual(parsed['model'], 'keep')
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        codexgate._enable_hooks_config(path)
        with open(path, encoding="utf-8") as f:
            enabled = codexgate.tomllib.loads(f.read())
        self.assertIn('hooks = false', enabled['notes'])
        self.assertTrue(enabled['features']['hooks'])

    def test_等价quoted受管表会被替换而非重复定义(self):
        body = ('[mcp_servers."desk_app_control"]\ncommand = "old"\n'
                '["plugins".\'browser@openai-bundled\']\nenabled = true\n')
        parser = codexgate.tomllib
        _, cfg = self._run(body)
        parsed = parser.loads(cfg)
        self.assertNotEqual(parsed['mcp_servers']['desk_app_control']['command'], 'old')
        self.assertFalse(parsed['plugins']['browser@openai-bundled']['enabled'])
        codexgate.tomllib = None
        try:
            _, fallback_cfg = self._run(body)
        finally:
            codexgate.tomllib = parser
        fallback = parser.loads(fallback_cfg)
        self.assertNotEqual(fallback['mcp_servers']['desk_app_control']['command'], 'old')
        self.assertFalse(fallback['plugins']['browser@openai-bundled']['enabled'])

    def test_清理受管段时保留后续普通表和数组表(self):
        _, cfg = self._run('[plugins."browser@openai-bundled"]\nenabled = true\n'
                           '[mcp_servers."user]name"]\ncommand = "keep"\n'
                           '[[groups."a]b"]]\nname = "also keep"\n')
        parsed = codexgate.tomllib.loads(cfg)
        self.assertEqual(parsed['mcp_servers']['user]name']['command'], 'keep')
        self.assertEqual(parsed['groups']['a]b'], [{'name': 'also keep'}])

    def test_清掉残留的两个键(self):
        _, cfg = self._run('model = "x"\n\n[features]\ncode_mode = false\n'
                           'code_mode_host = false\nhooks = true\njs_repl = false\n')
        self.assertNotIn("code_mode", cfg)        # 两行都要没
        self.assertIn("hooks = true", cfg)        # 别人的键照旧
        self.assertIn("js_repl = false", cfg)
        self.assertIn("[features]", cfg)          # 段本身不删

    def test_不再写回这两个键(self):
        _, cfg = self._run('model = "x"\n')
        self.assertNotIn("code_mode", cfg)        # 撤回之后不该再出现

    def test_只清features段里的同名键(self):
        # 别的段里恰好有同名键不归我们管
        _, cfg = self._run('model = "x"\n\n[somewhere_else]\ncode_mode = true\n')
        self.assertIn("code_mode = true", cfg)

    def test_清理幂等(self):
        path, first = self._run('model = "x"\n\n[features]\ncode_mode = false\nhooks = true\n')
        codexgate._configure_app_control(path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(first, f.read())

class McpCommandPointsAtRunningCopy(unittest.TestCase):
    """MCP 命令必须指向真正在跑的那份 Desk,并显式带解释器(2026-09-02 复发)。

    REPO_ROOT 在运行态指向 workspace,拼出的 desktron/bin/app-control-mcp 根本不存在,
    codex 每轮报 "MCP startup failed: No such file or directory (os error 2)"。
    正确来源是 codexgate 自己的位置(它与 bin/ 同属一份副本),并显式用当前 Python 启动,
    不依赖可执行位与 shebang 的二次解释器查找。
    """

    def test_命令是解释器脚本走args且路径存在(self):
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('model = "x"\n')
        codexgate._configure_app_control(path)
        with open(path, encoding="utf-8") as f:
            cfg = f.read()
        self.assertIn("command = " + json.dumps(sys.executable), cfg)
        m = re.search(r'args = \["([^"]+)"\]', cfg)
        self.assertIsNotNone(m, "MCP 必须用 args 传脚本,不能把脚本当 command")
        self.assertTrue(os.path.exists(m.group(1)), "写进配置的脚本路径不存在:" + m.group(1))
        self.assertNotIn("REPO_ROOT", cfg)
