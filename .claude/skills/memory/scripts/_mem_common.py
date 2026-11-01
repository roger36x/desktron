"""desktron 记忆命令的共享工具。

写入侧包一层 curator:纪律(三档派生、verify 绑正文 hash、表达黑名单、索引、访问计数)全部
复用 curator 引擎,不在 desk 重写。mem_* 命令是本地入口(命令面保留),映射成对一个 desk 本地
curator 实例的 add/update/verify/lint 调用。

工作层布局取自会话 1 冻结的契约 `memory_store._workspace_layout(ws)`(键 temp/private/shared/
imports),实例根取自 `memory_store._memory_root()`,不自拼路径。band 名 temp/private/shared
(dir:temp + local/{private,shared};旧扁平 temp/local/private 与 area/shared/journal 退役)。记忆笔记 kind=judgment。

历史 frontmatter 工具(read_md/write_md)供 mem_migrate 复用;表达黑名单与置信度纪律已全归
curator 引擎(读各实例 rules.yaml),不在 desk 重复。
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    print("需要 PyYAML:pip install pyyaml", file=sys.stderr)
    sys.exit(1)

SKILL_DIR = Path(__file__).resolve().parent.parent          # .claude/skills/memory
REPO_ROOT = SKILL_DIR.parents[2]                            # .claude/skills/memory → REPO_ROOT
# 实例种子(ontology/rules)已随引擎走:单一源 memory_engine/seeds,经 ensure_instance 物化。
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)

# memory_engine 的解析路径必须显式钉住,不能让它撞运气。开发机上常另外 `pip install -e`
# 一份独立仓(engine 抽取出去之后的正常开发方式),那份会抢到 import 优先级——凡是本进程
# 第一次 `import memory_engine` 落在哪份,这个进程往后所有 memory_engine.* 都定在那份
# (Python 的包缓存)。Desk 自己随发行版带的是 desktron/lib/memory_engine,任何机器都有、
# 且是运行中的 app 后台调度器实际在用的那份;不显式插到 sys.path 最前,skill 脚本会跟着
# pip 装的那份走,与 app 用的不是同一份代码,曾经各自漂移过一段时间才被发现(2026-08-21)。
_ENGINE_LIB = str(REPO_ROOT / "desktron" / "lib")
if _ENGINE_LIB not in sys.path:
    sys.path.insert(0, _ENGINE_LIB)

# 契约常量与纪律(tier 别名/查重/emoji 预检/curator 调用)单一源在 memory-engine,此处 re-export。
from memory_engine.commands import (TYPES, TIERS, TIER_ALIAS, SCOPE_ALIAS, ID_RE, EMOJI_RE,  # noqa: E402,F401
                                    EngineError)
from memory_engine import commands as _eng_cmd, layout as _eng_layout  # noqa: E402


def die(msg):
    print("✗ " + msg, file=sys.stderr)
    sys.exit(1)


def today():
    return date.today().isoformat()


# —— 会话 1 冻结契约的单一出口(不自拼路径) ——

def _runtime_repo_root():
    """运行态 REPO_ROOT(数据根,desk 启动时的 WORKSPACE)。解析顺序:
    1) env DESKTRON_REPO_ROOT(分发态权威来源);
    2) cwd 在受管工作区下(<root>/domains/<id>[/<sub>])→ 取 domains 之前那段;
    3) cwd 在 desk 工作区树内(== 或位于 REPO_ROOT 常量之下)→ 用 cwd(desktop / 工作区子目录);
    4) 其余(外部代码仓:coding 项目 / 编码子区等)→ 回落工作区根,**绝不**把外部仓当数据根——
       否则记忆 / 知识会写进 <外部仓>/.memory、<外部仓>/knowledge,污染真实仓(踩「不污染真实仓」红线)。
    注:REPO_ROOT 常量是 skill 软链 resolve 出的 desk 根(dev 态即数据根);分发态应由 env 指定。"""
    env = os.environ.get("DESKTRON_REPO_ROOT")
    if env:
        return os.path.abspath(env)
    cwd = os.path.realpath(os.getcwd())
    parts = cwd.split(os.sep)
    if "domains" in parts:
        i = parts.index("domains")
        return os.sep.join(parts[:i]) or os.sep
    root = os.path.realpath(str(REPO_ROOT))
    if cwd == root or cwd.startswith(root + os.sep):
        return cwd   # desktop / 工作区子目录:仍在 desk 树内,保持原语义
    inst = _instance_root_above(cwd)
    if inst:
        # 分发态:skill 经软链 resolve 到代码根(home),而数据根是并列的 workspace,
        # 两者不互为前缀,上面两条都不命中。直接回落 REPO_ROOT 会指到 home/.memory
        # ——那是随发行版附带的空实例,search/list/graph 全查不到东西,而 show 按 id
        # 找文件仍能命中,故障因此长期不显形(2026-08-21 实测)。先认 cwd 之上已存在的
        # 实例,只认已存在的,不新建,故不会在外部仓里凭空造出数据根。
        return inst
    return root   # 外部仓 / 非受管 cwd:回落工作区根,绝不落外部仓(不污染真实仓)


def _instance_root_above(cwd):
    """从 cwd 向上找已存在的 curator 实例,返回它的父目录(数据根);找不到返回 ''。
    判据是 <dir>/.memory 存在且带 INDEX.md 或 .curator——只有物化过的实例才算,
    空目录不算,避免把偶然同名的目录当数据根。"""
    d = cwd
    while True:
        mem = os.path.join(d, ".memory")
        if os.path.isdir(mem) and (os.path.exists(os.path.join(mem, "INDEX.md"))
                                   or os.path.isdir(os.path.join(mem, ".curator"))):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


def runtime_repo_root():
    """公开别名(knowledge 等脚本用作 --root 默认):同 _runtime_repo_root,外部仓下也绝不返回外部仓。"""
    return _runtime_repo_root()


def _load_session1():
    """导入会话 1 的 memory_store(工作层路径契约的唯一出口)。
    memory_store 只依赖 deskcfg.REPO_ROOT,而 deskcfg 在 import 时吃 ui_server 的 sys.argv
    (int(argv[2]) 等),独立脚本无法直接 import。这里给 deskcfg 注一个只含 REPO_ROOT 的桩,
    再 import memory_store——band 路径仍由 _workspace_layout 给,只把 REPO_ROOT 这一个依赖喂进去。
    TODO(会话 1/2 接缝):让 memory_store 解耦 deskcfg 的 argv 依赖,或 desk 运行时导出
    DESKTRON_REPO_ROOT,即可去掉这里的桩与 _runtime_repo_root 启发式。"""
    lib = REPO_ROOT / "desktron" / "lib"      # 源码仓 lib(skill 软链 resolve 到源码仓)
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    if "deskcfg" not in sys.modules:
        import types
        stub = types.ModuleType("deskcfg")
        stub.REPO_ROOT = _runtime_repo_root()
        sys.modules["deskcfg"] = stub
    try:
        import memory_store
        return memory_store
    except Exception as e:
        die(f"无法导入会话 1 的 memory_store({lib}):{e}")


def work_root():
    """工作层 curator 实例根 = memory_store._memory_root()。"""
    return _load_session1()._memory_root()


def workspace_layout(ws):
    """会话 1 契约:ws → {temp,local,private,imports} 绝对路径。"""
    return _load_session1()._workspace_layout(ws)


def domain_for_cwd(cwd):
    """cwd → 工作区 id(local 桶的 topic 用)。desktop=_desktop,domains/<id>=该 id,外部=''。"""
    cwd = os.path.realpath(cwd or os.getcwd())
    if cwd == os.path.realpath(_runtime_repo_root()):
        return "_desktop"
    parts = cwd.split(os.sep)
    if "domains" in parts:
        i = parts.index("domains")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def ensure_instance(root):
    """工作层根缺 ontology/rules 时物化种子(单一源:memory_engine.seeds)。"""
    _eng_layout.ensure_instance(str(root))


def curator_target(ws, tier):
    """(ws, tier) → (root, band, topic)。判定在 memory_engine.commands,root 解析在本模块(宿主职责)。"""
    root = work_root()
    try:
        band, topic = _eng_cmd.curator_target(str(root), ws, tier)
    except EngineError as e:
        die(str(e))
    return root, band, topic


def check_emoji(body):
    """emoji / 装饰符号预检(委托 engine;返回旧接口的问题清单形态)。"""
    try:
        _eng_cmd.check_emoji(body)
        return []
    except EngineError:
        return ["正文含 emoji / 装饰符号"]


def dup_check(root, terms, self_id, force):
    """写前查重(委托 engine;命中转 die,保命令壳退出语义)。"""
    try:
        _eng_cmd.dup_check(str(root), terms, self_id, force)
    except EngineError as e:
        die(str(e))


def run_curator(root, sub, args, body=None):
    """调 curator(委托 engine;失败按 engine 给的退出码退出)。"""
    try:
        return _eng_cmd.run_curator(str(root), sub, args, body=body)
    except EngineError as e:
        print("✗ " + str(e), file=sys.stderr)
        sys.exit(e.rc)


# —— 历史 frontmatter 工具(mem_migrate 复用) ——

def read_md(path):
    if not Path(path).exists():
        return None, None
    t = Path(path).read_text(encoding="utf-8")
    m = FM_RE.match(t)
    if not m:
        return None, t
    try:
        return (yaml.safe_load(m.group(1)) or {}), m.group(2)
    except yaml.YAMLError:
        return None, t


def write_md(path, fm, body):
    fm_str = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(f"---\n{fm_str}\n---\n\n{body.strip()}\n", encoding="utf-8")


def check_body(body, rules):
    """表达黑名单(维护者视角 / 过程 / 浮夸 / emoji)。返回违规列表。"""
    hits = []
    for s in rules.get("forbidden_in_body", []):
        if s in body:
            hits.append(f"禁用表达「{s}」")
    if EMOJI_RE.search(body):
        hits.append("正文含 emoji / 装饰符号")
    return hits
