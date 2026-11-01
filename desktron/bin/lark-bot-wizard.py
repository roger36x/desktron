#!/usr/bin/env python3
"""飞书 bot 接入向导:替人记住顺序,并在每一步之后当场验一次。

不替人点开放平台——建应用、勾权限、改订阅方式、发版都由本人在浏览器里做,这里只做三件事:
把对的页面打开、说清楚这一页要动哪一项、动完立刻验一次。

价值在第三件。这套配置一共六步,错任何一步的表现都是同一个:「bot 不回话」。
而六步分散在四个页面上,人配完通常几分钟后才在飞书里试,那时已经离配置很远了。
向导把验证紧挨着操作放:填完 app_id 当场验它存不存在、填完 secret 当场取一次 token、
最后真的把长连接拉起来看能不能连上——连上了才说明订阅方式那一步也对了。

事件订阅方式没有公开接口可查,所以最后那一步不是"检查配置",是"真的连一次"。
这也是唯一可信的验法。

三种模式:
  默认        六步引导,你在浏览器里做,它盯着顺序并当场验。
  --auto      扫一次码,前四步替你做完(lark_app_auto.py),再走后两步。断了自动降级回引导——
              自动那条走的是开发者后台的内部接口,后台改版就断,所以引导那条始终留着。
  --have-app  应用已经建好了。跳过那四步,只填 App ID 与 Secret,再真连一次验完。

Desk 界面上的两个按钮就是后两种。配置这类事一律走终端,不走网页弹窗:看得见前后文、
每步当场验、失败能读输出——浏览器的 prompt() 一次只看得见一问,2026-08-25 就这么把
App Secret 填进了 App ID 那一栏。

--backend 决定声明与凭证落到哪一侧(desktop / server)。向导本身始终在人面前这台机器上跑。

用法:desktron/bin/lark-bot-wizard.py [--auto|--have-app] [--backend server]
                                        [--domain feishu|larksuite] [--port 7765]
"""
import json
import atexit
import getpass
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# 只为让 input() 有行编辑:不导入的话方向键与 Ctrl-A/E 会打出 ^[[D 这样的转义序列
# (2026-08-26 用户在向导终端里实测)。导入本身就生效,不需要调它任何函数。
# 精简版 python 可能没编这个模块,缺了只是退化回没有行编辑,不该拦向导。
try:
    import readline  # noqa: F401
except ImportError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

HOSTS = {
    "feishu": "https://open.feishu.cn",
    "larksuite": "https://open.larksuite.com",
}

#: 要勾的权限。少而具体是有意的:权限表上百项,多勾的每一项都要在发版时被人多审一遍。
SCOPES = [
    ("im:message", "接收消息"),
    ("im:message:send_as_bot", "以机器人身份发消息"),
    # 权限表里的可申请名。公开文档管这个能力叫 im:chat:readonly,但表里搜不到那个名
    # (2026-08-26 实测)——按这里写的搜;搜不到再试 im:chat:readonly、im:chat。
    ("im:chat:read", "读群信息(判断是不是话题群要用)"),
    ("im:resource", "读消息里的图片与文件"),
]
EVENT = "im.message.receive_v1"


def arg(name, default=""):
    a = sys.argv
    return a[a.index("--" + name) + 1] if ("--" + name) in a and a.index("--" + name) + 1 < len(a) else default


BRAND = arg("domain", "feishu")
BASE = HOSTS.get(BRAND, HOSTS["feishu"])
PORT = arg("port", os.environ.get("DESKTRON_UI_PORT", "7765"))
DESK = f"http://127.0.0.1:{PORT}"

#: bot 建到哪一侧(desktop / server)。空 = 本机。
#:
#: 向导本身始终在人面前这台机器上跑,即使目标是服务器。这样是对的:建应用那几步是往
#: 飞书发 HTTPS,在哪台机器上发都一样;二维码是画在终端里的 ASCII,用手机扫,与终端属于
#: 哪台机器无关。真正必须落在目标那一侧的只有声明与凭证,而它们都经 Desk 的接口写——
#: 给那些调用挂上 ?backend= 就够了。
BACKEND = arg("backend", "")

JOB = arg("onboarding")
GENERATION = int(arg("generation", "0"))
_saved = {}
if JOB:
    from larkbot import onboarding
    _saved = onboarding.get(JOB)
    if _saved["generation"] != GENERATION or _saved["phase"] == "cancelled":
        sys.exit("接入已由另一轮接管")


def progress(**fields):
    if JOB:
        onboarding.checkpoint(JOB, GENERATION, **fields)
        _saved.update(fields)


def _unfinished():
    if JOB and _saved.get("phase") not in ("complete", "cancelled", "needs_attention"):
        try:
            progress(phase="interrupted")
        except Exception:
            pass


atexit.register(_unfinished)

C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "0": "\033[0m"}
if not sys.stdout.isatty():
    C = dict.fromkeys(C, "")


def say(s=""):
    print(s)


def step(n, total, title):
    say(f"\n{C['b']}[{n}/{total}] {title}{C['0']}")


def ok(s):
    say(f"  {C['g']}✓{C['0']} {s}")


def warn(s):
    say(f"  {C['y']}!{C['0']} {s}")


def fail(s):
    say(f"  {C['r']}✗{C['0']} {s}")


def ask(q, default=""):
    try:
        v = input(f"  {q}" + (f"（默认 {default}）" if default else "") + ": ").strip()
    except (EOFError, KeyboardInterrupt):
        say("\n已取消。")
        sys.exit(130)
    return v or default


def pause(q="做完了按回车继续"):
    try:
        input(f"  {C['d']}{q}{C['0']} ")
    except (EOFError, KeyboardInterrupt):
        say("\n已取消。")
        sys.exit(130)


def browse(url):
    """打开浏览器。打不开不算错——远程终端、无桌面环境都很常见,把地址印出来就行。"""
    say(f"  {C['d']}{url}{C['0']}")
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.run([opener, url], check=False, timeout=8,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def yes(q, default="y"):
    return ask(q + " (y/n)", default).lower().startswith("y")


def pick_app(apps, total):
    """把名下的应用列出来让人挑。→ 选中的那个,或 None(放弃)。

    序号与 App ID 都认:列表长了翻着找序号反而麻烦,而人手上常常已经有那串 ID。
    「未开机器人能力」单独标出来——它解释了一类困惑:没开的话 im:* 那几项在权限表里
    根本不可选,应用「看着好好的却配不上」。挑了也没关系,下一步就会替它开。
    """
    if _saved.get("app_id"):
        found = next((a for a in apps if a["app_id"] == _saved["app_id"]), None)
        if not found:
            raise RuntimeError("未找到此前的应用，请确认登录账号；不会另建应用")
        return found
    say("")
    for i, a in enumerate(apps, 1):
        flag = "" if a["has_bot"] else f"　{C['y']}未开机器人能力{C['0']}"
        say(f"    {C['b']}{i:>2}{C['0']}. {a['name']}　{C['d']}{a['app_id']}{C['0']}{flag}")
        if a.get("desc"):
            say(f"        {C['d']}{a['desc'][:60]}{C['0']}")
    while True:
        v = ask(f"挑一个（填序号 1-{len(apps)}，或直接填 App ID；留空放弃）")
        if not v:
            return None
        if v.isdigit() and 1 <= int(v) <= len(apps):
            return apps[int(v) - 1]
        for a in apps:
            if a["app_id"] == v:
                return a
        fail("没有这一个,照上面的序号再挑一次")


def slug(s):
    """从应用名推一个代号。推不出来(中文名、太短)就返回空,让人自己填——
    硬凑一个 `app-1` 之类的默认值,等他有三个 bot 时就分不清谁是谁了。"""
    v = re.sub(r"[^a-z0-9_-]+", "-", str(s or "").lower()).strip("-")
    return v if re.match(r"^[a-z0-9][a-z0-9_-]{1,39}$", v) else ""


def desk(path, body=None, method="POST"):
    """调本机 Desk。→ (数据, 错误说明)。

    带 --backend 时,larkbot 那几个接口挂上 ?backend=,由本机 Desk 带票转到那一侧去
    (lib/larkbot/peers.py)。挂在这里而不是各个调用点:调用点写成 f-string 之后,
    「向导调的接口都被认领了吗」那条测试就抠不出路径了,而那条测试比这点整洁更值钱。
    """
    at = f"?backend={urllib.parse.quote(BACKEND)}" \
        if BACKEND and path.startswith("/workbench/lark-bot/") else ""
    url = DESK + path + at
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    tok = os.environ.get("DESKTRON_REMOTE_TOKEN", "").strip()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode() or "{}")
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"连不上本机 Desk({DESK}):{str(e)[:120]}"
    if isinstance(d, dict) and d.get("ok") is False:
        return d, d.get("error") or d.get("detail") or "失败"
    return d, ""


# ---------------------------------------------------------------- 主流程

#: 应用已经在开放平台建好了,只差把它交给 Desk。跳过建应用那四步,直接进凭证与验连接。
#: 这条路以前在界面上走浏览器的 prompt() 连问三次——一次只看得见一问、没有上下文、
#: 填完没有校验反馈,2026-08-25 就这么把 App Secret 填进了 App ID 那一栏。
#: 配置这类事统一走终端:看得见前后文、每步当场验、失败能读输出。
HAVE = "--have-app" in sys.argv or bool(_saved.get("app_id") or _saved.get("create_requested"))
if HAVE and "--auto" in sys.argv:
    say("继续此前的接入。应用可能已创建，先核对已有应用，不会重复新建。")

#: 步数按走哪条路算。已有应用时只剩两步,写「第 5 步(共 6 步)」会让人以为漏了四步。
TOTAL = 2 if HAVE else 6
S_CRED, S_CONN = (1, 2) if HAVE else (5, 6)

say(f"{C['b']}飞书 bot 接入向导{C['0']}")
say(f"站点 {BRAND}（{BASE}）　Desk {DESK}"
    + (f"　建到 {C['b']}{BACKEND}{C['0']}" if BACKEND else ""))
say(f"{C['d']}" + ("已有应用,两步:扫码挑一个应用并把凭证交给 Desk、把长连接拉起来验一次。"
                   if HAVE else
                   "六步。每一步动完当场验一次——这套配置错哪一步都表现为「bot 不回话」,"
                   "分开验才知道错在哪。") + f"{C['0']}")

d, err = desk("/workbench/lark-bot/deps", method="GET")
if err:
    fail(f"Desk 没应答:{err}")
    say("  先确认 Desk 在跑,或用 --port 指对端口。")
    sys.exit(1)
if not (d or {}).get("ok"):
    warn(f"长连接依赖还没装:{(d or {}).get('detail', '')}")
    if ask("现在安装吗?(y/n)", "y").lower().startswith("y"):
        say("  装依赖中(npm install,可能要一两分钟)…")
        d2, e2 = desk("/workbench/lark-bot/deps", {})
        if e2 or not (d2 or {}).get("ok"):
            fail(f"没装上:{e2 or (d2 or {}).get('detail')}")
            say(f"  手动:cd ~/.local/share/desktron/larkbot && npm install")
            sys.exit(1)
        ok("依赖就绪")

AUTO = "--auto" in sys.argv and not HAVE

# ---- 自动:扫一次码,前四步替你做完 ----
# 断了不是终点:降级回下面那条六步引导。自动那条走的是开发者后台的内部接口,
# 后台改版就会断——所以引导那条始终留着,而不是把它删掉换成这个。
auto_done = None
adopted = None
half = ""
if AUTO:
    say(f"\n{C['b']}自动模式{C['0']}　扫一次码,建应用 / 权限 / 事件 / 发版四步替你做完。")
    say(f"{C['d']}用的是开发者后台的内部接口(不是公开 API),断了会自动退回六步引导。{C['0']}")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import lark_app_auto as _auto
        name0 = ask("这个 bot 在飞书里叫什么", "Desk 助手")
        desc0 = ask("一句话说明", "由 Desk 托管的工作助手")
        progress(name=name0, phase="authorizing")
        auto_done = _auto.run(
            BRAND, name0, desc0,
            on_qr=lambda qr: say("\n请用飞书 App 扫码（登录你自己的账号）：\n\n" + qr),
            on_status=lambda m: say(f"  {C['d']}{m}{C['0']}"),
            on_create=lambda: progress(create_requested=True, phase="creating"),
            on_app=lambda aid, name: progress(app_id=aid, name=name, phase="configuring"))
        ok(f"应用已就绪:{auto_done['app_id']}（版本 {auto_done['version']}）")
        if auto_done.get("missing"):
            warn(f"这几项权限没找到,去后台手动勾:{'、'.join(auto_done['missing'])}")
    except Exception as e:
        half = getattr(e, "app_id", "")
        where = getattr(e, "step", "")
        fail(f"自动没走通{f'（卡在{where}）' if where else ''}：{str(e)[:200]}")
        if half:
            progress(app_id=half, phase="configuring")
            # 应用已经建出来了。不说清楚的话,开放平台上会躺着一个半配好的应用,
            # 而人以为什么都没发生,下次又建一个。
            warn(f"应用 {half} 已经建好了,下面接着配它,不必重建。")
            say(f"  {C['d']}{BASE}/app/{half}{C['0']}")
        say(f"  {C['d']}退回六步引导。{C['0']}")
        if JOB and _saved.get("create_requested") and not _saved.get("app_id"):
            # 创建请求可能已到飞书，不能把未知结果当成失败再创建一次。
            HAVE = True

if auto_done:
    app_id = auto_done["app_id"]      # 前四步已完成,直接进凭证与拉起连接
elif HAVE:
    app_id = ""
    say(f"\n{C['b']}已有应用{C['0']}　扫一次码,把你名下的应用列出来挑一个。")
    say(f"{C['d']}挑完它顺手把该配的配好(机器人能力、四项权限、长连接订阅),再把 Secret"
        f"直接读出来——不用去后台抄那两串,也就不会填反。{C['0']}")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import lark_app_auto as _auto
        adopted = _auto.adopt(
            BRAND, choose=pick_app, confirm=yes,
            on_qr=lambda qr: say("\n请用飞书 App 扫码（登录你自己的账号）：\n\n" + qr),
            on_status=lambda m: say(f"  {C['d']}{m}{C['0']}"),
            on_app=lambda aid, name: progress(app_id=aid, name=name, phase="configuring"))
        if adopted is None:
            say("\n已取消,什么都没动。")
            sys.exit(0)
        app_id = adopted["app_id"]
        ok(f"选定「{adopted['name']}」{app_id}")
        if adopted.get("missing"):
            warn(f"这几项权限没找到,去后台手动勾:{'、'.join(adopted['missing'])}")
    except SystemExit:
        raise
    except Exception as e:
        where = getattr(e, "step", "")
        fail(f"扫码这条没走通{f'（卡在{where}）' if where else ''}：{str(e)[:200]}")
        say(f"  {C['d']}退回手填 App ID 与 Secret。{C['0']}")

    if not app_id:
        app_id = _saved.get("app_id") or ""
        # 手填那条。把「那个应用必须已经是什么样」摆出来让人自己核对——四件里缺任何
        # 一件,表现都是同一个「bot 不回话」,而那时人已经离配置很远了。
        say(f"\n{C['d']}前提是那个应用已经配好了这四件:{C['0']}")
        say(f"    · 「添加应用能力」里加了{C['b']}机器人{C['0']}")
        say(f"    · 权限勾了:{'、'.join(s for s, _ in SCOPES)}")
        say(f"    · 事件订阅方式是{C['b']}长连接{C['0']},订了 {EVENT}")
        say(f"    · 版本已创建并发布{C['d']}（企业内可能要管理员审批）{C['0']}")
        say(f"  {C['d']}不确定就去看一眼:{BASE}/app{C['0']}")
        say(f"  {C['d']}缺哪件也不必现在补齐——最后一步会真连一次,连不上会说往哪儿看。{C['0']}")
        while not (app_id.startswith("cli_") and len(app_id) > 8):
            app_id = ask("App ID（cli_ 开头，在「凭证与基础信息」页）")
            if app_id.startswith("cli_") and len(app_id) > 8:
                break
            # 这句点名 Secret:同一页上挨着的两串,填反过(2026-08-25)。
            fail("不像 App ID，应该是 cli_ 开头的一串。App Secret 是同一页上的另一串，等下单独问")
else:
    # ---- 1 建应用
    step(1, TOTAL, "建一个企业自建应用")
    say("  在打开的页面点「创建企业自建应用」,填个名字和说明。")
    say(f"  {C['d']}它会建在你当前登录的那个企业下,建完不能搬,注意别用错账号。{C['0']}")
    app_id = _saved.get("app_id") or half
    if not app_id:
        browse(f"{BASE}/app")
        pause()
    else:
        say(f"  继续配置已创建的应用 {app_id}，不必新建。")
    while not (app_id.startswith("cli_") and len(app_id) > 8):
        app_id = ask("把 App ID 粘过来（cli_ 开头，在「凭证与基础信息」页）")
        if app_id.startswith("cli_") and len(app_id) > 8:
            break
        fail("不像 App ID，应该是 cli_ 开头的一串")

    # ---- 2 权限
    step(2, TOTAL, "勾四项权限")
    say("  在「权限管理」里搜这四个,逐个开通:")
    for s, why in SCOPES:
        say(f"    {C['b']}{s}{C['0']}　{C['d']}{why}{C['0']}")
    say(f"  {C['d']}搜不到 im:* 的话,先去「添加应用能力」把「机器人」加上——没开机器人能力,"
        f"这几项在权限表里是不可选的。{C['0']}")
    browse(f"{BASE}/app/{app_id}/auth")
    pause()

    # ---- 3 事件订阅
    step(3, TOTAL, "订阅方式改成长连接，订阅一个事件")
    say(f"  在「事件与回调」→「事件配置」里:")
    say(f"    · 订阅方式选 {C['b']}长连接{C['0']}"
        f"　{C['d']}默认是 webhook,而 Desk 可能跑在内网,飞书回调不进来{C['0']}")
    say(f"    · 添加事件 {C['b']}{EVENT}{C['0']}　{C['d']}接收消息{C['0']}")
    browse(f"{BASE}/app/{app_id}/dev-config")
    pause()

    # ---- 4 发版
    step(4, TOTAL, "创建版本并发布")
    say("  左侧「版本管理与发布」→ 创建版本 → 申请发布。")
    say(f"  {C['d']}企业内可能要管理员审批。没通过的话权限不生效,后面那一步会连不上。{C['0']}")
    browse(f"{BASE}/app/{app_id}/baseinfo")
    pause()

progress(app_id=app_id, phase="configuring")
# ---- 5 凭证进 Desk
step(S_CRED, TOTAL, "把凭证交给 Desk")
say(f"  App Secret 在「凭证与基础信息」页,与 App ID 同一页。")
say(f"  {C['d']}它只会写进 ~/.config/desktron/env,不进仓库、不进 .workbench——"
    f"那两处会随工作区被打包同步。{C['0']}")

# 扫码那两条已经知道应用叫什么,拿它当默认值:代号与显示名是最后还需要人给的东西,
# 能带出来的就带出来。中文名推不出合法代号时 slug() 返回空,那时照常问。
_app_name = (adopted or auto_done or {}).get("name") or ""
_id_default = _saved.get("agent_id") or slug(_app_name) or "my-bot"

bot_id = ""
while True:
    # 叫「代号」不叫「id」:飞书那边的 App ID 也是个 id,两处都叫 id 时容易填反。
    bot_id = ask("给这个 bot 起个代号（你自己起的短名；小写字母、数字与 _-，2 到 40 位）",
                 _id_default).lower()
    if bot_id.startswith("cli_"):
        fail("这看着是飞书的 App ID。代号是你自己起的短名，App ID 在后面单独填")
        continue
    if re.match(r"^[a-z0-9][a-z0-9_-]{1,39}$", bot_id):
        break
    fail("代号只能是小写字母、数字与 _-，2 到 40 位")
bot_name = ask("显示名称", _app_name or bot_id)

# 不问工作流:记忆与知识是全工作台检索的,bot 不属于任何一个工作流。
# 要问的是工作目录——它决定 bot 脚下能看见什么(代码仓与工作目录里的资料),
# 是「它看得见什么」里剩下的那一项。留空 = 工作台根,多数情况够用。
cwd = ask("工作目录（留空则为工作台根目录）").strip()
cwd = os.path.expanduser(cwd) if cwd else ""
if cwd and BACKEND:
    # 路径要在 BACKEND 那台机器上存在,这里校验的是本机的文件系统,校了也不作数。
    say(f"  {C['d']}目录在 {BACKEND} 上,这里不校验{C['0']}")
elif cwd and not os.path.isdir(cwd):
    fail(f"目录不存在:{cwd}。先留空,之后在 Desk 里改或在飞书里 /cd")
    cwd = ""
if cwd:
    ok(f"在 {cwd} 里干活")

# 用哪个引擎。此前不问,建出来一律是默认的 claude,人得事后去面板里改——而这是
# 建的时候最该定的一项:它决定这个 bot 到底由谁来答。
#
# 只列目标那一侧真的装了的:装没装是按机器算的,
# 而选一个没装的,表现是「bot 收到消息但不回话」,与配错权限的现象一模一样。
eng_default, eng_ok, eng_all = "claude", [], []
d, err = desk("/workbench/lark-bot/engines", method="GET")
for e in ((d or {}).get("items") or []):
    eng_all.append(e.get("name"))
    if e.get("ok"):
        eng_ok.append(e.get("name"))
if eng_ok:
    if eng_default not in eng_ok:
        eng_default = eng_ok[0]
    say("  可用的引擎:")
    for i, n in enumerate(eng_all, 1):
        mark = "" if n in eng_ok else f"　{C['y']}未安装{C['0']}"
        say(f"    {C['b']}{i:>2}{C['0']}. {n}{mark}")
else:
    # 一个都没探到时不拦:可能是接口老版本没有这一条,不该因此把向导堵死。
    warn("探不到已安装的引擎,按默认继续;之后可在配置面板里改,或在飞书里用 /engine 改")

while True:
    v = ask(f"引擎（序号或名字，默认 {eng_default}）", eng_default).lower()
    if v.isdigit() and 1 <= int(v) <= len(eng_all):
        engine = eng_all[int(v) - 1]
        break
    if not eng_all or v in eng_all:
        engine = v
        break
    fail(f"没有叫 {v} 的引擎,照上面的序号再选一次")
if eng_ok and engine not in eng_ok:
    warn(f"{engine} 在{BACKEND or '本机'}上尚未安装——收到消息也无法回答,请先安装")

# 用哪个模型。清单现问引擎(在目标那一侧问:两边装的版本、开的权限都可能不一样),
# 列出来按序号挑,免得抄错名字——模型名抄错的表现是引擎报参数错,一条消息都答不了。
# 列不出来(引擎没装、超时、老版本没这个接口)就退回手填,不拦路。
model = ""
d, _e = desk(f"/workbench/lark-bot/engines/{engine}/models", method="GET")
_models = (d or {}).get("items") or []
if _models:
    say(f"  {engine} 可用的模型:")
    for i, m in enumerate(_models[:20], 1):
        say(f"    {C['b']}{i:>2}{C['0']}. {m}")
    if len(_models) > 20:
        say(f"    {C['d']}…另有 {len(_models) - 20} 个未列出,直接填名字同样有效{C['0']}")
    v = ask("模型（序号或名字；留空则用引擎默认）")
    if v.isdigit() and 1 <= int(v) <= min(len(_models), 20):
        model = _models[int(v) - 1]
    elif v:
        model = v
        if v not in _models:
            warn(f"{v} 不在清单里,按原样保存——名字有误时引擎会报错,那句报错会回到飞书里")
else:
    if (d or {}).get("error"):
        say(f"  {C['d']}列不出模型清单({(d or {}).get('error')}),留空用引擎默认{C['0']}")
    v = ask("模型（留空则用引擎默认）")
    model = v
if model:
    ok(f"用 {model}")

# 先核对现有声明；响应丢失后的重试不能覆盖同代号的另一只 Bot。
existing, lookup_error = desk("/workbench/lark-bot/agents", method="GET")
if lookup_error:
    fail("无法核对已有 Bot，进度已保留，请稍后继续。")
    sys.exit(1)
owned = [a for a in (existing or {}).get("items", [])
         if not a.get("backend") or a.get("backend") == (existing or {}).get("backend")]
same_id = next((a for a in owned if a.get("id") == bot_id), None)
same_app = next((a for a in owned if a.get("lark_app_id") == app_id), None)
if (same_id and same_id.get("lark_app_id") != app_id) or (same_app and same_app.get("id") != bot_id):
    fail("代号已被其他应用使用，或该应用已接入另一只 Bot。请在已有 Bot 配置中处理；不会覆盖。")
    progress(phase="needs_attention")
    sys.exit(1)
progress(agent_id=bot_id)
if same_id:
    d, err = {"agent": same_id}, ""
else:
    d, err = desk("/workbench/lark-bot/agents",
              {"id": bot_id, "name": bot_name, "lark_app_id": app_id,
               "domain": BRAND, "cwd": cwd, "engine": engine, "model": model})
if err:
    fail(f"建不了:{err}")
    sys.exit(1)
ok(f"已登记 {bot_name}")

def put_secret(sec):
    """存下并当场验一次。→ (成不成, 飞书那侧的原话)"""
    d, err = desk(f"/workbench/lark-bot/agents/{bot_id}/secret", {"secret": sec})
    return bool(not err and (d or {}).get("ok")), ((d or {}).get("detail") or err)


# 扫码那两条路已经把 Secret 从开发者后台读出来了,不该再让人去抄一遍——手抄正是
# 「把 Secret 填进 App ID 那一栏」的来源,而且它一次都没显示给人看过,抄无可抄。
_known = (adopted or auto_done or {}).get("secret") or ""
good = False
if _known:
    say(f"  {C['d']}Secret 已从开发者后台读出,不必手抄。{C['0']}")
    good, detail = put_secret(_known)
    if good:
        ok(f"凭证可用：{detail}")
    else:
        fail(f"验不过：{detail}")
        say(f"  凭证已存下,但连不通。多半是版本还没发布/没审批通过。")
        say(f"  改完之后回 Agents 页点「自检」重试,不必重跑向导。")
else:
    for attempt in range(3):
        secret = getpass.getpass("  App Secret（输入不回显）: ").strip()
        if not secret:
            continue
        good, detail = put_secret(secret)
        if good:
            ok(f"凭证可用：{detail}")
            break
        # 这里报的是飞书那侧的原话。凭证不对与应用没发版是两种错,提示不该混成一句。
        fail(f"验不过：{detail}")
        if attempt == 2:
            say(f"  凭证已存下,但连不通。常见原因:版本还没发布/没审批通过。")
            say(f"  改完之后回 Agents 页点「自检」重试,不必重跑向导。")
        else:
            say(f"  {C['d']}Secret 粘错了就重填;若确认没错,多半是没发布。{C['0']}")

if not good:
    progress(phase="needs_attention")
    fail("Bot 已登记，但凭证未通过检查。请从 Bot 配置页处理后再启动，不必重新创建应用。")
    sys.exit(1)

# ---- 6 真的连一次
progress(phase="connecting")
step(S_CONN, TOTAL, "把长连接拉起来看看")
say(f"  {C['d']}事件订阅方式没有公开接口可查,所以这一步不是检查配置,是真的连一次——"
    f"连上了才说明第 3 步也对了。{C['0']}")
d, err = desk(f"/workbench/lark-bot/agents/{bot_id}/start", {})
pid = (d or {}).get("pid") or 0
if pid:
    # PID 只证明进程启动；不能把它当作已连上飞书。
    progress(phase="complete")
    ok(f"Bot 已登记，长连接进程已启动（pid {pid}）；实际连接状态请在 Bot 页查看。")
    say(f"\n{C['g']}{C['b']}好了。{C['0']}去飞书里私聊「{bot_name}」试一句。")
    say(f"  拉进群要 @ 它才回；开话题群则进话题即触发。")
    say(f"  改配置:群里发 /help,或回代理的飞书 Bot 页。")
else:
    progress(phase="needs_attention")
    fail(f"没连上：{err or (d or {}).get('error') or '原因未知'}")
    say(f"\n  按可能性排:")
    say(f"    1. 第 3 步的订阅方式没改成长连接（最常见）")
    say(f"    2. 第 4 步的版本没发布,或还在等管理员审批")
    say(f"    3. 第 2 步的权限没勾全")
    say(f"  改完回 Agents 页点「启动」,旁边「日志」能看到飞书那侧的原话。")
    sys.exit(1)
