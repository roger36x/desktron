"""记忆写闸判定规则的单一源:各宿主钩子(claude PreToolUse、codex/cursor 闸、
引擎侧 js 插件)统一从这里取正则与文案,不各自手抄。

受闸桶:_sealed(红线封存)与 <ws>/sot(当前权威态,人审编辑);旧布局(<ws>/shared、
local/{private,shared}、cwd 软链 memory/{private,shared})保留兜住存量。
s0(流水)/notes(项目模型,记忆引擎自动写)免闸不匹配。

js 侧插件无法 import 本模块:规则以 PATTERN_JS 导出等价字符串,宿主用
check_js_sync() 校验插件源码未漂移(物理双份、逻辑单源)。
"""
import glob as _glob
import os
import re

PATTERN = (r"(^|/)\.memory/(_sealed|[\w.-]+/(sot|shared)|local/(private|shared))(/|$)"
           r"|(^|/)memory/(private|shared)(/|$)")
MEM_BLOCK = re.compile(PATTERN)
# journal 对系统调度器、迁移脚本仍是免闸层；但受管 Desk 主会话只能请求 recap，不能拿
# 自己写的摘要冒充隔离支线产物。是否属于受管会话由适配器注入的请求路径判定。
JOURNAL_PATTERN = r"(^|/)\.memory/[^/]+/(journal|s0)(/|$)"
JOURNAL_BLOCK = re.compile(JOURNAL_PATTERN)
# js 正则与 py 仅斜杠转义不同,语义等价(供 js 侧插件比对/生成)
PATTERN_JS = (r"(^|\/)\.memory\/(_sealed|[\w.-]+\/(sot|shared)|local\/(private|shared))(\/|$)"
              r"|(^|\/)memory\/(private|shared)(\/|$)")

MEM_MSG = (
    "拒绝:_sealed(红线封存)与 <ws>/sot(当前权威态)是记忆准入闸管控目录,不能直接写/编辑。"
    "请走命令(从工作区 cwd):python .claude/skills/memory/scripts/mem_add.py "
    "--tier sealed|pinned|fact --id <kebab-slug> [--ws <工作区>] [--source ...] [--gaps ...] "
    "--summary '一句话' --body '正文'(pinned 新增/更新须 --user-confirmed；fact 须 --kind fact)。"
    "更新用 mem_update.py。journal 由 recap 写、notes 由记忆引擎重写。"
)

JOURNAL_MSG = (
    "拒绝:受管 Desk 主会话不能直接写 journal，也不能自己提供日志摘要。"
    "需要立即留存时运行 python .claude/skills/journal/scripts/journal_append.py --request；"
    "它只设置请求，本回合收尾后由隔离 recap 支线生成并写入 [recap:<engine>]。"
)


def blocked(path):
    """路径是否落在受闸桶。"""
    return bool(MEM_BLOCK.search((path or "").replace("\\", "/")))


def session_journal_blocked(path):
    """受管主会话是否在直接写 journal；系统维护环境不带标记，仍可追加。"""
    return bool(os.environ.get("DESKTRON_RECAP_REQUEST_PATH")
                and JOURNAL_BLOCK.search((path or "").replace("\\", "/")))


def check_js_sync(js_source):
    """宿主自检:js 插件源码里是否内嵌了与当前规则等价的正则。漂移时返回 False。"""
    return PATTERN_JS in js_source


# ── adapter 归档回读闸 ────────────────────────────────────────────────────────
# 与记忆写闸同源同形:规则只写在这里,claude(Read/Bash)、codex/cursor 与 Pi 扩展
# 各自取用,换引擎不丢防护。
#
# 请求快照(request-contexts/live-request-context-*.txt)是某一次请求的完整模型输入。读它
# 会把那次输入连同本次读取记录一起带进下一次请求,逐轮自我放大;实测单会话 270 份、63MB。
# 会话上下文在进程拉起时已由 adapter 注入,回读不提供新信息,所以硬拒。
#
# 其余归档(工具结果/输入、回合 JSON)是正常的可检索历史,只拦"整份读回":超过阈值的全量
# 读取同样会二次进入上下文并被再次归档,定位式读取(grep / offset+limit)不受影响。
# 路径都限定在 adapter 目录下,避免误伤同名的业务目录。
SNAPSHOT_PATTERN = (r"(^|/)adapter/.*/request-contexts(/|$)"
                    r"|(^|/)live-request-context-[^/]*\.txt$")
SNAPSHOT_BLOCK = re.compile(SNAPSHOT_PATTERN)
SNAPSHOT_PATTERN_JS = (r"(^|\/)adapter\/.*\/request-contexts(\/|$)"
                       r"|(^|\/)live-request-context-[^\/]*\.txt$")

ARCHIVE_PATTERN = r"(^|/)adapter/.*/(tool-results|tool-inputs|rounds|messages)(/|$)"
ARCHIVE_BLOCK = re.compile(ARCHIVE_PATTERN)
ARCHIVE_PATTERN_JS = r"(^|\/)adapter\/.*\/(tool-results|tool-inputs|rounds|messages)(\/|$)"
# 整份读回的上限。小于它的归档一次读完不会明显放大上下文,放行。
ARCHIVE_BULK_BYTES = 32 * 1024

SNAPSHOT_MSG = (
    "拒绝:请求快照是某次请求的完整模型输入,不是任务状态文件。读它会把那次输入连同本次"
    "读取记录带进下一次请求,逐轮自我放大成读取循环。会话上下文已由 adapter 在进程拉起时"
    "注入,回读不提供新信息。恢复进度请查任务账本(desk-task list)、代码 diff 与测试结果;"
    "确需历史细节时检索 history.md 或单条工具结果。"
)

ARCHIVE_MSG = (
    "拒绝:整份读回 adapter 归档 {path}({size} 字节)会二次进入上下文并被再次归档。"
    "请用 grep 定位关键行,或带 offset/limit 读取指定区间。"
)


def snapshot_blocked(path):
    """路径是否为请求快照(硬拒回读)。"""
    return bool(SNAPSHOT_BLOCK.search((path or "").replace("\\", "/")))


def archive_bulk_blocked(path, size=None):
    """是否为"整份读回超阈值 adapter 归档"。size 为 None 时自行 stat;取不到大小就放行。"""
    if not ARCHIVE_BLOCK.search((path or "").replace("\\", "/")):
        return False
    if size is None:
        try:
            size = os.path.getsize(path)
        except Exception:
            return False
    return size > ARCHIVE_BULK_BYTES


def archive_size(path):
    """给拒绝文案用的大小;取不到返回 0。"""
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def js_literal(pattern):
    """规则写进 js 的 new RegExp("...") 字符串字面量时的形态(反斜杠再转义一层)。"""
    return pattern.replace("\\", "\\\\")


def check_js_snapshot_sync(js_source):
    """宿主自检:js 插件是否内嵌了与当前归档闸等价的正则。漂移时返回 False。"""
    return (js_literal(SNAPSHOT_PATTERN_JS) in js_source
            and js_literal(ARCHIVE_PATTERN_JS) in js_source)


# 宿主传进来的往往是 shell 操作数或工具参数,不是规范路径:可能带 ~、相对路径、通配符,
# 或是指向归档目录的符号链接。判定前统一在这里展开,各闸只调下面三个 *_target 函数,
# 不各自实现一遍展开逻辑(此前 `cat alias/*.txt` 与 `cat live-request-context-*` 正是从
# 缺少展开的缝里漏过去的)。
_GLOB_CHARS = re.compile(r"[*?\[]")
_SNAPSHOT_PREFIX = "live-request-context-"
_MAX_GLOB = 64


def read_targets(value, cwd=""):
    """把一个读操作数展开成待判定的真实路径集合。"""
    if not value:
        return []
    raw = os.path.expanduser(value)
    if not os.path.isabs(raw) and cwd:
        raw = os.path.join(cwd, raw)
    if _GLOB_CHARS.search(raw):
        hits = sorted(_glob.glob(raw))[:_MAX_GLOB]
        # 展开不出文件(目录不存在或已清理)时,仍按去掉通配符的前缀判定一次,
        # 避免"文件暂时不在"就把规则整个跳过。
        paths = hits or [_GLOB_CHARS.split(raw)[0]]
    else:
        paths = [raw]
    out = []
    for path in paths:
        ap = os.path.abspath(path)
        out.append(ap)
        try:
            real = os.path.realpath(ap)
        except Exception:
            continue
        if real != ap:
            out.append(real)
    return out


def snapshot_blocked_target(value, cwd=""):
    """shell 操作数 / 工具参数是否指向请求快照。"""
    for path in read_targets(value, cwd):
        if snapshot_blocked(path):
            return True
        if os.path.basename(path).startswith(_SNAPSHOT_PREFIX):   # `live-request-context-*` 这类裸通配
            return True
    return False


def archive_bulk_blocked_target(value, cwd=""):
    """是否为整份读回超阈值的 adapter 归档;返回命中的(路径, 大小),未命中返回 None。"""
    for path in read_targets(value, cwd):
        if archive_bulk_blocked(path):
            return path, archive_size(path)
    return None
