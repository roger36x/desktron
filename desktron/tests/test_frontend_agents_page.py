"""代理页下线后的前端契约。"""
import os
import unittest
import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(*parts):
    if parts and parts[-1] in ("app-chat.js", "app-panel.js", "app-rail.js", "app-assets.js"):
        return _srcmap.frontend_src(parts[-1])
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class AgentsPageRetired(unittest.TestCase):
    def setUp(self):
        self.index = _read("frontend", "index.html")
        self.rail = _read("frontend", "app-rail.js")
        self.panel = _read("frontend", "app-panel.js")
        self.assets = _read("frontend", "app-assets.js")
        self.css = _read("frontend", "style.css")
        self.provision = _read("desktron", "lib", "provision", "api.py")
        self.remote = _read("desktron", "lib", "remote", "api.py")

    def test_sidebar_no_longer_exposes_agents_entry(self):
        self.assertNotIn('id="fx-workshop"', self.index)
        self.assertNotIn('</span>代理</button>', self.index)
        self.assertNotIn('</span>工坊</button>', self.index)
        self.assertNotIn('agents: { title: "代理"', self.rail)
        self.assertNotIn('"飞书 Bot"', self.rail)
        self.assertNotIn('"知识库"', self.rail)
        self.assertNotIn('"技能"', self.rail)

    def test_legacy_history_entries_fall_back_to_welcome(self):
        self.assertIn('e.kind === "bots" || e.kind === "agents"', self.rail)
        self.assertIn('e.id === "assets" || e.id === "kb"', self.rail)
        self.assertIn('return _restoreEngineWelcome()', self.rail)
        self.assertIn('代理资源页已经下线', self.rail)

    def test_retired_frontend_modules_are_not_bundled(self):
        for name in ("app-rail-agents.js", "app-rail-bots.js", "app-assets-kb.js", "app-assets-workshop.js"):
            self.assertNotIn(name, self.rail + self.assets)
        self.assertNotIn("openAgentsPage", self.rail)
        self.assertNotIn("openBotsPage", self.rail)
        self.assertNotIn("openKbPage", self.assets)
        self.assertNotIn("openSkillsPage", self.assets)

    def test_retired_page_css_is_gone(self):
        for marker in (".agents-page", ".bots-page", ".skills-page", ".kb-page", ".workshop-page",
                       ".agp-head", ".agp-resource-head", ".bk-entry", ".ws-tab"):
            self.assertNotIn(marker, self.css)
        self.assertNotIn("工坊·技能页", self.css)

    def test_page_only_http_routes_are_gone(self):
        for path in ("/workbench/agentbuddy", "/workbench/skillmarket", "/workbench/skill-ledger"):
            self.assertNotIn(path, self.provision)

    def test_runtime_system_agents_remain_under_arrangements(self):
        self.assertIn("function _arrSysCards(st)", self.panel)
        self.assertIn("function _arrSysCard(", self.panel)
        self.assertIn("function _arrSysRenderResource(", self.panel)
        self.assertIn("matchingSystem.map(_arrSysCard)", self.panel)
        self.assertIn('_arrSysRenderResource(body, card', self.panel)
        self.assertIn("记忆引擎", self.panel)
        self.assertNotIn("Lark Timeline", self.panel)
        self.assertIn('/workbench/agents/status', self.panel)
        self.assertIn('/workbench/agents/run-once', self.panel)
        self.assertNotIn('/workbench/lark-sync/start', self.panel)
        self.assertNotIn('/workbench/lark-sync/stop', self.panel)
        self.assertIn('"/workbench/agents/status"', self.remote)
        self.assertIn('"/workbench/agents/run-once"', self.remote)

    def test_arrangement_system_cards_use_their_own_css_namespace(self):
        for marker in (".arr-sys-head", ".arr-sys-resource-head", ".arr-sys-status-grid", ".arr-sys-job", ".arr-sys-logs"):
            self.assertIn(marker, self.css)
        self.assertNotIn("_agp", self.panel)


class ArrangementSystemCache(unittest.TestCase):
    """系统安排状态在列表和详情动作间复用，不退回空白加载态。"""

    def setUp(self):
        self.panel = _read("frontend", "app-panel.js")

    def test_缓存在安排模块级(self):
        self.assertIn('let _arrCache = { items: [], peers: [], agents: null, system: null, backend: "desktop", error: "" };', self.panel)

    def test_从缓存起手(self):
        self.assertIn("_arrSysCards(_arrCache.system || {})", self.panel)

    def test_取失败不清空缓存(self):
        self.assertIn('try { _arrCache.system = await api("/workbench/agents/status"', self.panel)
        self.assertIn("} catch {}", self.panel)


if __name__ == "__main__":
    unittest.main()
