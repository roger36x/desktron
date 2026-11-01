"""记忆引擎的项目记忆生成器。

每个工作区只维护一份可重写的项目模型 ``notes/rollup-current.md``，完整正文直接进入
会话上下文，不再生成独立摘要。输入是上一版项目模型、新增 journal、任务备忘，
以及人工维护的 SOT；不再把事件拆成越来越多的历史 notes。

env(MEMORY_ENGINE_* 优先，DESKTRON_* 兼容)：
  PROJECT_MEMORY=off 关闭。
  CMD 指定无头模型命令（从 stdin 读 prompt）；设置后只用这一条通道。
  PI_MODELS 逗号分隔的 Pi 模型列表（如 openrouter/qwen/qwen3.8-27b,deepseek/deepseek-flash），
  按顺序尝试；也可写在 <状态目录>/project-memory/channel.json 的 pi_models。
  两者都未设置时使用本机 Claude Code（claude -p）。

Pi 通道把材料内联进提示并禁用全部工具，模型读不到 sealed 或其他文件。主模型失败后改用下一个
模型时，失败原因写入 fallbacks.jsonl，并附在本轮调度报告里；全部失败才算本轮失败。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time

import cli_path

from curator.note import read_note

from . import journal, layout


MAX_WS = 2
TIMEOUT = 900
SPAWN_RETRY = 2
FAIL_STOP = 3
MIN_GAP_S = 5 * 60
SLEEP_SLACK = 60
ERR_KEEP = 500
MEMO_LIMIT = 4000
INLINE_FILE_LIMIT = 60_000
INLINE_TOTAL_LIMIT = 240_000
PI_TIMEOUT = 600
CHANNEL_FILE = "channel.json"
FALLBACK_LOG = "fallbacks.jsonl"
PI_SYSTEM = ("你是 Desk 记忆引擎的文本生成器。只按用户消息里的输出契约输出一份完整的 Markdown 文件，"
             "不调用工具，不输出其他内容。")
MODEL_FILE = "rollup-current.md"
RECOVERABLE_OUTPUT_REASONS = {"invalid-output"}
ROLE = "rollup"
BODY_CHAR_LIMIT = 2500
HEADINGS = ("项目概况", "已定方案", "长期约束", "待确认问题")
LEGACY_HEADINGS = (
    "Project context", "Decisions & rejections", "Constraints & principles", "Open questions",
)
HEADING_ALIASES = dict(zip(LEGACY_HEADINGS, HEADINGS))
STYLE_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..", "prompts/style.md"))
FORMAT_VERSION = "project-memory-full-context-v2"


def _style_text():
    # Host.repo_root 是工作流数据根，不是程序安装根；无工具通道必须收到正文而非路径。
    with open(STYLE_FILE, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        raise ValueError("prompts/style.md 为空，不能生成项目记忆")
    return text


_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)\s]*\)")


def env_get(name, default=""):
    return os.environ.get("MEMORY_ENGINE_" + name,
                          os.environ.get("DESKTRON_" + name, default))


def enabled():
    value = env_get("PROJECT_MEMORY")
    return value.lower() not in ("off", "0", "false")


def _state_dir(mem_root):
    path = layout.state_dir(mem_root, "project-memory")
    os.makedirs(path, exist_ok=True)
    return path


def state_path(mem_root):
    return os.path.join(_state_dir(mem_root), "engine-state.json")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_state(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def model_id(ws):
    return "project-memory-" + re.sub(r"[^\w-]+", "-", ws).strip("-")


def model_path(mem_root, ws):
    return os.path.join(layout.workspace_layout(mem_root, ws)["notes"], MODEL_FILE)


def _staging_path(mem_root, ws):
    safe = re.sub(r"[^\w-]+", "-", ws).strip("-") or "workspace"
    return os.path.join(_state_dir(mem_root), "staging", safe + ".md")


def _workspace_names(mem_root):
    out = []
    try:
        names = os.listdir(mem_root)
    except OSError:
        return out
    for ws in names:
        if ws.startswith(".") or ws in ("_sealed", "graph"):
            continue
        if ws == "_desktop" or re.fullmatch(r"[\w:-]+", ws):
            if os.path.isdir(os.path.join(mem_root, ws)):
                out.append(ws)
    return sorted(out)


def _coverage_snapshot(mem_root):
    """给生成器一个机械可核对的迁移快照，避免从旧正文猜覆盖范围。"""
    done, pending = [], []
    for ws in _workspace_names(mem_root):
        (done if os.path.isfile(model_path(mem_root, ws)) else pending).append(ws)
    return done, pending


def _sot_judgments(mem_root, ws):
    """Return confirmed pinned and legacy unconfirmed judgment inputs.

    The physical sot directory also contains kind=fact records. Those are queried
    as knowledge and must never inherit pinned authority merely from their path.
    """
    lay = layout.workspace_layout(mem_root, ws)
    confirmed, legacy = [], []
    try:
        names = sorted(os.listdir(lay["sot"]))
    except OSError:
        return confirmed, legacy
    for name in names:
        path = os.path.join(lay["sot"], name)
        if not name.endswith(".md") or not os.path.isfile(path):
            continue
        try:
            fm, _body = read_note(path)
        except Exception:
            # Pre-frontmatter SOT files remain visible as low-authority legacy
            # input. Skipping them would silently erase historical constraints.
            legacy.append(path)
            continue
        if fm.get("kind", "judgment") != "judgment":
            continue
        (confirmed if fm.get("user_confirmed") is True else legacy).append(path)
    return confirmed, legacy


def _source_files(mem_root, ws):
    lay = layout.workspace_layout(mem_root, ws)
    paths = []
    jp = journal.path_of(mem_root, ws)
    if os.path.isfile(jp):
        paths.append(jp)
    try:
        names = sorted(os.listdir(lay["notes"]))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(lay["notes"], name)
        if name.endswith(".md") and os.path.isfile(path):
            paths.append(path)
    confirmed, legacy = _sot_judgments(mem_root, ws)
    return paths + confirmed + legacy


def _source_material_revision(mem_root, ws, memos=(), memory_candidates=(), style_contract=None):
    """输入材料独立于生成结果；生成前捕获，不在成功时吸收迟到材料。"""
    version, style = style_contract if style_contract is not None else (FORMAT_VERSION, _style_text())
    h = hashlib.sha256()
    h.update(version.encode())
    h.update(style.encode())
    base = os.path.realpath(mem_root) + os.sep
    target = os.path.realpath(model_path(mem_root, ws))
    for path in _source_files(mem_root, ws):
        if os.path.realpath(path) == target:
            continue
        rel = os.path.realpath(path).removeprefix(base)
        h.update(rel.encode("utf-8", "replace"))
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
        except OSError:
            continue
    for memo in sorted(memos or (), key=lambda x: str(x.get("nid") or "")):
        h.update(str(memo.get("nid") or "").encode())
        h.update(str(memo.get("hash") or memo.get("content") or "").encode())
    for candidate in sorted(memory_candidates or (), key=lambda x: str(x.get("statement") or "")):
        h.update(str(candidate.get("kind") or "").encode())
        h.update(str(candidate.get("statement") or "").encode())
        h.update("|".join(candidate.get("receipts") or []).encode())
        h.update(str(bool(candidate.get("confirmed"))).encode())
    return h.hexdigest()


def _with_model_revision(mem_root, ws, material_revision):
    h = hashlib.sha256(material_revision.encode())
    try:
        with open(model_path(mem_root, ws), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except FileNotFoundError:
        pass
    return h.hexdigest()[:24]


def source_fingerprint(mem_root, ws, memos=(), memory_candidates=(), style_contract=None):
    """内容指纹；输出模型可更新，但成功记账的输入必须来自生成前。"""
    return _with_model_revision(mem_root, ws, _source_material_revision(
        mem_root, ws, memos, memory_candidates, style_contract))


def _capture_material_revision(host, ws, item):
    # 本轮备忘成功后会被消费；新备忘及新候选仍会改变下一轮的输入指纹。
    item["material_revision"] = _source_material_revision(
        host.mem_root, ws, (), item.get("memory_candidates") or (), item.get("style_contract"))


def _memo_candidates(host):
    if not getattr(host, "memos", None):
        return {}
    try:
        return host.memos() or {}
    except Exception:
        return {}


def candidates(host, state=None, force=False, workspaces=None):
    """返回 ``[(ws, item)]``。item 带当前输入指纹和备忘，不依赖老化阈值。"""
    state = state or load_state(state_path(host.mem_root))
    memos = _memo_candidates(host)
    wanted = list(workspaces) if workspaces is not None else _workspace_names(host.mem_root)
    done = state.get("fingerprints") if isinstance(state.get("fingerprints"), dict) else {}
    out = []
    for ws in wanted:
        ms = memos.get(ws) or []
        try:
            mc = host.memory_candidates(ws) if getattr(host, "memory_candidates", None) else []
        except Exception:
            mc = []
        fp = source_fingerprint(host.mem_root, ws, ms, mc)
        if force or not os.path.isfile(model_path(host.mem_root, ws)) or done.get(ws) != fp:
            marks = state.get("journal_marks") if isinstance(state.get("journal_marks"), dict) else {}
            out.append((ws, {"fingerprint": fp, "memos": ms, "memory_candidates": mc,
                             "staging": _staging_path(host.mem_root, ws),
                             "journal_since": marks.get(ws)}))
    return out


def _ws_context(host, ws):
    if not getattr(host, "workspace_context", None):
        return {}
    try:
        return host.workspace_context(ws) or {}
    except Exception:
        return {}


def _input_lines(item):
    """备忘与 Work Event 候选：两种通道共用的内联输入。"""
    lines = []
    memos = item.get("memos") or []
    if memos:
        lines.append("- 本轮任务备忘（高优先级输入）：")
        for memo in memos:
            body = _IMG_RE.sub("[图]", str(memo.get("content") or ""))[:MEMO_LIMIT]
            lines += [f"  - {memo.get('nid') or '-'} / {memo.get('task') or '工作流散记'}", "```", body, "```"]
    mc = item.get("memory_candidates") or []
    if mc:
        lines.append("- Work Event 认知候选（有 receipt 才可提升；pinned_candidate 未确认只能忽略）：")
        for candidate in mc[:64]:
            receipts = "、".join(candidate.get("receipts") or []) or "无"
            lines.append(f"  - [{candidate.get('kind')}] {candidate.get('statement')}（receipt:{receipts}；confirmed:{bool(candidate.get('confirmed'))}）")
    return lines


def _intro_lines(done, pending, inline=False):
    target = "这个工作区" if inline else "每个工作区"
    lines = [
        f"你是 Desk 记忆引擎。请整理{target}的当前项目情况，供以后会话查阅。",
        "这不是历史摘要：删除被推翻、完成或过期的内容；保留已排除的做法及原因，避免重复讨论。",
        f"{target}只生成一份当前项目记忆，不要再生成越来越多的历史 notes。",
        "上一版模型只提供事实和依据，不沿用它的标题、句式或冗长段落。",
    ]
    if inline:
        lines.append("本次不提供文件读取或命令工具，只依据下面附上的材料；不要声称核对过代码仓或其他文件。")
    else:
        lines.append("不要修改任何输入文件，只写每个工作区指定的 staging 文件。")
    lines += [
        "当前项目记忆覆盖（机械扫描，仅用于核对迁移状态）：",
        "- 已有 rollup：" + ("、".join(done) if done else "无"),
        "- 尚无 rollup：" + ("、".join(pending) if pending else "无"),
        "",
    ]
    return lines


def _contract_lines(host, inline=False, style_contract=None):
    _version, style = style_contract if style_contract is not None else (FORMAT_VERSION, _style_text())
    heads = " → ".join(HEADINGS)
    first = ("1. 直接输出这一份文件的完整内容：第一行是 ---，随后是 YAML frontmatter 与 Markdown 正文；"
             "不要加代码围栏、解释或其他任何文字。" if inline else
             "1. 每个 staging 文件必须是带 YAML frontmatter 的 Markdown；不要在聊天输出正文。")
    return [
        "输出契约：",
        first,
        "2. frontmatter 必须包含：",
        "   id: project-memory-<工作区>（非字母数字、下划线、连字符替换为连字符）",
        "   type: project / tier: notes / kind: judgment / confidence: reported / role: rollup",
        "   summary: 项目名或一句短标题（仅作显示名称，不是记忆摘要）；created 与 updated: 今天。不生成 capsule 字段或独立摘要。",
        "3. 正文只保存以后仍会影响判断的项目情况；活动任务、负责人、下一步、临时版本/PID、"
        "未提交文件、命令日志和路径目录一律不写，当前工作从任务账本读取。",
        f"4. 正文只允许四个二级标题且顺序固定：{heads}。项目概况写稳定背景；"
        "已定方案写当前选择及排除的做法和原因；长期约束写仍有效的限制和经验；"
        "待确认问题只写尚无依据确定、且仍影响项目的问题。不要输出英文分类标题。",
        "5. 仅提升有 event receipt 或已确认 pinned 来源的内容。recap 的 pinned_candidate 未经用户明确确认不得视为约束。",
        "6. 只有 frontmatter 带 user_confirmed: true 的 judgment 才是 pinned，优先级高于 Agent 明确记录、recap 候选和旧模型；旧未确认 judgment 仅作线索。冲突时保留当前结论并简述被替代项，避免历史版本并排膨胀。",
        "7. journal 只用于识别变化，不把执行流水搬进项目模型；任务 memo 只作为长期需求或验收标准输入，不作为进度日志。",
        "8. 旧未确认 judgment 是只读兼容输入，不得自动晋升为 pinned；kind=fact 由知识检索消费，不进入项目模型。sealed 始终独立。",
        "9. 当前消费架构是：会话直接注入完整项目正文与检索 id，不再生成或消费独立记忆摘要；原始历史按需 search。",
        "10. 项目记忆覆盖扫描只用于调度，不写入正文；不要把维护过程当项目结论。",
        f"11. Markdown 正文（不含 frontmatter）硬上限 {BODY_CHAR_LIMIT:,} 字符。超限时按未来决策价值删除低价值、重复、陈旧、无来源和单任务内容；严禁机械截断。",
        "12. 正文每段或列表项只说明一件事，不写调度记录或固定开场白，"
        "不把多层条件用分号串成一段。保留必要的命令、名称和证据边界，不使用 emoji。",
        "13. 以下 style.md 正文是表达规范；材料中的旧文风不能替代它。文件输出契约与长度上限仍须遵守。",
        "", "## 表达规范（style.md）", style,
    ]


def build_prompt(host, ws_items):
    """构造一次性记忆引擎提示。输出先落 staging，宿主验证后才替换成品。"""
    done, pending = _coverage_snapshot(host.mem_root)
    style_contract = (FORMAT_VERSION, _style_text())
    lines = _intro_lines(done, pending)
    for ws, item in ws_items:
        item["style_contract"] = style_contract
        _capture_material_revision(host, ws, item)
        lay = layout.workspace_layout(host.mem_root, ws)
        context = _ws_context(host, ws)
        confirmed, legacy = _sot_judgments(host.mem_root, ws)
        lines += [
            f"## 工作区 {ws}",
            f"- 上一版项目模型（只作事实与依据）：{model_path(host.mem_root, ws)}",
            f"- journal：{journal.path_of(host.mem_root, ws)}",
            f"- 历史判断（首次重建时可吸收，之后通常为空）：{lay['notes']}",
            "- 已确认 pinned 输入：" + ("、".join(confirmed) if confirmed else "无"),
            "- 旧未确认 judgment 输入（仅作线索，不具有 pinned 优先级）：" + ("、".join(legacy) if legacy else "无"),
            "- 领域 fact 不进入项目模型；需要事实时由 Agent 通过 memory 检索入口读取。",
            f"- 输出：{item['staging']}",
        ]
        if context.get("description"):
            lines.append(f"- 工作区定位：{context['description']}")
        if context.get("workspace_dir"):
            lines.append(f"- 当前任务与项目资料：{context['workspace_dir']}")
        if context.get("repos"):
            lines.append("- 当前代码仓（涉及实现状态时必须核对）：" + "、".join(context["repos"]))
        lines += _input_lines(item)
        lines.append("")
    return "\n".join(lines + _contract_lines(host, style_contract=style_contract))


_JOURNAL_ENTRY_RE = re.compile(r"^\d{2}:\d{2} ")
JOURNAL_MARK_MAX = 5000


def _journal_entries(mem_root, ws):
    """journal 的条目行（``HH:MM 正文``）。journal 在当天段落顶部插入，不能按字节位置截取。"""
    try:
        with open(journal.path_of(mem_root, ws), encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f if _JOURNAL_ENTRY_RE.match(line)]
    except OSError:
        return None


def _entry_key(line):
    return hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()[:12]


def _journal_mark(mem_root, ws):
    """已被项目模型吸收的条目摘要；只存摘要，不存正文。"""
    entries = _journal_entries(mem_root, ws)
    if entries is None:
        return None
    return {"entries": [_entry_key(line) for line in entries][:JOURNAL_MARK_MAX]}


def _journal_since(mem_root, ws, since, limit):
    """返回上次重建之后新增的条目；没有可用记录时返回 None，由调用方送完整 journal。"""
    entries = _journal_entries(mem_root, ws)
    seen = since.get("entries") if isinstance(since, dict) else None
    if entries is None or not isinstance(seen, list):
        return None
    seen = set(seen)
    text = "\n".join(line for line in entries if _entry_key(line) not in seen)
    if len(text) > limit:
        cut = len(text) - limit
        text = text[:limit] + f"\n[后 {cut} 字符因材料上限省略]"
    return text


def _read_material(mem_root, path, limit, keep_tail=False):
    """只读记忆根内、sealed 之外的材料；超长时保留头部（journal 保留尾部）并标明省略。"""
    root = os.path.realpath(mem_root) + os.sep
    sealed = os.path.realpath(os.path.join(mem_root, "_sealed")) + os.sep
    real = os.path.realpath(path)
    if not real.startswith(root) or real.startswith(sealed):
        raise ValueError(f"材料不在允许范围内：{path}")
    with open(real, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    if keep_tail:
        return f"[前 {cut} 字符因材料上限省略]\n" + text[-limit:]
    return text[:limit] + f"\n[后 {cut} 字符因材料上限省略]"


def _inline_materials(mem_root, ws, include_journal=True):
    """按优先级返回 ``[(标题, 路径, 保留尾部)]``。不含 sealed、fact 与工作区以外的文件。"""
    lay = layout.workspace_layout(mem_root, ws)
    current = model_path(mem_root, ws)
    confirmed, legacy = _sot_judgments(mem_root, ws)
    out = []
    if os.path.isfile(current):
        out.append(("上一版项目模型（只作事实与依据）", current, False))
    out += [("已确认 pinned 输入", p, False) for p in confirmed]
    jp = journal.path_of(mem_root, ws)
    if include_journal and os.path.isfile(jp):
        out.append(("journal（只用于识别变化）", jp, True))
    out += [("旧未确认 judgment（仅作线索，不具有 pinned 优先级）", p, False) for p in legacy]
    try:
        names = sorted(os.listdir(lay["notes"]))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(lay["notes"], name)
        if (name.endswith(".md") and os.path.isfile(path)
                and os.path.realpath(path) != os.path.realpath(current)):
            out.append(("历史判断（首次重建时可吸收）", path, False))
    return out


def build_inline_prompt(host, ws, item, coverage=None):
    """Pi 通道的单工作区提示：材料全部内联，模型不需要也不能读文件。"""
    done, pending = coverage or _coverage_snapshot(host.mem_root)
    item["style_contract"] = (FORMAT_VERSION, _style_text())
    _capture_material_revision(host, ws, item)
    context = _ws_context(host, ws)
    lines = _intro_lines(done, pending, inline=True) + [f"## 工作区 {ws}"]
    if context.get("description"):
        lines.append(f"- 工作区定位：{context['description']}")
    lines += _input_lines(item)
    lines += ["", "## 材料", ""]
    budget = INLINE_TOTAL_LIMIT
    skipped = []
    # 已有上一版模型时只送上次成功重建之后新增的 journal；之前的内容已经吸收进上一版模型。
    item["journal_mark"] = _journal_mark(host.mem_root, ws)
    since = None
    if os.path.isfile(model_path(host.mem_root, ws)):
        since = _journal_since(host.mem_root, ws, item.get("journal_since"), INLINE_FILE_LIMIT)
    item["journal_incremental"] = since is not None
    materials = _inline_materials(host.mem_root, ws, include_journal=since is None)
    if since is not None:
        # 上一版模型排第一；新增 journal 紧随其后，保证在预算内。
        head, rest = materials[:1], materials[1:]
        for title, path, tail in head:
            text = _read_material(host.mem_root, path, min(INLINE_FILE_LIMIT, budget), keep_tail=tail)
            budget -= len(text)
            lines += [f"### {title}：{os.path.basename(path)}", "<<<材料开始", text.strip(), "材料结束>>>", ""]
        body = since.strip() or "（上次重建后 journal 没有新增内容）"
        budget -= len(body)
        lines += ["### 上次重建后新增的 journal（只用于识别变化）", "<<<材料开始", body, "材料结束>>>", ""]
        materials = rest
    for title, path, tail in materials:
        if budget <= 0:
            skipped.append(os.path.basename(path))
            continue
        text = _read_material(host.mem_root, path, min(INLINE_FILE_LIMIT, budget), keep_tail=tail)
        budget -= len(text)
        lines += [f"### {title}：{os.path.basename(path)}", "<<<材料开始", text.strip(), "材料结束>>>", ""]
    if not any(line.startswith("### ") for line in lines):
        lines += ["（本工作区暂无文件材料，只依据上面的备忘与候选。）", ""]
    if skipped:
        lines += ["以下材料因超出本次上限未附上：" + "、".join(skipped), ""]
    return "\n".join(lines + _contract_lines(host, inline=True, style_contract=item["style_contract"]))


def _section_matches(body):
    return list(re.finditer(r"(?m)^## ([^#\n].*?)\s*$", body))


def _section_bodies(body):
    """解析固定四段；返回 ``[(heading, content)]``，结构不合法时返回 None。"""
    matches = _section_matches(body)
    if tuple(HEADING_ALIASES.get(m.group(1), m.group(1)) for m in matches) != HEADINGS:
        return None
    out = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((HEADING_ALIASES.get(match.group(1), match.group(1)), body[match.end():end].strip()))
    return out


def _validate(path, ws):
    try:
        from curator.note import read_note
        fm, body = read_note(path)
    except Exception as exc:
        return False, f"frontmatter 无效：{exc}"
    expected = model_id(ws)
    if str(fm.get("id") or "") != expected:
        return False, f"id 应为 {expected}"
    expected_meta = {"type": "project", "tier": "notes", "kind": "judgment",
                     "confidence": "reported", "role": ROLE}
    if any(fm.get(key) != value for key, value in expected_meta.items()):
        return False, "type/tier/kind/confidence/role 不符合项目记忆契约"
    sections = _section_bodies(body)
    if sections is None:
        return False, "四段标题缺失、顺序错误或存在额外二级标题"
    if len(body) > BODY_CHAR_LIMIT:
        return False, f"正文超过 {BODY_CHAR_LIMIT} 字符：{len(body)}；必须按决策价值重写，不能机械截断"
    return True, ""


def _stamp_dates(staging, target):
    """日期由宿主填写，不采用模型给的值。

    模型不知道当天日期，2026-10-03 的重建把 updated 写成了 2026-06-26，界面据此显示的
    “更新于”会误导人。updated 取今天；created 沿用上一版模型，首次生成取今天。
    """
    from datetime import date
    from curator.note import write_note
    fm, body = read_note(staging)
    today = date.today().isoformat()
    created = today
    try:
        old, _ = read_note(target)
        value = str(old.get("created") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and value <= today:
            created = value
    except Exception:
        pass
    fm["created"], fm["updated"] = created, today
    # 只清理已通过正文校验的新成品；旧模型在成功重建前保持原样。
    fm.pop("capsule", None)
    # 仅规范化通过校验的生成暂存文件；不直接修改旧模型、流水或 pinned。
    body = re.sub(r"(?m)^## ([^#\n].*?)[ \t]*$",
                  lambda m: "## " + HEADING_ALIASES.get(m.group(1), m.group(1)), body)
    write_note(staging, fm, body)


def _tag(kind, t_wall, t_mono):
    slept = (time.time() - t_wall) - (time.monotonic() - t_mono)
    return f"host-slept/{kind}" if slept > SLEEP_SLACK else kind


def _remove_staging(ws_items):
    """坏 staging 不能留给下一轮误读；删除失败只影响清理，不改变恢复判断。"""
    for _ws, item in ws_items:
        try:
            os.remove(item["staging"])
        except OSError:
            pass


def _recoverable_output_reason(reason):
    return str(reason or "") in RECOVERABLE_OUTPUT_REASONS


def _workspace_errors(state):
    errors = state.get("workspace_errors")
    if not isinstance(errors, dict):
        errors = {}
        state["workspace_errors"] = errors
    return errors


def _mark_workspace_error(state, due, reason, detail):
    now = int(time.time())
    errors = _workspace_errors(state)
    for ws, item in due:
        old = errors.get(ws) if isinstance(errors.get(ws), dict) else {}
        fingerprint = item.get("fingerprint")
        previous = int(old.get("fails") or 0) if old.get("fingerprint") == fingerprint else 0
        errors[ws] = {"fails": previous + 1, "fingerprint": fingerprint,
                      "reason": reason, "detail": str(detail or "")[:ERR_KEEP], "ts": now}


def _clear_workspace_errors(state, due):
    errors = state.get("workspace_errors")
    if not isinstance(errors, dict):
        return
    for ws, _item in due:
        errors.pop(ws, None)
    if not errors:
        state.pop("workspace_errors", None)


def _command():
    configured = env_get("CMD")
    if configured:
        return shlex.split(configured)
    # 项目记忆会把工作区材料发给所配置的模型。默认只使用本机已登录的 Claude Code；
    # 需要其它通道（例如自托管模型）时显式配置 MEMORY_ENGINE_CMD。失败不自动换通道。
    claude = cli_path.which("claude")
    if not claude:
        return []
    cmd = [claude, "--dangerously-skip-permissions", "-p"]
    model = env_get("MODEL", "").strip()
    return cmd + (["--model", model] if model else [])


def _reindex(mem_root):
    try:
        # 新装环境的 .memory 只有工作区目录，没有 curator 实例配置；reindex 会以
        # “不像 curator 实例”失败，把已经生成的项目模型记成失败并反复重建
        # （2026-10-03 实见）。按种子补齐缺失配置，已有文件不动。
        layout.ensure_instance(mem_root)
        import contextlib
        import io
        from curator.cli import main as curator_main
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return curator_main(["--root", mem_root, "reindex"]) == 0
    except Exception:
        return False


def pi_models(mem_root):
    """返回 ``(模型列表, 配置错误)``。配置文件存在但无效时报错，不悄悄退回其他通道。"""
    raw = env_get("PI_MODELS", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()], ""
    path = os.path.join(_state_dir(mem_root), CHANNEL_FILE)
    if not os.path.exists(path):
        return [], ""
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get("pi_models")
    except Exception as exc:
        return [], f"{path} 无法解析：{exc}"
    if not isinstance(value, list) or not value or not all(isinstance(m, str) and m.strip() for m in value):
        return [], f"{path} 的 pi_models 必须是非空字符串列表"
    return [m.strip() for m in value], ""


def _clean_inline_output(text):
    """去掉模型偶尔加上的代码围栏或前言，保留从 frontmatter 开始的完整文件。"""
    text = str(text or "").strip()
    fenced = re.fullmatch(r"\x60{3}[\w-]*\n([\s\S]*?)\n\x60{3}", text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("---"):
        at = text.find("\n---\n")
        if at >= 0:
            text = text[at + 1:]
    # 实测 deepseek-flash 会漏掉 frontmatter 的结束分隔线；第一个二级标题之前补上，
    # 其余格式问题仍交给 _validate 判定。
    if text.startswith("---\n"):
        heading = text.find("\n## ")
        closing = text.find("\n---\n", 3)
        if heading >= 0 and (closing < 0 or closing > heading):
            text = text[:heading].rstrip() + "\n---\n" + text[heading:]
    return text + "\n" if text else ""


def _run_pi(pi, model, prompt, cwd, system=PI_SYSTEM, timeout=PI_TIMEOUT):
    """调用一次 Pi。返回 ``(正文或 None, 失败类别, 说明, 用量)``。"""
    cmd = [pi, "-p", "--mode", "json", "--no-session", "--no-tools", "--no-extensions", "--no-skills",
           "--no-context-files", "--model", model, "--system-prompt", system]
    t_wall, t_mono = time.time(), time.monotonic()
    try:
        # Decode in the caller, not Windows subprocess reader threads: their
        # UnicodeDecodeError can otherwise become only a warning plus stdout=None.
        result = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True,
                                timeout=timeout, cwd=cwd, env={**os.environ, "PI_OFFLINE": "1"})
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
    except subprocess.TimeoutExpired:
        return None, _tag("timeout", t_wall, t_mono), f"超过 {timeout} 秒未结束", {}
    except UnicodeDecodeError as exc:
        return None, "invalid-output", f"进程输出不是 UTF-8：{exc}", {}
    except OSError as exc:
        return None, "spawn", f"{type(exc).__name__}: {exc}", {}
    message = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            message = event["message"]
    raw = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    usage = {"input": raw.get("input"), "output": raw.get("output"),
             "cached": raw.get("cacheRead"), "cache_write": raw.get("cacheWrite")}
    if result.returncode != 0:
        tail = stderr.strip()[-ERR_KEEP:]
        return None, _tag("exit", t_wall, t_mono), f"退出码 {result.returncode}" + (f"：{tail}" if tail else ""), usage
    if message.get("stopReason") == "error" or str(message.get("errorMessage") or "").strip():
        return None, "exit", "服务端错误：" + str(message.get("errorMessage") or "")[:ERR_KEEP], usage
    text = "".join(b.get("text") or "" for b in (message.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text")
    text = _clean_inline_output(text)
    if not text:
        return None, "invalid-output", "输出为空", usage
    return text, "", "", usage


def run_short(mem_root, prompt, system, timeout=60):
    """按项目记忆的 Pi 模型顺序做一次短文本生成（如会话标题）。

    返回 ``(正文或 None, 所用模型, [{model, reason, detail, usage}...], 成功那次的用量)``。
    未配置 Pi 通道时返回 ``(None, "", [], {})``，由调用方退回原做法。
    """
    models, error = pi_models(mem_root)
    pi = cli_path.which("pi") if models and not error else None
    if not pi:
        return None, "", ([{"model": "", "reason": "config", "detail": error, "usage": {}}] if error else []), {}
    attempts = []
    for model in models:
        text, reason, detail, usage = _run_pi(pi, model, prompt, _state_dir(mem_root), system, timeout)
        if text is not None:
            return text.strip(), model, attempts, usage
        attempts.append({"model": model, "reason": reason, "detail": str(detail)[:ERR_KEEP], "usage": usage})
    return None, "", attempts, {}


def _record_usage(host, model, usage, outcome):
    recorder = getattr(host, "record_usage", None)
    if not callable(recorder) or not any(usage.values()):
        return
    try:
        recorder("memory-rebuild", model, usage, outcome)
    except Exception:
        pass


def _note_fallback(host, ws, used, attempts):
    entry = {"ts": int(time.time()), "ws": ws, "used": used, "failed": attempts}
    path = os.path.join(_state_dir(host.mem_root), FALLBACK_LOG)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    first = attempts[0]
    try:
        host.log("memory", f"项目记忆 {ws}：{first['model']} 失败（{first['reason']}），已改用 {used}")
    except Exception:
        pass


def fallbacks_since(mem_root, ts):
    path = os.path.join(_state_dir(mem_root), FALLBACK_LOG)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if int(entry.get("ts") or 0) >= int(ts):
                    out.append(entry)
    except OSError:
        pass
    return out


def _run_pi_engine(host, ws_items, models):
    """逐个工作区调用 Pi，按模型顺序尝试；全部工作区合格后才一起替换成品。"""
    pi = cli_path.which("pi")
    if not pi:
        return False, "no-engine", "已配置 Pi 记忆通道，但 PATH 里找不到 pi"
    state_dir = _state_dir(host.mem_root)
    mark = os.path.join(state_dir, "running")
    coverage = _coverage_snapshot(host.mem_root)
    _remove_staging(ws_items)
    try:
        with open(mark, "w", encoding="utf-8") as f:
            f.write(f"{int(time.time())} ws={','.join(ws for ws, _ in ws_items)} channel=pi\n")
        for ws, item in ws_items:
            try:
                prompt = build_inline_prompt(host, ws, item, coverage=coverage)
            except Exception as exc:
                _remove_staging(ws_items)
                return False, "input", f"{ws}: 材料读取失败：{exc}"[:ERR_KEEP]
            attempts = []
            for model in models:
                text, reason, detail, usage = _run_pi(pi, model, prompt, state_dir)
                if text is not None:
                    os.makedirs(os.path.dirname(item["staging"]), exist_ok=True)
                    with open(item["staging"], "w", encoding="utf-8") as f:
                        f.write(text)
                    ok, why = _validate(item["staging"], ws)
                    if ok:
                        _record_usage(host, model, usage, "ok")
                        if attempts:
                            _note_fallback(host, ws, model, attempts)
                        break
                    _remove_staging([(ws, item)])
                    reason, detail = "invalid-output", why
                _record_usage(host, model, usage, "invalid" if reason == "invalid-output" else "failed")
                attempts.append({"model": model, "reason": reason, "detail": str(detail)[:ERR_KEEP]})
            else:
                _remove_staging(ws_items)
                hard = [a["reason"] for a in attempts if a["reason"] != "invalid-output"]
                summary = "；".join(f"{a['model']} {a['reason']}：{a['detail']}" for a in attempts)
                return False, (hard[0] if hard else "invalid-output"), f"{ws}: {summary}"[:ERR_KEEP * 2]
        for ws, item in ws_items:
            target = model_path(host.mem_root, ws)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _stamp_dates(item["staging"], target)
            os.replace(item["staging"], target)
        if not _reindex(host.mem_root):
            return False, "reindex", "项目记忆已生成，但索引重建失败"
        return True, "", ""
    finally:
        try:
            os.remove(mark)
        except OSError:
            pass


def run_engine(host, ws_items):
    """运行一次无头记忆引擎，验证 staging 后原子替换成品。"""
    if not env_get("CMD"):
        models, error = pi_models(host.mem_root)
        if error:
            return False, "config", error
        if models:
            return _run_pi_engine(host, ws_items, models)
    base_cmd = _command()
    if not base_cmd:
        return False, "no-engine", "PATH 里找不到可用的记忆引擎命令"
    state_dir = _state_dir(host.mem_root)
    mark = os.path.join(state_dir, "running")
    sent = os.path.join(state_dir, "process-ok")
    for _ws, item in ws_items:
        os.makedirs(os.path.dirname(item["staging"]), exist_ok=True)
        try:
            os.remove(item["staging"])
        except OSError:
            pass
    try:
        prompt = build_prompt(host, ws_items)
    except Exception as exc:
        _remove_staging(ws_items)
        return False, "input", f"生成提示读取失败：{exc}"[:ERR_KEEP]
    last = ""
    last_reason = "spawn"
    for _attempt in range(1 + SPAWN_RETRY):
        t_wall, t_mono = time.time(), time.monotonic()
        try:
            _remove_staging(ws_items)
            try:
                os.remove(sent)
            except OSError:
                pass
            with open(mark, "w", encoding="utf-8") as f:
                f.write(f"{int(t_wall)} ws={','.join(ws for ws, _ in ws_items)}\n")
            import oscompat
            result = oscompat.run_with_success_marker(
                base_cmd, "MEMORY_ENGINE_PROCESS_OK", sent,
                input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT, cwd=host.mem_root,
            )
            if not os.path.exists(sent):
                detail = (result.stderr or result.stdout or "").strip()[-ERR_KEEP:]
                return False, _tag("no-sentinel", t_wall, t_mono), detail
            for ws, item in ws_items:
                ok, why = _validate(item["staging"], ws)
                if not ok:
                    last = f"{ws}: {why}"
                    last_reason = "invalid-output"
                    _remove_staging([(ws, item)])
                    break
            else:
                for ws, item in ws_items:
                    target = model_path(host.mem_root, ws)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    _stamp_dates(item["staging"], target)
                    os.replace(item["staging"], target)
                if not _reindex(host.mem_root):
                    return False, "reindex", "项目记忆已生成，但索引重建失败"
                return True, "", ""
            time.sleep(3)
            continue
        except subprocess.TimeoutExpired as exc:
            tail = ((exc.stderr or "") + (exc.stdout or "")).strip()[-ERR_KEEP:]
            return False, _tag("timeout", t_wall, t_mono), f"超过 {TIMEOUT} 秒未结束" + (f"；末尾输出：{tail}" if tail else "")
        except UnicodeDecodeError as exc:
            last = f"进程输出不是 UTF-8：{exc}"[:ERR_KEEP]
            last_reason = "invalid-output"
            _remove_staging(ws_items)
            time.sleep(3)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"[:ERR_KEEP]
            last_reason = "spawn"
            time.sleep(3)
        finally:
            try:
                os.remove(mark)
            except OSError:
                pass
    if last:
        _remove_staging(ws_items)
        return False, last_reason, last[:ERR_KEEP]
    return False, _tag("spawn", t_wall, t_mono), last


def _reset_workspace(mem_root, ws):
    """成功生成后清旧 notes/journal；旧 SOT 与 sealed 都保持只读，不自动删除。"""
    lay = layout.workspace_layout(mem_root, ws)
    target = os.path.realpath(model_path(mem_root, ws))
    try:
        names = os.listdir(lay["notes"])
    except OSError:
        names = []
    for name in names:
        path = os.path.join(lay["notes"], name)
        if name.endswith(".md") and os.path.isfile(path) and os.path.realpath(path) != target:
            os.remove(path)
    jp = journal.path_of(mem_root, ws)
    if os.path.isfile(jp):
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        tmp = jp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(journal.TPL.format(ws=ws))
        os.replace(tmp, jp)


def _record_success(host, state, due, reset_legacy=False):
    now = int(time.time())
    fingerprints = state.get("fingerprints") if isinstance(state.get("fingerprints"), dict) else {}
    last_ws = state.get("last_run_ws") if isinstance(state.get("last_run_ws"), dict) else {}
    marks = state.get("journal_marks") if isinstance(state.get("journal_marks"), dict) else {}
    for ws, item in due:
        if reset_legacy:
            _reset_workspace(host.mem_root, ws)
            marks[ws] = _journal_mark(host.mem_root, ws)
        elif item.get("journal_mark"):
            # 只记构造提示那一刻的位置；生成期间新追加的流水留给下一次。
            marks[ws] = item["journal_mark"]
        memos = item.get("memos") or []
        if memos and getattr(host, "memos_done", None):
            try:
                host.memos_done(ws, memos)
            except Exception:
                pass
        if reset_legacy:
            fingerprints[ws] = source_fingerprint(
                host.mem_root, ws, (), item.get("memory_candidates") or (), item.get("style_contract"))
        elif item.get("material_revision"):
            fingerprints[ws] = _with_model_revision(host.mem_root, ws, item["material_revision"])
        else:
            # 无生成前回执不能把当前材料当作已吸收；保留旧指纹，下一轮继续核对。
            fingerprints[ws] = item.get("fingerprint")
        last_ws[ws] = now
    _clear_workspace_errors(state, due)
    state.update({"fingerprints": fingerprints, "last_run_ws": last_ws, "journal_marks": marks,
                  "last_ok": now, "fails": 0, "last_error": None})
    save_state(state_path(host.mem_root), state)
    _reindex(host.mem_root)


def refresh(host, workspaces=None, force=False, reset_legacy=False):
    """刷新一批项目记忆，返回一行供调度报告展示；改用过备用模型时附上说明。"""
    started = int(time.time())
    line = _refresh(host, workspaces=workspaces, force=force, reset_legacy=reset_legacy)
    used = fallbacks_since(host.mem_root, started)
    if used:
        names = "、".join(f"{e.get('ws')}→{e.get('used')}" for e in used)
        line += (f"；{len(used)} 个工作区主模型失败后改用备用模型（{names}），"
                 f"原因见 {os.path.join(_state_dir(host.mem_root), FALLBACK_LOG)}")
    return line


def _refresh(host, workspaces=None, force=False, reset_legacy=False):
    if not force and not enabled():
        return "记忆引擎：off(MEMORY_ENGINE_PROJECT_MEMORY/DESKTRON_PROJECT_MEMORY)"
    sp = state_path(host.mem_root)
    state = load_state(sp)
    if (not force and int(state.get("fails") or 0) >= FAIL_STOP
            and not _recoverable_output_reason((state.get("last_error") or {}).get("reason"))):
        return f"记忆引擎：连续 {state['fails']} 轮失败已停；修复后把 {sp} 的 fails 置 0"
    if _recoverable_output_reason((state.get("last_error") or {}).get("reason")) and int(state.get("fails") or 0):
        # 旧版本把模型坏输出计入全局停机；新版按工作区隔离，不让历史状态继续卡住整机。
        state["fails"] = 0
    now = int(time.time())
    try:
        due = candidates(host, state=state, force=force, workspaces=workspaces)
    except (OSError, ValueError) as exc:
        state["last_error"] = {"ts": now, "reason": "input", "detail": str(exc)[:ERR_KEEP]}
        save_state(sp, state)
        return "记忆引擎：本轮失败（input，表达规范或项目材料读取失败；原模型保留）"
    last_ws = state.get("last_run_ws") if isinstance(state.get("last_run_ws"), dict) else {}
    if not force:
        due = [(ws, item) for ws, item in due if now - int(last_ws.get(ws) or 0) >= MIN_GAP_S]
        errors = state.get("workspace_errors") if isinstance(state.get("workspace_errors"), dict) else {}
        # 失败预算绑定当时的输入；规范或材料已修复时允许自动重试。
        skipped = [(ws, item) for ws, item in due
                   if int((errors.get(ws) or {}).get("fails") or 0) >= FAIL_STOP
                   and (errors.get(ws) or {}).get("fingerprint") == item["fingerprint"]]
        due = [(ws, item) for ws, item in due if ws not in {sws for sws, _ in skipped}]
        if skipped and not due:
            return "记忆引擎：工作区输出连续失败已暂停（" + "、".join(ws for ws, _ in skipped) + "）；修复输入或 force 后重试"
    due = due[:MAX_WS]
    if not due:
        return "记忆引擎：项目记忆已是最新"
    ok, reason, detail = run_engine(host, due)
    if ok:
        _record_success(host, state, due, reset_legacy=reset_legacy)
        return f"记忆引擎：已更新 {len(due)} 个工作区（{'、'.join(ws for ws, _ in due)}）"
    if _recoverable_output_reason(reason):
        successes, failures = [], []
        if len(due) > 1:
            for one in due:
                ok1, reason1, detail1 = run_engine(host, [one])
                if ok1:
                    successes.append(one)
                elif _recoverable_output_reason(reason1):
                    failures.append((one, detail1))
                else:
                    state["last_error"] = {"ts": now, "reason": reason1, "detail": detail1,
                                           "ws": [one[0]]}
                    if not str(reason1).startswith("host-slept"):
                        state["fails"] = int(state.get("fails") or 0) + 1
                    save_state(sp, state)
                    return f"记忆引擎：本轮失败（{reason1}，连续 {int(state.get('fails') or 0)}/{FAIL_STOP}）"
        else:
            failures = [(due[0], detail)]
        if successes:
            _record_success(host, state, successes, reset_legacy=reset_legacy)
            state = load_state(sp)
        if failures:
            _mark_workspace_error(state, [one for one, _detail in failures], reason, "; ".join(str(d) for _one, d in failures))
            state["last_error"] = {"ts": now, "reason": reason,
                                   "detail": "; ".join(str(d) for _one, d in failures)[:ERR_KEEP],
                                   "ws": [one[0] for one, _detail in failures]}
            state["fails"] = 0
            save_state(sp, state)
        if successes:
            return (f"记忆引擎：已更新 {len(successes)} 个工作区（{'、'.join(ws for ws, _ in successes)}）；"
                    f"{len(failures)} 个工作区输出无效已跳过，不计全局停机")
        return f"记忆引擎：本轮输出无效已丢弃（invalid-output，不计全局停机，工作区 {'、'.join(one[0] for one, _d in failures)}）"
    state["last_error"] = {"ts": now, "reason": reason, "detail": detail,
                           "ws": [ws for ws, _ in due]}
    if not str(reason).startswith("host-slept"):
        state["fails"] = int(state.get("fails") or 0) + 1
    save_state(sp, state)
    return f"记忆引擎：本轮失败（{reason}，连续 {int(state.get('fails') or 0)}/{FAIL_STOP}）"


def rebuild_all(host, reset_legacy=False):
    """显式全量重建。每批成功才清该批旧记忆，失败即停止，返回各批状态。"""
    pending = _workspace_names(host.mem_root)
    lines = []
    while pending:
        batch = pending[:MAX_WS]
        line = refresh(host, workspaces=batch, force=True, reset_legacy=reset_legacy)
        lines.append(line)
        if "本轮失败" in line:
            break
        pending = pending[MAX_WS:]
    return lines


def project_context(mem_root, ws):
    """仅供应本工作流项目模型的完整正文，不读流水、原始对话或 sealed。

    旧模型的 capsule 忽略，不能作为正文缺失时的替代。保留原文，不机械截断，
    2500 字符上限由新成品生成校验保证；只读消费不改写旧模型。
    """
    try:
        fm, body = read_note(model_path(mem_root, ws))
    except FileNotFoundError:
        return ""
    if not body.strip():
        return ""
    updated = str(fm.get("updated") or fm.get("created") or "未记录")
    return f"项目模型：`{model_id(ws)}`；更新时间：{updated}\n\n" + body.strip()
