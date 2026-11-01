"""grounding 的装配：系统约定与当前上下文。"""
import os


def _domains():
    import domains
    return domains


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit).strip()
    except Exception:
        return ""


def _localized(fn, *args, locale="zh"):
    """Chinese calls keep their original signature; only English passes ``locale``."""
    return fn(*args, locale=locale) if locale == "en" else fn(*args)


def constant(workspace, domain, engine="", locale="zh"):
    """构造系统约定：角色、站位、工作流职责、红线、取用入口。

    不含任何随数据增长的内容——记忆条数、待办都放在当前上下文中。
    表达约束（prompts/style.md）由适配器另行拼进系统提示词，这里不重复。
    """
    import prompt_locale
    d = _domains()
    segs = []
    # 三层依次:起手地图(你在哪) → 红线(不许做什么) → 判断原则(分叉往哪走)
    # 英文会话优先读同名 .en.md;缺英文文件时回落中文原文,不留空。
    for rel in ("common/orientation.md", "AGENTS.md", "common/principles.md"):
        body = _read(os.path.join(workspace, prompt_locale.localized_rel(workspace, rel, locale)))
        if body:
            segs.append(body)
    if domain:
        ws = domain.split(":")[0]
        # 工作流自己的原则(人写,可缺)在全局原则之后:同一分叉上它的口径优先。
        wp = _read(os.path.join(workspace, prompt_locale.localized_rel(
            workspace, os.path.join(".workbench", "workspaces", ws + ".principles.md"), locale)))
        if wp:
            segs.append(wp)
    # 技能不再枚举；orientation 只保留本地技能路径入口。
    for fn in (lambda: _localized(d._grounding_index, (domain or "_desktop").split(":")[0], "nav",
                                  locale=locale),):
        try:
            s = fn()
        except Exception:
            s = ""
        if s:
            segs.append(s)
    return "\n\n---\n\n".join(segs)


def scene(workspace, domain, locale="zh"):
    """Project context; adapters refresh it outside the user frame.

    Keep the full project model and repository safety excerpts, but read task state
    on demand. A change rotates the native host, never rewrites historical turns.
    """
    import prompt_locale
    d = _domains()
    ws = (domain or "").split(":")[0]
    segs = []
    if ws:
        try:
            wf_dir = d._ensure_domain_dir(domain)
        except Exception:
            wf_dir = os.path.join(workspace, "domains", ws)
        for fn in (lambda: _localized(d._grounding_index, ws, "scene", locale=locale),
                   lambda: _localized(d._context_segment, ws, locale=locale),
                   lambda: _repos_with_conventions(d, ws, wf_dir, workspace, locale)):
            try:
                s = fn()
            except Exception:
                s = ""
            if s:
                segs.append(s)
        segs.append(prompt_locale.text(TASK_ENTRY, locale))
    return "\n\n---\n\n".join(segs)


TASK_ENTRY = ('### 任务与项目状态入口\n\n'
              '当前任务按需执行 `desk-task list` 查询；任务全文及证据用 `desk-task show`。'
              '本段是项目导航，不代表当前任务状态或用户授权。')


# 单仓 / 合计取多少字。2026-08-24 从 2000/3000 收到 800/1600:实测三个仓吃掉 4,587 字符,
# 是当前上下文里最大的一项,而截断处本来就带着"全文见 <path>"的指针——真要动那个仓时会去读全文,
# 起手需要的只是"这仓是干什么的、有什么红线"。
REPO_CONV_ONE = 800
REPO_CONV_TOTAL = 1600


def _repos_with_conventions(d, ws, wf_dir, workspace="", locale="zh"):
    """引用代码仓：清单之外把各仓自带的 AGENTS.md（没有就 CLAUDE.md / README.md）正文一并带上。

    此前这一段只给一句「开工前先读各目录自带的 AGENTS.md」。那是一条自然语言指令，
    执行与否取决于模型当时的判断，而仓库自带的约定往往含红线与禁令，不宜依赖概率性执行。
    仓库侧仍然一个字都不写，只是把「让 agent 去读」换成「起手就读好了」。

    两处收口：与工作台自己的宪法同文的跳过（desk 源码仓引用自身时会撞上，纯重复）；
    单仓与合计都有上限，超出只留指针——引用三四个仓时正文很容易盖过其余所有段落。
    """
    import prompt_locale
    en = prompt_locale.normalize(locale) == "en"
    base = _localized(d._repos_segment, ws, wf_dir, locale=locale)
    if not base:
        return ""
    own = _read(os.path.join(workspace, "AGENTS.md")) if workspace else ""
    out, used, skipped = [base], 0, []
    for it in d._repo_items(ws):
        for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
            body = _read(os.path.join(it["dir"], name), REPO_CONV_ONE + 1)
            if not body:
                continue
            if own and body[:400] == own[:400]:
                out.append(f"#### {it['name']}: same as the Desktron working rules; not repeated"
                           if en else f"#### {it['name']}：说明与 Desktron 工作规则相同，不重复")
                break
            if used >= REPO_CONV_TOTAL:
                skipped.append(f"{it['dir']}/{name}")
                break
            more = ""
            if len(body) > REPO_CONV_ONE:
                body, more = body[:REPO_CONV_ONE], (
                    f"\n\n(Truncated here; full text in {it['dir']}/{name})" if en
                    else f"\n\n（此处截断，全文见 {it['dir']}/{name}）")
            used += len(body)
            out.append((f"#### {it['name']} instructions ({name}; its requirements take precedence)"
                        if en else f"#### {it['name']} 的说明（{name}，其中的要求优先）")
                       + f"\n\n{body}{more}")
            break
    if skipped:
        out.append("Instructions for the remaining repositories are not included here; read them "
                   "before making changes: " + ", ".join(skipped) if en
                   else "其余代码仓的说明未附在此处，修改前先读：" + "、".join(skipped))
    return "\n\n".join(out)


def build(workspace, domain, first_turn=True, engine="", locale="zh"):
    """返回系统约定与当前上下文；内部键名为兼容既有适配器保持不变。"""
    return {"constant": constant(workspace, domain, engine, locale),
            "scene": scene(workspace, domain, locale) if first_turn else ""}
