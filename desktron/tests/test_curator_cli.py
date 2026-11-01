"""CLI 级回归测试(monorepo 实例模型)。直接调 cli.main(argv),对临时实例跑。

实例 = 一个 monorepo:子 KB example-kb(角色层 global/domain + graph)+ 消费方
工作层 temp/private/local。stdlib unittest:python3 -m unittest discover -s tests
"""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from curator import cli
from curator.note import read_note, write_note
from curator.util import today as kb_today


def run(root, *args):
    buf, errbuf = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(errbuf):
        rc = cli.main(["--root", root, *args])
    return rc, buf.getvalue() + errbuf.getvalue()


def run_view(view, *args):
    buf, errbuf = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(errbuf):
        rc = cli.main(["--view", view, *args])
    return rc, buf.getvalue() + errbuf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="curator-test-")
        self.root = os.path.join(self.tmp, "inst")
        rc, out = run(self.tmp, "init", self.root)
        self.assertEqual(rc, 0, out)

    def fm(self, rel):
        return read_note(os.path.join(self.root, rel))[0]

    def add_local(self, nid="w-1", topic="t", source="https://ex"):
        a = [self.root, "add", "--band", "shared", "--kind", "judgment",
             "--id", nid, "--summary", "s", "--topic", topic, "--body", "正文"]
        if source:
            a += ["--source", source]
        return run(*a)

    def add_kb(self, nid="g-1", role="domain", topic="t"):
        return run(
            self.root, "add", "--sub-kb", "example-kb", "--role", role,
            "--kind", "fact", "--doc-type", "landscape", "--id", nid,
            "--summary", "s", "--source", "https://ex", "--topic", topic, "--body", "正文",
        )


class TestInit(Base):
    def test_scaffold(self):
        for f in ("ontology.yaml", "rules.yaml", "CODEOWNERS", "INDEX.md",
                  "example-kb/index.md", "example-kb/graph/entities.yaml"):
            self.assertTrue(os.path.exists(os.path.join(self.root, f)), f)

    def test_missing_config_errors(self):
        os.remove(os.path.join(self.root, "rules.yaml"))
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 2)
        self.assertIn("rules.yaml", out)


class TestAddGates(Base):
    def test_work_no_source_reported(self):
        rc, out = self.add_local("a", source=None)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "reported")

    def test_work_source_documented(self):
        rc, out = self.add_local("a")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")

    def test_unstructured_source_stays_reported(self):
        # 纯文本出处(非 url/file:line/commit)不算可核引用 → reported,即便填了 source
        rc, out = self.add_local("a", source="某公司文档 2026")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "reported")

    def test_git_inside_add(self):
        rc, out = self.add_kb("g")
        self.assertEqual(rc, 0, out)
        fm = self.fm("example-kb/domain/t/g.md")
        self.assertEqual(fm["sub_kb"], "example-kb")
        self.assertEqual(fm["tier"], "domain")

    def test_band_must_be_product(self):
        rc, out = run(self.root, "add", "--band", "domain", "--kind", "judgment",
                      "--id", "x", "--summary", "s", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("工作层成品", out)

    def test_role_must_be_role_layer(self):
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--role", "shared",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "x",
                      "--summary", "s", "--source", "https://ex", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("角色层", out)

    def test_role_needs_sub_kb(self):
        rc, out = run(self.root, "add", "--role", "domain", "--kind", "fact",
                      "--doc-type", "landscape", "--id", "x", "--summary", "s",
                      "--source", "https://ex", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("--sub-kb", out)

    def test_band_with_sub_kb_rejected(self):
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--band", "shared",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "x",
                      "--summary", "s", "--source", "https://ex", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("不配 --sub-kb", out)

    def test_unknown_sub_kb_rejected(self):
        rc, out = run(self.root, "add", "--sub-kb", "nope", "--role", "domain",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "x",
                      "--summary", "s", "--source", "https://ex", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("不在实例", out)

    def test_blacklist_rejected(self):
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "x", "--summary", "s", "--topic", "t", "--body", "本文档主要介绍 y")
        self.assertEqual(rc, 2)
        self.assertIn("禁用表达", out)

    def test_dup_id_rejected(self):
        self.assertEqual(self.add_local("a")[0], 0)
        rc, out = self.add_local("a")
        self.assertEqual(rc, 2)
        self.assertIn("已存在", out)

    def test_single_source_is_documented(self):
        self.add_local("a")
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")

    def test_two_independent_sources_corroborate(self):
        self.add_local("b", source="https://a.example/doc,https://b.example/doc")
        self.assertEqual(self.fm("local/shared/t/b.md")["confidence"], "corroborated")

    def test_same_doc_twice_is_one_source(self):
        # 同一篇文档的两个锚点不算两个来源
        self.add_local("c", source="https://x.example/docx/TOKEN123#a,https://x.example/docx/TOKEN123#b")
        self.assertEqual(self.fm("local/shared/t/c.md")["confidence"], "documented")


class TestVerifyUpdate(Base):
    def test_verify_is_retired(self):
        self.add_local("a")
        rc, out = run(self.root, "verify", "--id", "a", "--by", "u")
        self.assertEqual(rc, 2)
        self.assertIn("已退役", out)
        self.assertIn("corroborated", out)
        # 档位不因这次调用变化
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")

    def test_body_edit_keeps_confidence(self):
        # 改正文不产生新来源,档位不动(旧 verified 才有 hash 失效那一套)
        self.add_local("a")
        rc, out = run(self.root, "update", "--id", "a", "--body", "完全不同的正文")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")

    def test_adding_second_source_upgrades(self):
        self.add_local("a")
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")
        rc, out = run(self.root, "update", "--id", "a",
                      "--source", "https://a.example/doc,https://b.example/doc")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "corroborated")

    def test_removing_second_source_downgrades(self):
        self.add_local("a", source="https://a.example/doc,https://b.example/doc")
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "corroborated")
        rc, out = run(self.root, "update", "--id", "a", "--source", "https://a.example/doc")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")

    def test_source_change_recomputes(self):
        self.add_local("a", source=None)  # reported
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "reported")
        rc, out = run(self.root, "update", "--id", "a", "--source", "https://ex")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/a.md")["confidence"], "documented")


class TestPromote(Base):
    def add_private(self, nid):
        return run(self.root, "add", "--band", "private", "--kind", "judgment",
                   "--id", nid, "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "正文")

    def test_private_to_local_desensitize(self):
        self.add_private("p")
        rc, out = run(self.root, "promote", "--id", "p", "--to-band", "shared")
        self.assertEqual(rc, 3)
        self.assertIn("脱敏", out)
        rc, out = run(self.root, "promote", "--id", "p", "--to-band", "shared", "--confirm")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "local/shared/t/p.md")))

    def test_local_to_git_inside_cleanse(self):
        self.add_local("w")
        rc, out = run(self.root, "promote", "--id", "w", "--to-role", "domain", "--to-sub-kb", "example-kb")
        self.assertEqual(rc, 3)
        self.assertIn("清洗", out)
        rc, out = run(self.root, "promote", "--id", "w", "--to-role", "domain",
                      "--to-sub-kb", "example-kb", "--confirm")
        self.assertEqual(rc, 0, out)
        fm = self.fm("example-kb/domain/t/w.md")
        self.assertEqual(fm["sub_kb"], "example-kb")
        self.assertEqual(fm["tier"], "domain")

    def test_git_to_work_rejected(self):
        self.add_kb("g")
        rc, out = run(self.root, "promote", "--id", "g", "--to-band", "shared")
        self.assertEqual(rc, 2)
        self.assertIn("回退", out)

    def test_private_cannot_cross_line(self):
        self.add_private("p")
        rc, out = run(self.root, "promote", "--id", "p", "--to-role", "domain", "--to-sub-kb", "example-kb")
        self.assertEqual(rc, 2)
        self.assertIn("不能一步跨 git 线", out)

    def test_git_inside_reclassify(self):
        self.add_kb("g", role="domain")
        rc, out = run(self.root, "promote", "--id", "g", "--to-role", "global", "--to-sub-kb", "example-kb")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "example-kb/global/t/g.md")))

    def test_need_sub_kb_for_git_inside(self):
        self.add_local("w")
        rc, out = run(self.root, "promote", "--id", "w", "--to-role", "domain")
        self.assertEqual(rc, 2)
        self.assertIn("--to-sub-kb", out)


class TestLintGraph(Base):
    def test_lint_clean(self):
        self.add_kb("g")
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)

    def test_path_tier_mismatch(self):
        self.add_kb("g")
        p = os.path.join(self.root, "example-kb/domain/t/g.md")
        fm, body = read_note(p)
        fm["tier"] = "global"  # 与路径(domain)不符
        write_note(p, fm, body)
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 1)
        self.assertIn("与路径不符", out)

    def test_graph_check_dangling(self):
        gf = os.path.join(self.root, "example-kb/graph/entities.yaml")
        with open(gf, "w", encoding="utf-8") as f:
            f.write(
                "entities:\n  - id: a\n    type: thing\n    profile_doc: example-kb/domain/none.md\n"
                "edges:\n  - from: a\n    type: depends_on\n    to: ghost\n"
            )
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 1)
        self.assertIn("悬空", out)

    def test_doctor_clean(self):
        rc, out = run(self.root, "doctor")
        self.assertEqual(rc, 0, out)

    def test_oversize_warns_not_blocks(self):
        self.add_kb("big")
        p = os.path.join(self.root, "example-kb/domain/t/big.md")
        fm, _ = read_note(p)
        write_note(p, fm, "字" * 2600)   # > warn_chars 2500
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)        # 不阻断
        self.assertIn("应拆分或蒸馏", out)
        self.assertIn("WARN", out)

    def test_soft_size_hints(self):
        self.add_kb("mid")
        p = os.path.join(self.root, "example-kb/domain/t/mid.md")
        fm, _ = read_note(p)
        write_note(p, fm, "字" * 2200)   # ∈ (2000, 2500]
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)
        self.assertIn("建议拆分", out)
        self.assertIn("提示", out)

    def test_size_threshold_configurable(self):
        rp = os.path.join(self.root, "rules.yaml")
        with open(rp, "a", encoding="utf-8") as f:
            f.write("\nfile_structure:\n  soft_chars: 50\n  warn_chars: 100\n")
        self.add_kb("small")
        p = os.path.join(self.root, "example-kb/domain/t/small.md")
        fm, _ = read_note(p)
        write_note(p, fm, "字" * 120)    # > 覆盖后的 warn 100
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)
        self.assertIn("应拆分或蒸馏", out)


class TestGraphWrite(Base):
    def ent(self, eid, etype, *sets):
        a = [self.root, "graph", "add-entity", "--id", eid, "--type", etype]
        for s in sets:
            a += ["--set", s]
        return run(*a)

    def test_add_entity_edge(self):
        self.assertEqual(self.ent("a", "thing", "status=current")[0], 0)
        self.assertEqual(self.ent("p", "actor")[0], 0)
        rc, out = run(self.root, "graph", "add-edge", "--from", "p", "--type", "links_to", "--to", "a")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "graph", "edges")
        self.assertIn("example-kb:p -links_to-> example-kb:a", out)

    def test_unknown_type_rejected(self):
        rc, out = run(self.root, "graph", "add-entity", "--id", "x", "--type", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("未在 ontology.entities", out)


class TestClusters(Base):
    """簇视图(实体之上的分组层):归属事实源、contains 生成、派生簇边、深度/环/一致校验。"""

    GF = "example-kb/graph/entities.yaml"

    def gf(self):
        return os.path.join(self.root, self.GF)

    def write_graph(self, text):
        with open(self.gf(), "w", encoding="utf-8") as f:
            f.write(text)

    def ent(self, eid, etype="actor", cluster=None):
        # 默认 actor:不要求 profile_doc,避免悬空 backref 干扰簇校验。
        a = [self.root, "graph", "add-entity", "--id", eid, "--type", etype]
        if cluster:
            a += ["--cluster", cluster]
        return run(*a)

    def test_add_cluster_and_membership(self):
        self.assertEqual(run(self.root, "graph", "add-cluster", "--id", "c1", "--desc", "组一")[0], 0)
        self.assertEqual(self.ent("a", cluster="c1")[0], 0)
        # contains 生成,且按 cluster 字段算出
        rc, out = run(self.root, "graph", "expand", "c1")
        self.assertEqual(rc, 0, out)
        self.assertIn("example-kb:a", out)

    def test_add_entity_unknown_cluster_rejected(self):
        rc, out = self.ent("a", cluster="ghost")
        self.assertEqual(rc, 2)
        self.assertIn("归属簇不存在", out)

    def test_map_shows_top_and_derived_edge(self):
        run(self.root, "graph", "add-cluster", "--id", "ca", "--desc", "A")
        run(self.root, "graph", "add-cluster", "--id", "cb", "--desc", "B")
        self.ent("p", etype="actor", cluster="ca")
        self.ent("t", etype="thing", cluster="cb")
        run(self.root, "graph", "add-edge", "--from", "p", "--type", "links_to", "--to", "t")
        rc, out = run(self.root, "graph", "map")
        self.assertEqual(rc, 0, out)
        self.assertIn("example-kb:ca", out)
        self.assertIn("example-kb:ca -links_to(1)-> example-kb:cb", out)

    def test_sync_clusters_rewrites_contains(self):
        run(self.root, "graph", "add-cluster", "--id", "c1")
        self.ent("a", cluster="c1")
        # 手动把 contains 改脏
        import yaml
        with open(self.gf(), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        for c in data["clusters"]:
            c["contains"] = ["stale"]
        with open(self.gf(), "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        # check 报不一致
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 1)
        self.assertIn("contains 与成员不一致", out)
        # sync 修复
        self.assertEqual(run(self.root, "graph", "sync-clusters")[0], 0)
        self.assertEqual(run(self.root, "graph", "check")[0], 0)

    def test_unclustered_entity_warns(self):
        self.ent("a")  # 不给 cluster
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 0)  # WARN 不阻断
        self.assertIn("未归入任何簇", out)

    def test_dangling_cluster_ref_errors(self):
        self.write_graph(
            "entities:\n- id: a\n  type: actor\n  cluster: ghost\nedges: []\n"
        )
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 1)
        self.assertIn("指向不存在的簇", out)

    def test_cluster_cycle_errors(self):
        self.write_graph(
            "entities: []\nclusters:\n"
            "- id: c1\n  cluster: c2\n- id: c2\n  cluster: c1\nedges: []\n"
        )
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 1)
        self.assertIn("成环", out)

    def test_cluster_depth_errors(self):
        # 实体之上四层簇:l1<l2<l3<l4 → l1 父链含四个簇,超限
        self.write_graph(
            "entities: []\nclusters:\n"
            "- id: l1\n  cluster: l2\n- id: l2\n  cluster: l3\n"
            "- id: l3\n  cluster: l4\n- id: l4\nedges: []\n"
        )
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 1)
        self.assertIn("父链超过", out)

    def test_remove_cluster_with_members_rejected(self):
        run(self.root, "graph", "add-cluster", "--id", "c1")
        self.ent("a", cluster="c1")
        rc, out = run(self.root, "graph", "remove-cluster", "--id", "c1")
        self.assertEqual(rc, 2)
        self.assertIn("仍有", out)


class TestCentrality(Base):
    """图中心性:PageRank 把汇点排前,孤立点单列,空图优雅退化。"""

    def ent(self, eid, etype="thing"):
        return run(self.root, "graph", "add-entity", "--id", eid, "--type", etype)

    def test_empty_graph(self):
        rc, out = run(self.root, "graph", "centrality")
        self.assertEqual(rc, 0)
        self.assertIn("图为空", out)

    def test_sink_ranks_top_and_isolated_listed(self):
        for e in ("t1", "t2", "t3"):
            self.ent(e)
        self.ent("lonely", "actor")  # 无边
        run(self.root, "graph", "add-edge", "--from", "t1", "--type", "depends_on", "--to", "t3")
        run(self.root, "graph", "add-edge", "--from", "t2", "--type", "depends_on", "--to", "t3")
        rc, out = run(self.root, "graph", "centrality")
        self.assertEqual(rc, 0, out)
        # t3 是汇点(in=2),PageRank 最高,排在 t1 之前
        self.assertLess(out.index("example-kb:t3"), out.index("example-kb:t1"))
        # lonely 无边,进孤立点
        self.assertIn("孤立点", out)
        self.assertIn("example-kb:lonely", out)

    def test_edge_type_filter(self):
        self.ent("a", "actor")
        self.ent("b", "thing")
        run(self.root, "graph", "add-edge", "--from", "a", "--type", "links_to", "--to", "b")
        # 限定到不存在的关系子集:depends_on 无边 → 全成孤立点
        rc, out = run(self.root, "graph", "centrality", "--edge", "depends_on")
        self.assertEqual(rc, 0, out)
        self.assertIn("0 边", out)


class TestGraphGate(Base):
    """实体型 doc_type:提交期闸硬挡未登记的实体型笔记,lint 棘轮 WARN 兜存量。"""

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)

    def setUp(self):
        super().setUp()
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        with open(os.path.join(self.root, "ontology.yaml"), "a", encoding="utf-8") as f:
            f.write("\nentity_backed_doc_types: [landscape]\n")

    def test_lint_warns_unregistered(self):
        self.add_kb("g")  # landscape fact,无实体
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0)  # WARN 不阻断
        self.assertIn("未登记 graph", out)

    def test_gate_blocks_unregistered(self):
        self.add_kb("g")
        self._git("add", "example-kb/domain/t/g.md")
        rc, out = run(self.root, "gate-staged")
        self.assertEqual(rc, 3)
        self.assertIn("无对应实体", out)

    def test_gate_passes_when_entity_added(self):
        self.add_kb("g")
        run(self.root, "graph", "add-entity", "--id", "g-ent", "--type", "thing",
            "--set", "profile_doc=example-kb/domain/t/g.md")
        self._git("add", "example-kb/domain/t/g.md", "example-kb/graph/entities.yaml")
        rc, out = run(self.root, "gate-staged")
        self.assertEqual(rc, 0, out)

    def test_off_by_default_backward_compatible(self):
        # 不声明 entity_backed_doc_types 的实例:闸与棘轮都不生效
        import re
        op = os.path.join(self.root, "ontology.yaml")
        with open(op, encoding="utf-8") as f:
            txt = f.read()
        txt = re.sub(r"\nentity_backed_doc_types: \[landscape\]\n", "\n", txt)
        with open(op, "w", encoding="utf-8") as f:
            f.write(txt)
        self.add_kb("g")
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0)
        self.assertNotIn("未登记 graph", out)


class TestRm(Base):
    def test_rm_needs_yes(self):
        self.add_local("a")
        rc, out = run(self.root, "rm", "--id", "a")
        self.assertEqual(rc, 3)
        self.assertTrue(os.path.exists(os.path.join(self.root, "local/shared/t/a.md")))

    def test_rm_yes(self):
        self.add_local("a")
        rc, out = run(self.root, "rm", "--id", "a", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "local/shared/t/a.md")))


class TestRetrieval(Base):
    def test_list_and_label(self):
        self.add_kb("g")
        rc, out = run(self.root, "list")
        self.assertEqual(rc, 0, out)
        self.assertIn("g", out)
        self.assertIn("git 内 · example-kb/domain", out)

    def test_show_reverse_links(self):
        self.add_kb("g")
        run(self.root, "add", "--band", "shared", "--kind", "fact", "--doc-type", "glossary",
            "--id", "f", "--summary", "s", "--links", "g", "--topic", "t", "--body", "b")
        rc, out = run(self.root, "show", "--id", "g")
        self.assertEqual(rc, 0, out)
        self.assertIn("被 links 引用", out)
        self.assertIn("f", out)

    def test_search(self):
        self.add_kb("g")
        rc, out = run(self.root, "search", "占位")  # body 含"占位"? 不一定;搜 summary "s"
        # 用确定能命中的:搜 id 无关,搜 summary
        rc, out = run(self.root, "search", "s", "--fields", "summary")
        self.assertEqual(rc, 0, out)
        self.assertIn("g", out)


class TestRobustness(Base):
    def test_add_dry_run_no_write(self):
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "d", "--summary", "s", "--topic", "t", "--body", "b", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "local/shared/t/d.md")))

    def test_add_dry_run_still_gates(self):
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "d", "--summary", "s", "--topic", "t", "--body", "经过分析", "--dry-run")
        self.assertEqual(rc, 2)
        self.assertIn("禁用表达", out)

    def test_path_escape_rejected(self):
        op = os.path.join(self.root, "ontology.yaml")
        with open(op, encoding="utf-8") as f:
            txt = f.read().replace("dir: temp", "dir: ../escape")
        with open(op, "w", encoding="utf-8") as f:
            f.write(txt)
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 2)
        self.assertIn("越出实例根", out)


class TestCuration(Base):
    def _enable_override(self):
        rp = os.path.join(self.root, "rules.yaml")
        with open(rp, encoding="utf-8") as f:
            r = f.read()
        r = r.replace(
            "# forbidden_overrides:\n#   private: [杀手锏, 革命性, 颠覆, 至关重要, 本质上, 最关键]",
            "forbidden_overrides:\n  private: [杀手锏, 革命性]",
        )
        with open(rp, "w", encoding="utf-8") as f:
            f.write(r)

    def test_default_blocks_narrative(self):
        self._enable_override()
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "x", "--summary", "s", "--topic", "t", "--body", "经过分析得出")
        self.assertEqual(rc, 2)
        self.assertIn("经过分析", out)

    def test_override_relaxes(self):
        self._enable_override()
        rc, out = run(self.root, "add", "--band", "private", "--kind", "judgment",
                      "--id", "x", "--summary", "s", "--topic", "t", "--body", "经过分析得出")
        self.assertEqual(rc, 0, out)

    def test_temp_list_clean(self):
        with open(os.path.join(self.root, "temp", "scratch.md"), "w", encoding="utf-8") as f:
            f.write("draft")
        rc, out = run(self.root, "temp", "list")
        self.assertIn("scratch.md", out)
        rc, out = run(self.root, "temp", "clean", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "temp", "scratch.md")))


class TestConsistency(Base):
    def seed(self):
        self.add_kb("note-x")  # example-kb/domain/t/note-x.md
        run(self.root, "graph", "add-entity", "--id", "ent-a", "--type", "thing",
            "--set", "profile_doc=example-kb/domain/t/note-x.md", "--set", "status=current")

    def test_promote_retargets_profile_doc(self):
        self.seed()
        self.assertEqual(run(self.root, "graph", "check")[0], 0)
        rc, out = run(self.root, "promote", "--id", "note-x", "--to-role", "global", "--to-sub-kb", "example-kb")
        self.assertEqual(rc, 0, out)
        self.assertEqual(run(self.root, "graph", "check")[0], 0)

    def test_rm_blocked_when_referenced(self):
        self.seed()
        rc, out = run(self.root, "rm", "--id", "note-x", "--yes")
        self.assertEqual(rc, 3)
        self.assertIn("profile_doc 引用", out)


class TestQualityScores(Base):
    def test_authority(self):
        self.add_kb("g")
        rc, out = run(self.root, "authority")
        self.assertEqual(rc, 0, out)
        self.assertIn("g", out)
        rc, out = run(self.root, "authority", "--id", "g")
        self.assertIn("authority=", out)

    def test_authority_never_touches_confidence(self):
        self.add_kb("g")
        run(self.root, "authority")
        self.assertEqual(self.fm("example-kb/domain/t/g.md")["confidence"], "documented")


class TestLifecycle(Base):
    def test_stamps_created(self):
        self.add_local("a")
        fm = self.fm("local/shared/t/a.md")
        self.assertIn("created", fm)
        self.assertEqual(fm.get("kind"), "judgment")
        self.assertNotIn("type", fm)   # type 可选、无默认:未给即不写

    def test_type_passthrough(self):
        run(self.root, "add", "--band", "shared", "--kind", "judgment", "--id", "a",
            "--summary", "s", "--type", "feedback", "--topic", "t", "--body", "正文")
        self.assertEqual(self.fm("local/shared/t/a.md").get("type"), "feedback")

    def test_review_every(self):
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment", "--id", "a",
                      "--summary", "s", "--review-every", "14d", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        fm = self.fm("local/shared/t/a.md")
        self.assertGreater(fm.get("next_review"), fm.get("created"))

    def test_trace(self):
        # update 不落事件,verify 已退役 —— 目前 trace 只有 created 一条
        self.add_local("a")
        rc, out = run(self.root, "trace", "--id", "a")
        self.assertEqual(rc, 0, out)
        self.assertIn("created", out)

    def test_review_due_next_review(self):
        self.add_local("a")
        p = os.path.join(self.root, "local/shared/t/a.md")
        fm, body = read_note(p)
        fm["next_review"] = "2000-01-01"
        write_note(p, fm, body)
        rc, out = run(self.root, "review-due")
        self.assertIn("next_review 到期", out)
        self.assertIn("a", out)


class TestGC(Base):
    def _temp(self, name, text):
        p = os.path.join(self.root, "temp", name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_pretenure_promotes_to_local(self):
        self._temp("good.md",
                   "---\nid: good\ntype: knowledge\nkind: fact\nconfidence: reported\n"
                   "doc_type: glossary\nstatus: current\nsource: t\n"
                   f"last_verified: '{kb_today()}'\nsummary: s\n---\n正文\n")
        self._temp("ref.md",
                   "---\nid: refd\ntype: knowledge\nkind: judgment\nconfidence: reported\n"
                   "summary: s\nlinks: [good]\n---\nx\n")
        rc, out = run(self.root, "gc", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "local/shared/good.md")))

    def test_evict_old_raw(self):
        import time
        p = self._temp("junk.md", "随手草稿\n")
        old = time.mktime((2020, 1, 1, 0, 0, 0, 0, 0, 0))
        os.utime(p, (old, old))
        rc, out = run(self.root, "gc", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(p))

    def test_preview_default(self):
        self._temp("d.md", "随手\n")
        rc, out = run(self.root, "gc")
        self.assertIn("预览", out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "temp", "d.md")))

    def test_dry_run_overrides_yes(self):
        self._temp("d.md", "随手\n")
        rc, out = run(self.root, "gc", "--dry-run", "--yes")
        self.assertIn("预览", out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "temp", "d.md")))

    def test_promote_preserves_topic_subdir(self):
        os.makedirs(os.path.join(self.root, "temp", "svc"), exist_ok=True)
        self._temp("svc/good.md",
                   "---\nid: good\ntype: knowledge\nkind: fact\nconfidence: reported\n"
                   "doc_type: glossary\nstatus: current\nsource: t\n"
                   f"last_verified: '{kb_today()}'\nsummary: s\n---\n正文\n")
        self._temp("ref.md",
                   "---\nid: refd\ntype: knowledge\nkind: judgment\nconfidence: reported\n"
                   "summary: s\nlinks: [good]\n---\nx\n")
        rc, out = run(self.root, "gc", "--yes")
        self.assertEqual(rc, 0, out)
        # 晋升落桶时保留 temp 内的 topic 子目录,不平铺
        self.assertTrue(os.path.exists(os.path.join(self.root, "local/shared/svc/good.md")))

    def test_doctor_rejects_containment_promote_to(self):
        with open(os.path.join(self.root, "ontology.yaml"), "a", encoding="utf-8") as f:
            f.write("\ngc:\n  promote_to: private\n")
        rc, out = run(self.root, "doctor")
        self.assertEqual(rc, 1)
        self.assertIn("promote_to", out)


class TestCommitGate(Base):
    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)

    def setUp(self):
        super().setUp()
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def test_block_work_layer(self):
        os.makedirs(os.path.join(self.root, "local/private"), exist_ok=True)
        with open(os.path.join(self.root, "local/private", "leak.md"), "w", encoding="utf-8") as f:
            f.write("x")
        self._git("add", "-f", "local/private/leak.md")
        rc, out = run(self.root, "gate-staged")
        self.assertEqual(rc, 3)
        self.assertIn("git 外", out)

    def test_pass_git_inside(self):
        self.add_kb("g")
        self._git("add", "example-kb/domain/t/g.md")
        rc, out = run(self.root, "gate-staged")
        self.assertEqual(rc, 0, out)

    def test_install_hooks(self):
        rc, out = run(self.root, "install-hooks")
        self.assertEqual(rc, 0, out)
        hook = os.path.join(self.root, ".git", "hooks", "pre-commit")
        self.assertTrue(os.path.exists(hook) and os.access(hook, os.X_OK))


class TestTopic(Base):
    def test_add_topic_subdir(self):
        rc, out = self.add_local("a", topic="payments")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "local/shared/payments/a.md")))

    def test_topic_escape_rejected(self):
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "a", "--summary", "s", "--topic", "../escape", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("topic 非法", out)

    def test_lint_warns_layer_root(self):
        run(self.root, "add", "--band", "shared", "--kind", "judgment",
            "--id", "a", "--summary", "s", "--source", "https://ex", "--body", "正文")  # 无 topic → 层根
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)
        self.assertIn("未归入子目录", out)


class TestTracks(Base):
    """变更驱动 freshness:tracks(note 追踪引擎自检 + 外部源插件接口)。"""

    def add_map(self, nid, tracks):
        return run(self.root, "add", "--band", "shared", "--kind", "judgment",
                   "--id", nid, "--summary", "s", "--source", "https://ex",
                   "--tracks", tracks, "--topic", "t", "--body", "正文")

    def test_add_tracks(self):
        rc, out = self.add_map("m", "note:x")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.fm("local/shared/t/m.md")["tracks"], ["note:x"])

    def test_note_track_stale(self):
        self.add_kb("src")              # 源,created 今天
        self.add_map("mapd", "note:src")
        p = os.path.join(self.root, "local/shared/t/mapd.md")
        fm, body = read_note(p)
        fm["last_review"] = "2000-01-01"   # 源(今天)在其后 → 前进
        write_note(p, fm, body)
        rc, out = run(self.root, "review-due")
        self.assertIn("变更驱动陈旧", out)
        self.assertIn("mapd", out)

    def test_note_track_fresh_skips_time(self):
        self.add_kb("src")
        self.add_map("mapf", "note:src")   # 同日,可判且不陈旧 → 跳过时间评估
        rc, out = run(self.root, "review-due")
        self.assertNotIn("mapf", out)

    def test_external_track_falls_to_time(self):
        # 外部 track 无插件 → 不可判 → 落回时间;fact 无 last_verified → 到期
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--role", "domain",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "ext",
                      "--summary", "s", "--source", "https://ex", "--tracks", "repo:x@abc",
                      "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "review-due")
        self.assertIn("ext", out)
        self.assertIn("无 last_verified", out)


class TestDistilledGate(Base):
    """蒸馏准入闸:可核 tracks 不兑现 → 拒收到 rejected 缓冲,不写成品(建议1)。"""

    def add_distilled(self, nid, tracks=None, force=False):
        a = [self.root, "add", "--sub-kb", "example-kb", "--role", "global",
             "--kind", "fact", "--doc-type", "landscape", "--id", nid,
             "--summary", "地图", "--distilled", "--topic", "t", "--body", "正文"]
        if tracks:
            a += ["--tracks", tracks]
        if force:
            a += ["--force"]
        return run(*a)

    def _written(self, nid):
        return os.path.exists(os.path.join(self.root, "example-kb/global/t", nid + ".md"))

    def _rejected_text(self):
        p = os.path.join(self.root, "temp/rejected.md")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_phantom_note_track_rejected(self):
        rc, out = self.add_distilled("m", tracks="note:ghost")
        self.assertEqual(rc, 4, out)
        self.assertIn("准入闸未过", out)
        self.assertFalse(self._written("m"))
        rt = self._rejected_text()
        self.assertIn("m", rt)
        self.assertIn("note:ghost", rt)

    def test_real_note_track_ok(self):
        self.add_kb("leaf")
        rc, out = self.add_distilled("m", tracks="note:leaf")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._written("m"))

    def test_force_bypasses_gate(self):
        rc, out = self.add_distilled("m", tracks="note:ghost", force=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._written("m"))

    def test_non_distilled_phantom_track_not_gated(self):
        # 非蒸馏笔记的 tracks 只是 freshness 元数据,不受准入闸约束
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "w", "--summary", "s", "--source", "https://ex",
                      "--tracks", "note:ghost", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)

    def test_external_track_no_plugin_not_gated(self):
        # 外部 ref 未配 check_plugin → 不可核 → 放行
        rc, out = self.add_distilled("m", tracks="repo:x@abc")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._written("m"))

    def test_no_tracks_not_gated(self):
        rc, out = self.add_distilled("m")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._written("m"))

    def test_dry_run_reports_without_buffer(self):
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--role", "global",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "m",
                      "--summary", "地图", "--distilled", "--tracks", "note:ghost",
                      "--topic", "t", "--body", "正文", "--dry-run")
        self.assertEqual(rc, 4, out)
        self.assertEqual(self._rejected_text(), "")


class TestCompact(Base):
    """老年代:蒸馏产物纪律(lint)+ compact-due 候选。"""

    def add_distilled(self, nid, tracks=None):
        a = [self.root, "add", "--sub-kb", "example-kb", "--role", "global",
             "--kind", "fact", "--doc-type", "landscape", "--id", nid,
             "--summary", "地图", "--distilled", "--topic", "t", "--body", "正文"]
        if tracks:
            a += ["--tracks", tracks]
        return run(*a)

    def test_distilled_no_tracks_warns(self):
        self.add_distilled("m")
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)
        self.assertIn("蒸馏产物应 track", out)

    def test_distilled_with_tracks_ok(self):
        self.add_kb("x")
        self.add_distilled("m", tracks="note:x")
        rc, out = run(self.root, "lint")
        self.assertNotIn("蒸馏产物应 track", out)

    def test_distilled_corroborated_errors(self):
        self.add_kb("x")
        self.add_distilled("m", tracks="note:x")
        run(self.root, "update", "--id", "m",
            "--source", "https://a.example/doc,https://b.example/doc")
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 1)
        self.assertIn("蒸馏产物绝不 corroborated", out)

    def test_compact_due_lists_weak(self):
        # 弱 git 内事实:无 source(reported)、无 last_verified(陈旧)、低 authority → 候选
        run(self.root, "add", "--sub-kb", "example-kb", "--role", "domain",
            "--kind", "fact", "--doc-type", "landscape", "--id", "weak",
            "--summary", "s", "--topic", "t", "--body", "正文")
        rc, out = run(self.root, "compact-due")
        self.assertEqual(rc, 0, out)
        self.assertIn("weak", out)

    def test_compact_due_skips_work_layer(self):
        # 工作层不是老年代,不进 compact-due
        run(self.root, "add", "--band", "shared", "--kind", "judgment",
            "--id", "w", "--summary", "s", "--topic", "t", "--body", "正文")
        rc, out = run(self.root, "compact-due")
        self.assertNotIn("w  ", out)


class TestUnionGraph(Base):
    """O:多子 KB 的 graph union + 命名空间 + 跨子 KB 边。"""

    def _add_sub_kb(self, name):
        op = os.path.join(self.root, "ontology.yaml")
        with open(op, encoding="utf-8") as f:
            o = f.read()
        o = o.replace("sub_kbs: [example-kb]", f"sub_kbs: [example-kb, {name}]")
        with open(op, "w", encoding="utf-8") as f:
            f.write(o)
        for r in ("global", "domain", "graph"):
            os.makedirs(os.path.join(self.root, name, r), exist_ok=True)

    def test_cross_kb_edge_and_namespace(self):
        self._add_sub_kb("other-kb")
        self.assertEqual(run(self.root, "graph", "add-entity", "--id", "svc",
                             "--type", "thing", "--sub-kb", "example-kb", "--set", "status=current")[0], 0)
        self.assertEqual(run(self.root, "graph", "add-entity", "--id", "tce",
                             "--type", "thing", "--sub-kb", "other-kb", "--set", "status=current")[0], 0)
        # 跨子 KB 边:example-kb:svc depends_on other-kb:tce
        rc, out = run(self.root, "graph", "add-edge", "--from", "svc", "--type", "depends_on",
                      "--to", "other-kb:tce", "--sub-kb", "example-kb")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 0, out)   # 跨 KB 端点在 union 里解析,不悬空
        rc, out = run(self.root, "graph", "edges")
        self.assertIn("example-kb:svc -depends_on-> other-kb:tce", out)
        rc, out = run(self.root, "graph", "neighbors", "example-kb:svc")
        self.assertIn("other-kb:tce", out)
        # 命名空间:边写进 example-kb 分区,本分区端点落裸值、跨分区端点保留命名空间
        with open(os.path.join(self.root, "example-kb/graph/entities.yaml"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("from: svc", txt)
        self.assertIn("to: other-kb:tce", txt)

    def test_multi_sub_kb_write_needs_target(self):
        self._add_sub_kb("other-kb")
        rc, out = run(self.root, "graph", "add-entity", "--id", "x", "--type", "thing")
        self.assertEqual(rc, 2)
        self.assertIn("需 --sub-kb", out)

    def test_save_only_writes_target_partition(self):
        # 写 example-kb 分区不该改写(更不该格式化掉注释)未涉及的 other-kb 分区
        self._add_sub_kb("other-kb")
        sentinel = os.path.join(self.root, "other-kb/graph/entities.yaml")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("# SENTINEL 手写注释\nentities: []\nedges: []\n")
        rc, out = run(self.root, "graph", "add-entity", "--id", "svc", "--type", "thing",
                      "--sub-kb", "example-kb", "--set", "status=current")
        self.assertEqual(rc, 0, out)
        with open(sentinel, encoding="utf-8") as f:
            self.assertIn("SENTINEL", f.read())


class TestView(Base):
    """P:视图 manifest——挂载子 KB(self.root 当 server)+ 消费方工作层 + ro/rw。"""

    def _view(self, mode="rw"):
        consumer = os.path.join(self.tmp, "consumer")
        os.makedirs(consumer, exist_ok=True)
        manifest = os.path.join(consumer, "view.yaml")
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(
                f"governance: {self.root}\nwork_layer: .\nmounts:\n"
                f"  - {{sub_kb: example-kb, path: {self.root}/example-kb, mode: {mode}}}\n"
            )
        return manifest

    def test_view_lists_mounted(self):
        self.add_kb("srvf")   # 写进 server 的 example-kb
        rc, out = run_view(self._view(), "list")
        self.assertEqual(rc, 0, out)
        self.assertIn("srvf", out)

    def test_view_add_to_work_layer(self):
        v = self._view()
        rc, out = run_view(v, "add", "--band", "shared", "--kind", "judgment",
                           "--id", "w", "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "consumer/local/shared/t/w.md")))

    def test_ro_mount_rejects_write(self):
        v = self._view(mode="ro")
        rc, out = run_view(v, "add", "--sub-kb", "example-kb", "--role", "domain",
                           "--kind", "fact", "--doc-type", "landscape", "--id", "x",
                           "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "b")
        self.assertEqual(rc, 2)
        self.assertIn("只读挂载", out)

    def test_rw_mount_allows_write(self):
        v = self._view(mode="rw")
        rc, out = run_view(v, "add", "--sub-kb", "example-kb", "--role", "domain",
                           "--kind", "fact", "--doc-type", "landscape", "--id", "y",
                           "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "example-kb/domain/t/y.md")))

    def test_nearest_override_and_conflict(self):
        # server 共享里 corroborated 的 dup(两个独立来源)
        self.add_kb("dup")
        run(self.root, "update", "--id", "dup",
            "--source", "https://a.example/doc,https://b.example/doc")
        v = self._view(mode="ro")
        # 工作层加同 id(跨层就近覆盖,允许)
        rc, out = run_view(v, "add", "--band", "shared", "--kind", "fact", "--doc-type", "landscape",
                           "--id", "dup", "--summary", "本地猜测", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        # show 就近取本地 + 冲突浮出
        rc, out = run_view(v, "show", "--id", "dup")
        self.assertIn("就近覆盖", out)
        self.assertIn("冲突", out)
        self.assertIn("本地猜测", out)   # 显示的是最近层(本地)
        # conflicts 报危险冲突,rc 非零
        rc, out = run_view(v, "conflicts")
        self.assertEqual(rc, 1)
        self.assertIn("dup", out)

    def test_graph_check_resolves_mounted_profile_doc(self):
        # server 的 example-kb:一条 git 内笔记 + 指向它的实体 profile_doc
        self.add_kb("svcnote", role="domain", topic="t")
        rc, _ = run(self.root, "graph", "add-entity", "--id", "ent1", "--type", "thing",
                    "--sub-kb", "example-kb", "--set", "status=current",
                    "--set", "profile_doc=example-kb/domain/t/svcnote.md")
        self.assertEqual(rc, 0)
        self.assertEqual(run(self.root, "graph", "check")[0], 0)  # 单仓通过
        # 视图下:profile_doc 按挂载目录解析,不该假悬空
        rc, out = run_view(self._view(mode="ro"), "graph", "check")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("悬空", out)


class TestSuggest(Base):
    """S:可执行建议汇总。"""

    def test_empty(self):
        rc, out = run(self.root, "suggest")
        self.assertEqual(rc, 0, out)
        self.assertIn("(无)", out)

    def test_promote_candidate(self):
        run(self.root, "add", "--band", "shared", "--kind", "judgment",
            "--id", "cand", "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "正文")
        rc, out = run(self.root, "suggest")
        self.assertEqual(rc, 0, out)
        self.assertIn("promote 候选", out)
        self.assertIn("cand", out)


class TestShowFreshness(Base):
    """show 读侧吐 freshness:与 confidence 正交的派生时效信号(锚 last_verified,非文件 mtime)。"""

    def test_show_emits_freshness(self):
        self.add_kb("g")
        rc, out = run(self.root, "show", "--id", "g")
        self.assertEqual(rc, 0, out)
        self.assertIn("新鲜度", out)

    def test_fresh_when_recent(self):
        self.add_kb("g")  # last_verified 缺,landscape fact;无 lv → 未知,非陈旧
        rc, out = run(self.root, "show", "--id", "g")
        self.assertIn("新鲜度", out)
        self.assertNotIn("陈旧", out)

    def test_stale_surfaces_in_show(self):
        self.add_kb("g")
        p = os.path.join(self.root, "example-kb/domain/t/g.md")
        fm, body = read_note(p)
        fm["last_verified"] = "2000-01-01"
        write_note(p, fm, body)
        rc, out = run(self.root, "show", "--id", "g")
        self.assertIn("陈旧", out)
        self.assertIn("过期", out)

    def test_change_driven_stale_in_show(self):
        self.add_kb("src")
        run(self.root, "add", "--band", "shared", "--kind", "judgment", "--id", "mapd",
            "--summary", "s", "--source", "https://ex", "--tracks", "note:src",
            "--topic", "t", "--body", "正文")
        p = os.path.join(self.root, "local/shared/t/mapd.md")
        fm, body = read_note(p)
        fm["last_review"] = "2000-01-01"
        write_note(p, fm, body)
        rc, out = run(self.root, "show", "--id", "mapd")
        self.assertIn("陈旧", out)
        self.assertIn("变更驱动", out)


class TestSubkbAdd(Base):
    """subkb add:改 ontology.sub_kbs + 落骨架(角色层 + graph + index),一条命令收尾。"""

    def test_add_scaffolds_and_declares(self):
        rc, out = run(self.root, "subkb", "add", "ops-kb")
        self.assertEqual(rc, 0, out)
        for f in ("ops-kb/global/.gitkeep", "ops-kb/domain/.gitkeep",
                  "ops-kb/graph/entities.yaml", "ops-kb/index.md"):
            self.assertTrue(os.path.exists(os.path.join(self.root, f)), f)
        # ontology 声明已更新,实例自检无 ERROR(声明的子 KB 目录齐备)
        rc, out = run(self.root, "doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("0 ERROR", out)

    def test_add_then_write_note(self):
        run(self.root, "subkb", "add", "ops-kb")
        rc, out = run(self.root, "add", "--sub-kb", "ops-kb", "--role", "domain",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "o1",
                      "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "ops-kb/domain/t/o1.md")))

    def test_ontology_keeps_existing_subkb(self):
        run(self.root, "subkb", "add", "ops-kb")
        with open(os.path.join(self.root, "ontology.yaml"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("example-kb", txt)  # 原有声明未被吞
        self.assertIn("ops-kb", txt)

    def test_dup_rejected(self):
        rc, out = run(self.root, "subkb", "add", "example-kb")
        self.assertEqual(rc, 2)
        self.assertIn("已声明", out)

    def test_invalid_name_rejected(self):
        rc, out = run(self.root, "subkb", "add", "Ops_KB")
        self.assertEqual(rc, 2)
        self.assertIn("非法", out)

    def test_dry_run_writes_nothing(self):
        rc, out = run(self.root, "subkb", "add", "ops-kb", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("dry-run", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "ops-kb")))
        self.assertNotIn("ops-kb", run(self.root, "list")[1] or "")


class TestForbiddenRegex(Base):
    """第 1 点:forbidden_in_body 支持 re: 前缀正则。"""

    def _add_rule(self, pattern):
        import yaml
        rp = os.path.join(self.root, "rules.yaml")
        with open(rp, encoding="utf-8") as f:
            rules = yaml.safe_load(f) or {}
        rules.setdefault("forbidden_in_body", []).append(pattern)
        with open(rp, "w", encoding="utf-8") as f:
            yaml.safe_dump(rules, f, allow_unicode=True, sort_keys=False)

    def test_literal_still_works(self):
        self._add_rule("SECRET_WORD")
        rc, out = self.add_local("a", source=None)
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "b", "--summary", "s", "--body", "含有SECRET_WORD哦")
        self.assertEqual(rc, 2)
        self.assertIn("禁用表达", out)

    def test_regex_pattern_matches(self):
        self._add_rule("re:ou_[a-z0-9]{10,}")
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "c", "--summary", "s", "--body", "用户 ou_1b48c93fa21d 的信息")
        self.assertEqual(rc, 2)
        self.assertIn("禁用表达", out)

    def test_regex_no_match_passes(self):
        self._add_rule("re:ou_[a-z0-9]{10,}")
        rc, out = run(self.root, "add", "--band", "shared", "--kind", "judgment",
                      "--id", "d", "--summary", "s", "--body", "正常正文无 PII")
        self.assertEqual(rc, 0, out)

    def test_regex_in_lint(self):
        self._add_rule("re:oc_[a-z0-9]{10,}")
        self.add_local("e")
        path = os.path.join(self.root, "local/shared/t/e.md")
        fm, _ = read_note(path)
        write_note(path, fm, "含有 oc_abcdef1234567 的聊天")
        rc, out = run(self.root, "lint")
        self.assertIn("禁用表达", out)


class TestGateLevel(Base):
    """第 2 点:sub_kbs dict 形式 + gate_level。"""

    def _set_elevated(self, sub_kb="example-kb"):
        import yaml
        op = os.path.join(self.root, "ontology.yaml")
        with open(op, encoding="utf-8") as f:
            onto = yaml.safe_load(f) or {}
        onto.setdefault("instance", {})["sub_kbs"] = {
            sub_kb: {"gate_level": "elevated"},
        }
        with open(op, "w", encoding="utf-8") as f:
            yaml.safe_dump(onto, f, allow_unicode=True, sort_keys=False)

    def test_list_form_is_normal(self):
        rc, out = self.add_kb("g-1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("elevated", out)

    def test_elevated_rejects_no_source(self):
        self._set_elevated()
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--role", "domain",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "e-1",
                      "--summary", "s", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 2)
        self.assertIn("elevated", out)
        self.assertIn("reported", out)

    def test_elevated_with_source_ok(self):
        self._set_elevated()
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--role", "domain",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "e-2",
                      "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "正文")
        self.assertEqual(rc, 0, out)
        self.assertIn("elevated gate", out)

    def test_elevated_tags_output(self):
        self._set_elevated()
        rc, out = run(self.root, "add", "--sub-kb", "example-kb", "--role", "domain",
                      "--kind", "fact", "--doc-type", "landscape", "--id", "e-3",
                      "--summary", "s", "--source", "https://ex", "--topic", "t", "--body", "ok")
        self.assertEqual(rc, 0, out)
        self.assertIn("[elevated gate]", out)

    def test_dict_sub_kbs_compat(self):
        self._set_elevated()
        rc, out = run(self.root, "list")
        self.assertEqual(rc, 0, out)

    def test_gate_level_api(self):
        from curator.config import Instance
        self._set_elevated()
        inst = Instance(self.root)
        self.assertEqual(inst.gate_level("example-kb"), "elevated")
        self.assertEqual(inst.gate_level("nonexistent"), "normal")


class TestReconcile(Base):
    """第 3 点:reconcile 命令。"""

    def test_clean_state(self):
        self.add_kb("r-1")
        rc, out = run(self.root, "reconcile")
        self.assertEqual(rc, 0, out)
        self.assertIn("一致", out)

    def test_orphan_event(self):
        self.add_kb("r-2")
        os.remove(os.path.join(self.root, "example-kb/domain/t/r-2.md"))
        rc, out = run(self.root, "reconcile")
        self.assertEqual(rc, 1)
        self.assertIn("orphan_event", out)
        self.assertIn("r-2", out)

    def test_untracked_file(self):
        write_note(
            os.path.join(self.root, "example-kb/domain/t/manual.md"),
            {"id": "manual", "tier": "domain", "kind": "fact", "confidence": "reported",
             "summary": "手放", "doc_type": "landscape", "status": "current", "sub_kb": "example-kb"},
            "手写的",
        )
        rc, out = run(self.root, "reconcile")
        self.assertEqual(rc, 1)
        self.assertIn("untracked_file", out)
        self.assertIn("manual", out)

    def test_fix_orphan(self):
        self.add_kb("r-3")
        os.remove(os.path.join(self.root, "example-kb/domain/t/r-3.md"))
        rc, out = run(self.root, "reconcile", "--fix")
        self.assertEqual(rc, 0, out)
        self.assertIn("reconciled_removed", out)
        rc2, out2 = run(self.root, "reconcile")
        self.assertEqual(rc2, 0, out2)
        self.assertIn("一致", out2)

    def test_fix_untracked(self):
        write_note(
            os.path.join(self.root, "example-kb/domain/t/manual2.md"),
            {"id": "manual2", "tier": "domain", "kind": "fact", "confidence": "reported",
             "summary": "手放", "doc_type": "landscape", "status": "current", "sub_kb": "example-kb"},
            "手写的",
        )
        rc, out = run(self.root, "reconcile", "--fix")
        self.assertEqual(rc, 0, out)
        self.assertIn("reconciled_created", out)
        rc2, out2 = run(self.root, "reconcile")
        self.assertEqual(rc2, 0, out2)


if __name__ == "__main__":
    unittest.main()


class TestTopicalLayout(unittest.TestCase):
    """{topic} 占位 band(workspace-first 布局)+ journal + caller 口径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="curator-topical-")
        self.root = os.path.join(self.tmp, "mem")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "ontology.yaml"), "w", encoding="utf-8") as f:
            f.write(
                "instance:\n  sub_kbs: []\n  work_layer:\n"
                "    s0: { dir: \"{topic}/s0\", gated: false }\n"
                "    s1: { dir: \"{topic}/s1\", gated: false }\n"
                "    shared: { dir: \"{topic}/shared\" }\n"
                "    sealed: { dir: \"_sealed\", containment: true }\n"
                "doc_types: [note]\n")
        open(os.path.join(self.root, "rules.yaml"), "w").close()

    def test_add_lands_workspace_first(self):
        rc, out = run(self.root, "add", "--band", "shared", "--topic", "incident",
                      "--kind", "judgment", "--id", "n-1", "--summary", "s", "--body", "b")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "incident", "shared", "n-1.md")))

    def test_topical_band_requires_topic(self):
        rc, out = run(self.root, "add", "--band", "shared",
                      "--kind", "judgment", "--id", "n-2", "--summary", "s", "--body", "b")
        self.assertNotEqual(rc, 0)
        self.assertIn("topic", out)

    def test_sealed_band_plain_dir(self):
        rc, out = run(self.root, "add", "--band", "sealed",
                      "--kind", "judgment", "--id", "n-3", "--summary", "s", "--body", "b")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "_sealed", "n-3.md")))

    def test_journal_append_show(self):
        rc, out = run(self.root, "journal", "append", "--topic", "incident", "--text", "第一笔流水")
        self.assertEqual(rc, 0, out)
        p = os.path.join(self.root, "incident", "s0", "journal.md")
        self.assertTrue(os.path.exists(p))
        rc, out = run(self.root, "journal", "append", "--topic", "incident", "--text", "第二笔")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "journal", "show", "--topic", "incident")
        self.assertEqual(rc, 0, out)
        self.assertIn("第一笔流水", out)
        self.assertIn("第二笔", out)

    def test_journal_rejects_gated_band(self):
        rc, out = run(self.root, "journal", "append", "--band", "shared", "--topic", "x", "--text", "y")
        self.assertNotEqual(rc, 0)

    def test_show_caller_hotness(self):
        from curator.config import Instance
        from curator.state import read_access
        run(self.root, "add", "--band", "shared", "--topic", "ws1",
            "--kind", "judgment", "--id", "hot-1", "--summary", "s", "--body", "b")
        rc, out = run(self.root, "show", "--id", "hot-1", "--caller", "engine-internal")
        self.assertEqual(rc, 0, out)
        inst = Instance(self.root)
        self.assertIsNone(read_access(inst, "hot-1"))   # 机器自读不计热度
        rc, out = run(self.root, "show", "--id", "hot-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(read_access(inst, "hot-1")["count"], 1)   # task 读计数

    def test_show_raw(self):
        run(self.root, "add", "--band", "shared", "--topic", "ws1",
            "--kind", "judgment", "--id", "raw-1", "--summary", "s", "--body", "正文内容")
        rc, out = run(self.root, "show", "--id", "raw-1", "--raw")
        self.assertEqual(rc, 0, out)
        self.assertIn("正文内容", out)
        self.assertNotIn("被 links 引用", out)

    def test_temp_list_scans_ungated_bands(self):
        os.makedirs(os.path.join(self.root, "ws1", "s0"), exist_ok=True)
        with open(os.path.join(self.root, "ws1", "s0", "d.md"), "w") as f:
            f.write("draft")
        rc, out = run(self.root, "temp", "list")
        self.assertEqual(rc, 0, out)
        self.assertIn(os.path.join("ws1", "s0", "d.md"), out)


class TestIndexedUngated(Base):
    """免闸 band 声明 indexed: true 后进索引与检索,但仍不受成品纪律(lint)约束。

    场景:记忆引擎直接写 notes 层,不过 add 闸,但产物要能被 show / search 命中——
    否则读闸指向的命令查不到它,那一层对 agent 不存在。
    """

    def setUp(self):
        super().setUp()
        import yaml
        op = os.path.join(self.root, "ontology.yaml")
        with open(op, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        doc["instance"]["work_layer"]["temp"]["indexed"] = True
        with open(op, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)

    def write_raw(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_show_and_search_hit_ungated(self):
        self.write_raw("temp/d1.md", "---\nid: d1\ntier: temp\nkind: judgment\n"
                                     "confidence: reported\nsummary: 机器直写的判断\n---\n\n正文内容\n")
        rc, out = run(self.root, "show", "--id", "d1")
        self.assertEqual(rc, 0, out)
        self.assertIn("正文内容", out)
        rc, out = run(self.root, "search", "机器直写")
        self.assertEqual(rc, 0, out)
        self.assertIn("d1", out)
        rc, out = run(self.root, "reindex")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.root, "INDEX.md"), encoding="utf-8") as f:
            self.assertIn("d1", f.read())

    def test_lint_still_skips_ungated(self):
        self.write_raw("temp/d2.md", "---\nid: d2\n---\n\n缺必填字段\n")
        rc, out = run(self.root, "lint")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("d2", out)

    def test_broken_frontmatter_skipped_not_fatal(self):
        self.write_raw("temp/bad.md", "---\nid: bad\nsummary: 冒号: 混在里面\n---\n\n坏的\n")
        self.write_raw("temp/ok.md", "---\nid: ok\ntier: temp\nkind: judgment\n"
                                     "confidence: reported\nsummary: 好的\n---\n\n正文\n")
        rc, out = run(self.root, "list")
        self.assertEqual(rc, 0, out)
        self.assertIn("ok", out)
        rc, out = run(self.root, "reindex")
        self.assertEqual(rc, 0, out)


class TestGraphNoSubKB(Base):
    """无子 KB 的实例(记忆工作层就是这种)在实例根放一份 graph,不加命名空间前缀。"""

    def setUp(self):
        super().setUp()
        import yaml
        op = os.path.join(self.root, "ontology.yaml")
        with open(op, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        doc["instance"]["sub_kbs"] = []
        doc["entities"] = {"person": {"fields": ["profile_doc"], "requires": []}}
        doc["edges"] = {"reports_to": {"from": "person", "to": "person"}}
        with open(op, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)

    def test_add_entity_edge_and_query(self):
        rc, out = run(self.root, "graph", "add-entity", "--id", "a", "--type", "person")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "graph", "add-entity", "--id", "b", "--type", "person")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "graph", "entities.yaml")))
        rc, out = run(self.root, "graph", "add-edge", "--from", "a", "--type", "reports_to", "--to", "b")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.root, "graph", "neighbors", "a")
        self.assertEqual(rc, 0, out)
        self.assertIn("b", out)
        self.assertNotIn(":", out.split("\n")[0])   # 无前缀:不出现 <sub-kb>:<id>
        rc, out = run(self.root, "graph", "check")
        self.assertEqual(rc, 0, out)
        self.assertIn("0 ERROR", out)

    def test_sub_kb_flag_rejected(self):
        rc, out = run(self.root, "graph", "add-entity", "--id", "c",
                      "--type", "person", "--sub-kb", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("无子 KB", out)

