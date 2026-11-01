"""实例配置:加载 ontology.yaml + rules.yaml,合并缺省。

curator 实例是一个 monorepo,认两种东西(都由 ontology 的 instance 块声明):

- KB 内容(git 内成品):多个子 KB(sub_kbs)。每个子 KB 内按角色层 global/domain
  分(role,管用途),加 graph 分区与 index 导航。全是提交进版本控制的成品。
- 消费方工作层(git 外):temp + local/{private,shared}。gitignored、跨子 KB 共享、不上远端。
  temp 免闸暂存;private(用户全局,敏感 containment)与 shared(本工作区流通)是成品。

成熟度轴(band)与角色轴正交:temp ─add─► [private|shared] ─清洗(选子 KB)─► 子 KB 角色层。
治理(band、内容纪律、graph schema)在 monorepo 根声明一份,所有子 KB 共用。
kernel 不含任何子 KB 名或领域名。
"""

from __future__ import annotations

import glob
import os
import re

import yaml

from .note import read_note
from .util import CuratorError

# 基线置信度三档:reported(无证据)< documented(有 source,引擎自动)< verified(用户签)。
BASE_CONFIDENCE = ["reported", "documented", "corroborated"]

KINDS = ["judgment", "fact"]

DEFAULT_ROLE_LAYERS = ["global", "domain"]
DEFAULT_WORK_LAYER = {
    "temp": {"dir": "temp", "gated": False},
    "private": {"dir": "local/private", "containment": True},
    "shared": {"dir": "local/shared"},
}
# 角色层 / 工作层目录里这些文件不是笔记。
NON_NOTE = {"README.md", "index.md", "INDEX.md"}

# 单笔记正文字数两档(质量纪律,非硬闸):软上限提示拆分,硬上限 WARN 应拆分/蒸馏。
DEFAULT_FILE_STRUCTURE = {"soft_chars": 2000, "warn_chars": 2500}

# work_layer dir 里的 topic 占位:实例可声明 "{topic}/s0" 这类按 topic 分区的 band 目录。
TOPIC_PLACEHOLDER = "{topic}"

# documented 的 source 须匹配其一(结构化可核引用),否则即便填了也只算 reported。
# 纯文本出处(如「文档标题 + 日期」)不算可核引用。实例可经 rules.source_patterns 覆盖。
DEFAULT_SOURCE_PATTERNS = [
    r"https?://",            # URL(含飞书 / lark / 文档链接)
    r"\S+\.\w{1,8}:\d+",     # file.ext:line(代码 / 配置定位)
    r"\b[0-9a-f]{7,40}\b",   # commit hash
]


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise CuratorError(f"{path}: 顶层不是映射")
    return data


def _parse_view(view_path: str):
    """读视图 manifest,返回 (work_root, gov_root, mounts)。

    manifest(YAML):
      governance: <ontology/rules 来源,相对 manifest 或绝对>
      work_layer: <工作层 + INDEX + .curator,相对 manifest 或绝对>
      mounts:
        - {sub_kb: common-kb, path: <子 KB 目录>, mode: ro|rw}
    挂载机制(clone/softlink/sync)归消费方;curator 只读 manifest + 在这组路径上操作。
    """
    if not os.path.exists(view_path):
        raise CuratorError(f"视图 manifest 不存在:{view_path}")
    m = _load_yaml(view_path)
    base = os.path.dirname(os.path.abspath(view_path))

    def resolve(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    gov = resolve(m.get("governance") or ".")
    work = resolve(m.get("work_layer") or ".")
    mounts = {}
    for mt in m.get("mounts", []) or []:
        sk = mt.get("sub_kb")
        if not sk or not mt.get("path"):
            raise CuratorError(f"挂载缺 sub_kb 或 path:{mt}")
        mode = mt.get("mode", "ro")
        if mode not in ("ro", "rw"):
            raise CuratorError(f"挂载 {sk} 的 mode={mode} 非法(ro/rw)")
        mounts[sk] = {"dir": resolve(mt["path"]), "mode": mode}
    return work, gov, mounts


class Instance:
    """一份 curator 实例(monorepo),绑定一个目录根。"""

    def __init__(self, root: str = None, view: str = None):
        # 两种入口:
        # - root:单仓视图。ontology/rules、工作层、INDEX、.curator 都在这个 root;子 KB 在 root/<id>。
        # - view:跨实例视图(manifest)。治理(ontology/rules)在 governance;工作层 + INDEX + .curator
        #   在 work_layer(消费方,= self.root);子 KB 各自挂载在声明的 path,带 ro/rw。
        if view:
            self.root, self.gov_root, self._mounts = _parse_view(view)
        else:
            self.root = os.path.abspath(root)
            self.gov_root = self.root
            self._mounts = None  # 单仓:子 KB 由 ontology.sub_kbs 派生,目录在 root/<id>,均 rw

        missing = [
            f
            for f in ("ontology.yaml", "rules.yaml")
            if not os.path.exists(os.path.join(self.gov_root, f))
        ]
        if missing:
            where = "治理目录" if view else "实例"
            raise CuratorError(
                f"{self.gov_root}: 不像 curator {where},缺 {', '.join(missing)}。"
                "先 curator init 或用 --root / --view 指定。"
            )
        self.ontology = _load_yaml(os.path.join(self.gov_root, "ontology.yaml"))
        self.rules = _load_yaml(os.path.join(self.gov_root, "rules.yaml"))
        self._inst = self.ontology.get("instance", {}) or {}

        # 单仓:配置相对路径不得越出 root。视图模式挂载点是显式路径,跳过此检查。
        if not view:
            escaped = [
                rel
                for rel in (self._managed_rels() + [self.index_file])
                if self._escapes_root(rel)
            ]
            if escaped:
                raise CuratorError(
                    f"{self.root}: 实例配置路径越出实例根:{', '.join(escaped)}"
                )

    # —— monorepo 结构声明 ——

    @property
    def sub_kbs(self) -> list[str]:
        """子 KB id 列表。单仓:ontology.sub_kbs(列表或 dict 均可);视图:挂载点。"""
        if self._mounts is not None:
            return list(self._mounts)
        raw = self._inst.get("sub_kbs", []) or []
        if isinstance(raw, dict):
            return list(raw)
        return list(raw)

    def gate_level(self, sub_kb: str) -> str:
        """子 KB 的闸等级:elevated(高危,随发行)或 normal(默认)。

        ontology.sub_kbs 为 dict 时读 gate_level 字段;为纯列表时全部 normal。
        """
        raw = self._inst.get("sub_kbs", []) or []
        if isinstance(raw, dict):
            spec = raw.get(sub_kb)
            if isinstance(spec, dict):
                return spec.get("gate_level", "normal")
        return "normal"

    def mount_mode(self, sub_kb: str) -> str:
        """子 KB 的读写模式:ro(只读 clone,只消费)/ rw(可写 clone,可贡献)。单仓均 rw。"""
        if self._mounts is not None:
            return self._mounts.get(sub_kb, {}).get("mode", "ro")
        return "rw"

    def assert_writable(self, sub_kb: str) -> None:
        if self.mount_mode(sub_kb) != "rw":
            raise CuratorError(f"子 KB {sub_kb} 是只读挂载(ro),不能写入;贡献请去源仓的可写 clone")

    @property
    def role_layers(self) -> list[str]:
        return list(self._inst.get("role_layers") or DEFAULT_ROLE_LAYERS)

    @property
    def work_layer(self) -> dict:
        wl = self._inst.get("work_layer")
        return dict(wl) if wl else dict(DEFAULT_WORK_LAYER)

    @property
    def work_products(self) -> list[str]:
        """工作层里的成品 band(过 add 闸的:private / shared),不含免闸 temp。"""
        return [b for b, spec in self.work_layer.items() if (spec or {}).get("gated") is not False]

    @property
    def indexed_bands(self) -> list[str]:
        """进索引与检索的 band。成品一律进;免闸 band 显式声明 indexed: true 才进。

        进索引与过写闸是两件事:免闸层(如蒸馏产出的 notes)由机器直接写、不过 add 闸,
        但仍应可被 show / search 命中,否则读闸指向的命令查不到它,那一层对 agent 不存在。
        流水与暂存(journal / temp)不声明 indexed,不进索引。"""
        out = list(self.work_products)
        out += [b for b, spec in self.work_layer.items()
                if b not in out and (spec or {}).get("indexed") is True]
        return out

    @property
    def sub_kb_graph(self) -> str:
        return self._inst.get("sub_kb_graph", "graph/entities.yaml")

    @property
    def graph_partitions(self) -> list:
        """graph 分区:[(命名空间前缀, 所在目录)]。

        多子 KB 的实例按子 KB 分区,id 命名空间化为 `<sub-kb>:<id>`。无子 KB 的实例
        (记忆工作层就是这种)在实例根放一份,不加前缀——否则骨架这一层对它整个不可用。"""
        if self.sub_kbs:
            return [(sk, self.sub_kb_dir(sk)) for sk in self.sub_kbs]
        return [(None, self.abspath("."))]

    @property
    def sub_kb_index(self) -> str:
        return self._inst.get("sub_kb_index", "index.md")

    @property
    def index_file(self) -> str:
        return self._inst.get("index_file", "INDEX.md")

    # —— 目录解析 ——

    def sub_kb_dir(self, sub_kb: str) -> str:
        if self._mounts is not None:
            return self._mounts[sub_kb]["dir"]
        return self.abspath(sub_kb)

    def role_dir(self, sub_kb: str, role: str) -> str:
        return os.path.join(self.sub_kb_dir(sub_kb), role)

    def _band_spec_dir(self, band: str) -> str:
        spec = self.work_layer.get(band, {}) or {}
        return spec.get("dir") or band

    def band_is_topical(self, band: str) -> bool:
        """band 目录声明含 {topic} 占位(按 topic 分区,如 "{topic}/s0"):
        写入必须给 topic,扫描经 work_scan_dirs 展开。占位与形状由实例 ontology 声明,引擎不认识具体名字。"""
        return TOPIC_PLACEHOLDER in self._band_spec_dir(band)

    def work_dir(self, band: str, topic: str = None) -> str:
        d = self._band_spec_dir(band)
        if TOPIC_PLACEHOLDER in d:
            if not topic:
                raise CuratorError(f"band {band} 按 topic 分区({d}),此操作需要 topic")
            t = topic.strip().strip("/")
            if not t or os.path.isabs(topic) or ".." in t.split("/"):
                raise CuratorError(f"topic 非法:{topic}(须相对子目录,不含 .. 或绝对路径)")
            d = d.replace(TOPIC_PLACEHOLDER, t)
        return self.abspath(d)

    def work_scan_dirs(self, band: str) -> list[str]:
        """band 的全部实际目录:普通 band 单目录;topical band 按占位 glob 展开(只返回已存在的)。"""
        d = self._band_spec_dir(band)
        if TOPIC_PLACEHOLDER not in d:
            p = self.abspath(d)
            return [p] if os.path.isdir(p) else []
        pattern = self.abspath(d.replace(TOPIC_PLACEHOLDER, "*"))
        return sorted(p for p in glob.glob(pattern) if os.path.isdir(p))

    @property
    def ungated_bands(self) -> list[str]:
        """免闸 band 列表(gated: false)。可能多个(如 s0/s1 分层),顺序按声明。"""
        return [b for b, spec in self.work_layer.items() if (spec or {}).get("gated") is False]

    @property
    def draft_bands(self) -> list[str]:
        """gc / temp-clean 的作用范围:免闸 band 里排除显式标 `draft: false` 的。

        `gated: false` 只是写路径属性(绕过交互式 add 闸,给自动化直写),不等于「草稿、
        可回收」。内核模板的 `temp` band 二者合一——唯一的免闸 band 就是草稿区,ungated
        即 draft 这条等价关系向来成立。v3 记忆布局(journal/notes)引入了第二个免闸 band,
        且两个都不是草稿:journal 是 append-only 流水(单文件,不是逐条可晋升的笔记),
        notes 是记忆引擎原子重写的当前项目模型(免闸只为绕过交互闸,内容不是草稿)。gc 与
        `temp clean` 若仍按 ungated_bands 扫,会把这两类内容当草稿吞掉——`temp clean --yes`
        当场删空;gc 满预算 triage 时 journal.md 因无 frontmatter 质量恒为 0,总是最先被淘汰
        (2026-08-21 实测:淘汰列表头几条就是各工作流的 journal.md)。

        默认(不声明 draft)= 沿用 draft,保持内核模板向后兼容;v3 的两个免闸 band 在实例
        ontology 里显式声明 `draft: false` 退出。"""
        return [b for b in self.ungated_bands if (self.work_layer.get(b, {}) or {}).get("draft") is not False]

    @property
    def temp_band(self) -> str:
        for b in self.ungated_bands:
            return b
        return "temp"

    @property
    def temp_dir(self) -> str:
        """兼容属性:免闸 band 的单目录。topical band 无单目录,调用方应改用 work_scan_dirs。"""
        return self.work_dir(self.temp_band)

    @property
    def gitignored_dirs(self) -> list[str]:
        """工作层全部目录(temp/private/shared)——git 外,gitignore + 准入闸挡。"""
        return [(spec or {}).get("dir") or b for b, spec in self.work_layer.items()]

    def is_containment(self, band: str) -> bool:
        return bool(self.work_layer.get(band, {}).get("containment"))

    @property
    def valid_tiers(self) -> list[str]:
        """frontmatter `tier` 允许值:角色层(git 内)+ 工作层成品 band + 进索引的免闸 band。"""
        return list(self.role_layers) + list(self.indexed_bands)

    # —— 位置判定(路径为准) ——

    def location_of(self, path: str) -> dict | None:
        """把一个文件路径分类成位置(按工作层 / 子 KB 挂载目录前缀,兼容单仓与视图):
        {kind: git_inside, sub_kb, role, git: inside} 或 {kind: work, band, git: outside}。
        """
        ap = os.path.abspath(path)
        for band in self.work_layer:
            if self.band_is_topical(band):
                spec = os.path.abspath(self.abspath(self._band_spec_dir(band)))
                pat = re.escape(spec).replace(re.escape(TOPIC_PLACEHOLDER), r"(.+?)")
                m = re.match(pat + r"(?:$|" + re.escape(os.sep) + r")", ap)
                if m:
                    return {"kind": "work", "band": band, "git": "outside", "topic": m.group(1)}
                continue
            d = os.path.abspath(self.work_dir(band))
            if ap == d or ap.startswith(d + os.sep):
                return {"kind": "work", "band": band, "git": "outside"}
        for sk in self.sub_kbs:
            skd = os.path.abspath(self.sub_kb_dir(sk))
            if ap == skd or ap.startswith(skd + os.sep):
                parts = os.path.relpath(ap, skd).split(os.sep)
                if parts and parts[0] in self.role_layers:
                    return {"kind": "git_inside", "sub_kb": sk, "role": parts[0], "git": "inside"}
                return None
        return None

    def is_git_inside_path(self, path: str) -> bool:
        loc = self.location_of(path)
        return bool(loc and loc.get("git") == "inside")

    def band_label_for(self, path: str) -> str:
        """供 INDEX / list 展示:git 内 · <子KB> / git 外 · <band>。"""
        loc = self.location_of(path)
        if not loc:
            return ""
        if loc["kind"] == "git_inside":
            return f"git 内 · {loc['sub_kb']}/{loc['role']}"
        return f"git 外 · {loc['band']}"

    # —— 本体声明(治理,monorepo 根一份) ——

    @property
    def confidence_levels(self) -> list[str]:
        c = self.ontology.get("confidence_levels")
        return list(c) if c else list(BASE_CONFIDENCE)

    @property
    def doc_types(self) -> list[str]:
        return list(self.ontology.get("doc_types", []))

    @property
    def entity_backed_doc_types(self) -> list[str]:
        """实体型 doc_type:这些 doc_type 的 fact 笔记必须在 graph 有对应实体
        (某实体 profile_doc 指向它)。空 / 不声明 = 不强制,向后兼容。"""
        return list(self.ontology.get("entity_backed_doc_types", []) or [])

    @property
    def freshness(self) -> dict:
        return dict(self.ontology.get("freshness", {}))

    def freshness_threshold(self, doc_type: str) -> dict:
        fr = self.freshness.get(doc_type)
        if fr:
            return fr
        return self.rules.get("freshness_default", {"fresh": 6, "aging": 12})

    @property
    def file_structure(self) -> dict:
        """单笔记正文字数阈值(实例 rules.file_structure 覆盖,缺省走引擎默认)。"""
        fs = dict(DEFAULT_FILE_STRUCTURE)
        fs.update(self.rules.get("file_structure", {}) or {})
        return fs

    @property
    def source_patterns(self) -> list:
        """documented 认可的 source 形态(正则,匹配其一即结构化可核)。实例 rules.source_patterns 覆盖。"""
        pats = self.rules.get("source_patterns")
        return list(pats) if pats else list(DEFAULT_SOURCE_PATTERNS)

    @property
    def entity_types(self) -> dict:
        return dict(self.ontology.get("entities", {}))

    @property
    def edge_types(self) -> dict:
        return dict(self.ontology.get("edges", {}))

    # —— 路径 ——

    def _managed_rels(self) -> list[str]:
        rels = list(self.sub_kbs)
        rels += [(spec or {}).get("dir") or b for b, spec in self.work_layer.items()]
        return rels

    def _escapes_root(self, rel: str) -> bool:
        target = os.path.abspath(os.path.join(self.root, rel))
        return not (target == self.root or target.startswith(self.root + os.sep))

    def abspath(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def resolve_doc(self, rel: str) -> str:
        """profile_doc(`<sub_kb>/<层内相对>` 或工作层相对)→ 绝对路径。

        子 KB 前缀按挂载目录解析(视图下子 KB 不在 root 下也成立);非子 KB 前缀
        回退到 work_root 相对。单仓下两者等价(sub_kb_dir = root/<sub_kb>)。
        """
        norm = (rel or "").replace("\\", "/")
        head, _, rest = norm.partition("/")
        if rest and head in self.sub_kbs:
            return os.path.join(self.sub_kb_dir(head), *rest.split("/"))
        return self.abspath(rel)

    def doc_key(self, path: str) -> str:
        """笔记路径 → 规范 profile_doc 键:git 内 `<sub_kb>/<层内相对>`,工作层 root 相对。

        与 resolve_doc 互逆。用于 entities_with_doc / retarget_doc 的稳定匹配,
        视图下也不退化成 `../..`。
        """
        ap = os.path.abspath(path)
        loc = self.location_of(path)
        if loc and loc.get("kind") == "git_inside":
            sk = loc["sub_kb"]
            rel = os.path.relpath(ap, os.path.abspath(self.sub_kb_dir(sk)))
            return f"{sk}/{rel.replace(os.sep, '/')}"
        return os.path.relpath(ap, os.path.abspath(self.root)).replace(os.sep, "/")

    # —— 笔记遍历(git 内子 KB 角色层 + 工作层成品;流水与暂存不遍历) ——
    # 默认只遍历成品:纪律类命令(lint / gc / authority / promote)只对成品负责。
    # 检索与索引(show / search / list / reindex / 查重)传 include_indexed=True,
    # 把声明了 indexed 的免闸 band 一并纳入——它们不过写闸,但要能被查到。

    def note_paths(self, include_indexed: bool = False) -> list[str]:
        out = []
        dirs = []
        for sk in self.sub_kbs:
            for role in self.role_layers:
                dirs.append(self.role_dir(sk, role))
        for band in (self.indexed_bands if include_indexed else self.work_products):
            dirs += self.work_scan_dirs(band)
        for d in dirs:
            for p in sorted(glob.glob(os.path.join(d, "**", "*.md"), recursive=True)):
                if os.path.basename(p) in NON_NOTE:
                    continue
                out.append(p)
        return out

    def band_is_gated(self, band: str) -> bool:
        return (self.work_layer.get(band, {}) or {}).get("gated") is not False

    def path_is_ungated(self, path: str) -> bool:
        loc = self.location_of(path)
        band = (loc or {}).get("band")
        return bool(band) and not self.band_is_gated(band)

    def iter_notes(self, include_indexed: bool = False):
        for p in self.note_paths(include_indexed):
            try:
                fm, body = read_note(p)
            except Exception:
                # 免闸层由机器直写、不过 add 校验,frontmatter 可能缺失或损坏:跳过,不中断遍历。
                # 成品层的损坏是纪律问题,照旧抛给调用方(lint 会报出来)。
                if self.path_is_ungated(p):
                    continue
                raise
            yield p, fm, body

    def layer_rank(self, path: str) -> int:
        """就近排序:越本地越小。工作层成品按其顺序(private<shared),git 内最远。"""
        loc = self.location_of(path)
        if not loc:
            return 999
        if loc["kind"] == "work":
            wp = self.work_products
            return wp.index(loc["band"]) if loc["band"] in wp else len(wp)
        return 100  # git 内(挂载的共享子 KB)比任何工作层都远

    def find_all_by_id(self, note_id: str) -> list:
        """同 id 在各层的全部命中(视图下可能工作层 + 挂载子 KB 都有)。含 indexed 免闸层。"""
        return [(p, fm, body) for p, fm, body in self.iter_notes(include_indexed=True)
                if fm.get("id") == note_id]

    def find_by_id(self, note_id: str):
        matches = self.find_all_by_id(note_id)
        if not matches:
            return None
        return min(matches, key=lambda m: self.layer_rank(m[0]))  # 就近覆盖:取最近一层
