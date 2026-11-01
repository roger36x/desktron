"""migrate_domain_id 核心迁移函数测试:目录 rename、domains.json id 与子区 ref 改写、wbState 引用改写。
跑:python3 -m unittest tests.test_migrate_domain_id
"""
import os, sys, json, shutil, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import migrate_domain_id as mig  # noqa: E402


class MigrateContainerId(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="migid-test-")
        self.repo = os.path.join(self.base, "repo_root")
        os.makedirs(os.path.join(self.repo, "domains", "c-test"))
        os.makedirs(os.path.join(self.repo, ".memory", "c-test"))
        os.makedirs(os.path.join(self.repo, ".workbench"))
        with open(os.path.join(self.repo, ".workbench", "domains.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": "c-test", "name": "AI Native", "kind": "container", "icon": "target",
                        "subareas": [{"id": "thinking", "name": "工作思考", "ref": "thinking", "note": ""}]},
                       {"id": "c-other", "name": "Other", "kind": "container", "icon": "target",
                        "subareas": [{"id": "proj-old", "name": "引用旧项目", "ref": "proj-old", "note": ""}]},
                       {"id": "proj-old", "name": "旧项目", "kind": "project", "dir": self.repo}], f)
        with open(os.path.join(self.repo, ".workbench", "state.json"), "w", encoding="utf-8") as f:
            json.dump({"sessions": {"sess1": "c-test", "sess2": "c-test:thinking"},
                       "hidden": ["c-test"], "containerOrder": ["c-test", "c-other"]}, f)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _doms(self):
        with open(os.path.join(self.repo, ".workbench", "domains.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_container_id_rename(self):
        res = mig.migrate("c-test", "c-ai-native", self.repo)
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "domains", "c-test")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "domains", "c-ai-native")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".memory", "c-ai-native")))
        self.assertEqual({d["id"] for d in self._doms()}, {"c-ai-native", "c-other", "proj-old"})
        with open(os.path.join(self.repo, ".workbench", "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["sessions"], {"sess1": "c-ai-native", "sess2": "c-ai-native:thinking"})
        self.assertEqual(st["hidden"], ["c-ai-native"])
        self.assertEqual(st["containerOrder"], ["c-ai-native", "c-other"])

    def test_non_container_id_rewrites_refs_in_other_containers(self):
        res = mig.migrate("proj-old", "proj-new", self.repo)
        self.assertTrue(res["ok"])
        other = next(d for d in self._doms() if d["id"] == "c-other")
        self.assertEqual(other["subareas"][0]["ref"], "proj-new")
        self.assertEqual(other["subareas"][0]["id"], "proj-new")   # id 列与 ref 相同时同步改

    def test_unknown_old_id_returns_warning(self):
        res = mig.migrate("does-not-exist", "whatever", self.repo)
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
