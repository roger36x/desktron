"""domains skill index regression tests.

Run: python3 -m unittest desktron/tests/test_domains_skill_index.py
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock


_BASE = tempfile.mkdtemp(prefix="domains-skill-index-")
_REPO = os.path.join(_BASE, "repo")
_USER = os.path.join(_BASE, "user")
_FRONTEND = os.path.join(_BASE, "frontend")
_DATA = os.path.join(_BASE, "data")
for _d in (_REPO, _USER, _FRONTEND, _DATA):
    os.makedirs(_d, exist_ok=True)

sys.argv = ["test", _FRONTEND, "0", "0", _REPO]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

_STUBBED_MODULES = ("deskcfg", "workbench", "memory_store", "domains")
_OLD_MODULES = {name: sys.modules.get(name) for name in _STUBBED_MODULES}
for _name in _STUBBED_MODULES:
    sys.modules.pop(_name, None)

_workbench = types.ModuleType("workbench")
_workbench._kb_git_read = lambda: []
_workbench._wb_domains_read = lambda: []
_workbench._workspace_md_path = lambda ws: os.path.join(_REPO, "missing.md")
sys.modules["workbench"] = _workbench

_memory_store = types.ModuleType("memory_store")
_memory_store._provision_memory = lambda *a, **k: None
_memory_store._workspace_layout = lambda *a, **k: {}
sys.modules["memory_store"] = _memory_store

import domains  # noqa: E402


def _restore_modules():
    for name in _STUBBED_MODULES:
        old = _OLD_MODULES.get(name)
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


_restore_modules()


def _write_skill(root, dirname, name, desc):
    d = os.path.join(root, ".claude", "skills", dirname)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: {}\ndescription: {}\n---\n\nbody\n".format(name, desc))


def _expanduser(path):
    if path == "~":
        return _USER
    if path.startswith("~/"):
        return os.path.join(_USER, path[2:])
    return path


class SkillIndexDedupe(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(os.path.join(_REPO, ".claude"), ignore_errors=True)
        shutil.rmtree(os.path.join(_USER, ".claude"), ignore_errors=True)

    def test_dedupes_by_frontmatter_name_and_keeps_canonical_dir(self):
        _write_skill(_REPO, "older-copy", "same-skill", "older description")
        _write_skill(_REPO, "same-skill", "same-skill", "newer description")

        with mock.patch.object(domains, "REPO_ROOT", _REPO), \
             mock.patch.object(domains.os.path, "expanduser", _expanduser):
            index = domains._skill_index()

        self.assertEqual(index.count("`same-skill`"), 1)
        self.assertIn("newer description", index)
        self.assertNotIn("older description", index)

    def test_includes_user_global_skills_with_path_hint(self):
        _write_skill(_USER, "example-global", "example-global", "global rules")

        with mock.patch.object(domains, "REPO_ROOT", _REPO), \
             mock.patch.object(domains.os.path, "expanduser", _expanduser):
            index = domains._skill_index()

        self.assertIn("全局已安装技能", index)
        self.assertIn("~/.claude/skills/<name>/SKILL.md", index)
        self.assertIn("`example-global`", index)
        self.assertNotIn("writing rules", index)

    def test_repo_skill_shadows_user_global_skill(self):
        _write_skill(_REPO, "memory", "memory", "repo description")
        _write_skill(_USER, "memory", "memory", "global description")

        with mock.patch.object(domains, "REPO_ROOT", _REPO), \
             mock.patch.object(domains.os.path, "expanduser", _expanduser):
            index = domains._skill_index()

        self.assertEqual(index.count("`memory`"), 1)
        self.assertIn("repo description", index)
        self.assertNotIn("global description", index)

    def test_global_section_omits_repo_skills_and_their_shadowed_copies(self):
        _write_skill(_REPO, "memory", "memory", "repo description")
        _write_skill(_USER, "memory", "memory", "global description")
        _write_skill(_USER, "global-only", "global-only", "global only description")

        with mock.patch.object(domains, "REPO_ROOT", _REPO), \
             mock.patch.object(domains.os.path, "expanduser", _expanduser):
            index = domains._skill_index(("global",))

        self.assertNotIn("`memory`", index)
        self.assertIn("`global-only`", index)
        self.assertNotIn("仓内技能读", index)


def tearDownModule():
    shutil.rmtree(_BASE, ignore_errors=True)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_BASE, ignore_errors=True)
