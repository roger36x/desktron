#!/usr/bin/env python3
"""在开发者后台替人把飞书应用建出来:扫码 → 建应用 → 开机器人能力 → 配权限 →
事件订阅改长连接 → 订阅事件 → 发版 → 读凭证。

用户本人扫码登录自己的飞书账号,在自己所在的企业里建一个自己的应用。做的是他打开
浏览器手动会做的同一件事,一步不多。会话只在这个进程的内存里,不落盘、不外发;
凭证读出来后直接交给本机 Desk 写进 ~/.config/desktron/env。

**为什么要自动**:手动那六步分散在四个页面上,错任何一步的表现都是同一个「bot 不
回话」。而人配完通常几分钟后才在飞书里试,那时已经离配置很远了。

**代价说在前面**:用的是开发者后台前端自己在调的 `/developers/v1/*`,不是承诺兼容
的公开 API(做法取自 botmux,MIT)。后台改版这条路就断。所以每一步失败都带着
「做到哪儿了」抛出,调用方退回引导式向导(lark-bot-wizard.py)——自动化是纯增益,
断了回到原来的样子,已经建好的应用不受影响。

被调用:lark-bot-wizard.py --auto。也可以单独跑,结果打印成 JSON(不含凭证)。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOSTS = {
    "feishu": ("https://accounts.feishu.cn", "https://open.feishu.cn"),
    "larksuite": ("https://accounts.larksuite.com", "https://open.larksuite.com"),
}

#: 只勾这四项。权限表上百项,多勾的每一项都要在发版时被人多审一遍。
#: 要勾的权限。每项是(候选名元组, 用途):console 权限表里的可申请名与公开文档
#: 不完全一致——读群信息在公开文档叫 im:chat:readonly,表里搜不到(2026-08-26 实测,
#: botmux 校验的也是 im:chat:read 这个名)。候选按序找,勾到第一个存在的;
#: 全不存在才算 missing。
SCOPES = [
    (("im:message",), "接收消息"),
    (("im:message:send_as_bot",), "以机器人身份发消息"),
    (("im:chat:read", "im:chat:readonly", "im:chat"), "读群信息(判话题群)"),
    (("im:resource",), "读消息里的图片与文件"),
]
EVENTS = ["im.message.receive_v1"]
EVENT_MODE_WS = 4          # 4 = 长连接。默认是 webhook,而 Desk 可能跑在内网。

#: accounts 那侧要求的固定头。x-app-id=12 是开发者后台自己的标识。
COMMON = {
    "x-api-version": "1.0.28",
    "x-locale": "zh-CN",
    "x-terminal-type": "2",
    "x-app-id": "12",
    "x-device-info": ("device_id=0;device_name=Chrome;device_os=Mac;device_model=Chrome;"
                      "lark_version=;channel=Release;package_name=feishu;tt_app_id=1658;"
                      "is_dpop_support=true;is_iframe=false"),
    "accept": "application/json",
    "content-type": "application/json",
}

QR_WAIT_S = 120


class Failed(Exception):
    """带着「做到哪一步了」的失败。app_id 非空表示应用已经建出来了——那时不能只说
    「失败」,得让人知道去哪儿接着配,否则开放平台上会躺着一个半配好的应用,
    而人以为什么都没发生。"""

    def __init__(self, msg, app_id="", step=""):
        super().__init__(msg)
        self.app_id = app_id
        self.step = step


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Session:
    """带 cookie 的 HTTP 会话。标准库不管 cookie,而登录那一跳靠 302 链把 cookie
    从 accounts 域带到 open 域,必须自己跟、自己收。"""

    def __init__(self):
        self.jar = {}

    def _absorb(self, resp):
        for v in (resp.headers.get_all("Set-Cookie") or []):
            pair = v.split(";", 1)[0]
            if "=" in pair:
                k, _, val = pair.partition("=")
                self.jar[k.strip()] = val.strip()

    def req(self, url, method="GET", body=None, headers=None, depth=0):
        """→ (状态码, 响应头, 正文)。手动跟跳转:自动跟随会丢掉中间几跳的 Set-Cookie。"""
        if depth > 8:
            raise Failed(f"重定向次数过多:{url}")
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if self.jar:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=30) as r:
                self._absorb(r)
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self._absorb(e)
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                return self.req(urllib.parse.urljoin(url, e.headers["Location"]),
                                "GET", None, headers, depth + 1)
            return e.code, e.headers, e.read().decode("utf-8", "replace")
        except Failed:
            raise
        except Exception as e:
            raise Failed(f"请求失败({url}):{str(e)[:150]}")

    def json_post(self, url, body, headers):
        code, hdrs, text = self.req(url, "POST", body, headers)
        try:
            return json.loads(text or "{}"), hdrs
        except ValueError:
            raise Failed(f"返回的不是 JSON(HTTP {code}):{text[:150]}")

    def form_post(self, url, fields, files, headers):
        """multipart/form-data。标准库没有现成的,手拼——只给图标上传用,够了。
        files: [(字段名, 文件名, 内容 bytes, mime)]"""
        bound = "----deskform" + os.urandom(12).hex()
        buf = []
        for k, v in fields.items():
            buf.append(f"--{bound}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, fn, blob, mime in files:
            buf.append(f"--{bound}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                       f"filename=\"{fn}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + blob + b"\r\n")
        buf.append(f"--{bound}--\r\n".encode())
        data = b"".join(buf)
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={bound}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if self.jar:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._absorb(r)
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except Exception as e:
            raise Failed(f"上传失败:{str(e)[:150]}")


def _nonce():
    ms = int(time.time() * 1000)
    return f"?_r{ms % 90000 + 10000}={ms}"


def render_qr(payload):
    """把二维码画到终端。借 larkbot 目录下的 node + qrcode-terminal——QR 编码自己
    实现要几百行,而那个目录本来就有 node_modules(长连接 SDK 在那儿)。
    画不出来返回空串,调用方退回引导式向导,不是硬失败。"""
    home = os.environ.get("DESKTRON_LARKBOT_HOME") or os.path.expanduser(
        "~/.local/share/desktron/larkbot")
    js = ("const q=require('qrcode-terminal');"
          "q.generate(process.argv[1],{small:true},s=>process.stdout.write(s));")
    try:
        r = subprocess.run(["node", "-e", js, payload], cwd=home,
                           capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else ""
    except Exception:
        return ""


def login(brand, on_qr=None, on_status=None):
    """扫码登录,拿到开发者后台的会话。→ Session"""
    accounts, open_host = HOSTS.get(brand, HOSTS["feishu"])
    s = Session()

    data, hdrs = s.json_post(f"{accounts}/accounts/qrlogin/init{_nonce()}",
                             {"biz_type": None, "redirect_uri": f"{open_host}/app"},
                             COMMON)
    if data.get("code") not in (0, None):
        raise Failed(f"取二维码失败:code={data.get('code')} {data.get('message', '')}")
    token = ((data.get("data") or {}).get("step_info") or {}).get("token")
    flow_key = hdrs.get("x-flow-key")
    if not token or not flow_key:
        raise Failed("取二维码失败:没拿到 token 或 flow key")

    payload = json.dumps({"qrlogin": {"token": token}}, ensure_ascii=False)
    qr = render_qr(payload)
    if not qr:
        raise Failed("画不出二维码(需要 node 与 qrcode-terminal)")
    if on_qr:
        on_qr(qr)

    started, scanned = time.time(), False
    while True:
        if time.time() - started > QR_WAIT_S:
            raise Failed("等待扫码超时")
        d, _ = s.json_post(f"{accounts}/accounts/qrlogin/polling{_nonce()}",
                           {"biz_type": None}, dict(COMMON, **{"x-flow-key": flow_key}))
        p = d.get("data") or {}
        info = p.get("step_info") or {}
        st = info.get("status")
        if st == 2 and not scanned:
            scanned = True
            if on_status:
                on_status("已扫码,请在手机上确认")
        if st == 5:
            raise Failed("二维码已过期")
        if p.get("next_step") == "enter_app":
            if info.get("cross_login_uri"):
                s.req(info["cross_login_uri"])
            s.req(f"{open_host}/app")
            return s
        time.sleep(1.5)


def _walk(node, fn):
    """深度遍历嵌套的 dict/list。后台的返回体层级不定,各接口不同形,
    与其为每个接口写一份路径,不如遍历着找。"""
    if isinstance(node, list):
        for x in node:
            _walk(x, fn)
    elif isinstance(node, dict):
        fn(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk(v, fn)


#: 可见范围块的空值与各集合条目的 id 字段族(members 实测是 {id},其余按开放平台
#: 常见命名列候选)。任何条目解析不出 id 就抛:部分丢失同样会收窄可见范围。
_VIS_EMPTY = {"departments": [], "members": [], "groups": [], "isAll": 0}
_VIS_KEYS = {
    "departments": ("id", "departmentId", "department_id", "openDepartmentId", "open_department_id"),
    "members": ("id", "openId", "open_id", "userId", "user_id", "memberId", "member_id"),
    "groups": ("id", "groupId", "group_id", "chatId", "chat_id", "openChatId", "open_chat_id"),
}


def _vis_block(raw, label):
    """visible/online 的一个块 → 版本 payload 的 suggest 形态。解析不出就抛 Failed。"""
    if not isinstance(raw, dict):
        raise Failed(f"可见范围 {label} 形态未识别")
    if raw.get("isAll") not in (0, 1, True, False):
        raise Failed(f"可见范围 {label}.isAll 形态未识别")
    out = {"isAll": 1 if raw.get("isAll") in (1, True) else 0}
    for coll, keys in _VIS_KEYS.items():
        val = raw.get(coll)
        if val is None:
            out[coll] = []
            continue
        if not isinstance(val, list):
            raise Failed(f"可见范围 {label}.{coll} 形态未识别")
        ids = []
        for item in val:
            got = item if isinstance(item, str) else ""
            if not got and isinstance(item, (int, float)):
                got = str(item)
            if not got and isinstance(item, dict):
                for k in keys:
                    v = item.get(k)
                    if isinstance(v, str) and v:
                        got = v
                        break
                    if isinstance(v, (int, float)):
                        got = str(v)
                        break
            if not got:
                raise Failed(f"可见范围 {label}.{coll} 有条目解析不出 id")
            ids.append(got)
        out[coll] = ids
    return out


class Console:
    """开发者后台的 API 客户端。所有 /developers/v1/* 都要带从页面里读出的 csrfToken。"""

    def __init__(self, sess, brand):
        self.s = sess
        _, self.origin = HOSTS.get(brand, HOSTS["feishu"])
        self.referer = f"{self.origin}/app"
        _code, _h, html = self.s.req(self.referer)
        m = (re.search(r"\bwindow\.csrfToken\s*=\s*(['\"])([^'\"]+)\1", html)
             or re.search(r"\bcsrfToken\s*:\s*(['\"])([^'\"]+)\1", html))
        if not m:
            raise Failed("开发者后台没有返回 csrfToken,登录态可能没拿全")
        self.csrf = m.group(2)
        self.who = self._identity(html)

    @staticmethod
    def _identity(html):
        """当前登录人、企业与 userId。名字与企业给人确认没建错地方;uid 给首次发布的
        可见范围用——不含创建者的话应用发布后不会自动启用(botmux 同款要求)。

        按配对花括号截 JSON,不用「最多 800 字」这种长度上限:window.user 的实际长度
        没有契约,截断的表现是解析失败,而这里解析失败是静默的,人看不出少了什么。"""
        m = re.search(r"\bwindow\.user\s*=\s*", html)
        if not m:
            return {}
        s = html[m.end():]
        depth, end, in_str, esc = 0, -1, False, False
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return {}
        try:
            u = json.loads(s[:end])
        except ValueError:
            return {}

        def pick(*keys):
            for k in keys:
                v = u.get(k)
                if isinstance(v, str) and v:
                    return v
            return ""

        tdn = u.get("tenantDisplayName")
        return {"name": pick("name", "userName", "user_name"),
                "tenant": pick("tenantName", "tenant_name")
                          or (tdn.get("value") if isinstance(tdn, dict) else "") or "",
                "uid": pick("id", "userId", "user_id")}

    def post(self, path, body=None):
        d, _ = self.s.json_post(self.origin + path, body if body is not None else {}, {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": self.origin,
            "referer": self.referer,
            "x-csrf-token": self.csrf,
        })
        if isinstance(d, dict) and isinstance(d.get("code"), int) and d["code"] != 0:
            raise Failed(f"code={d['code']} {d.get('msg') or d.get('message') or ''}（{path}）")
        return d

    def _pick(self, r, keys):
        for k in keys:
            v = (r.get("data") or {}).get(k) if isinstance(r.get("data"), dict) else None
            v = v or r.get(k)
            if isinstance(v, str) and v:
                return v
        return ""

    # ---- 各步 ----

    def upload_icon(self):
        """传一张应用图标,→ url。模板建应用的请求体要它。
        取 Desk 自己的 512 图标;找不到就返回空,让调用方跳过模板路——
        缺图标不该把整条链拦死。"""
        for cand in ("frontend/icon-512.png", "frontend/icon-192.png"):
            p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), cand)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    blob = f.read()
                r = self.s.form_post(self.origin + "/developers/v1/app/upload/image",
                                     {"uploadType": "4",       # console 枚举:应用图标
                                      "isIsv": "false",
                                      "scale": json.dumps({"width": 512, "height": 512})},
                                     [("file", "desk.png", blob, "image/png")],
                                     {"origin": self.origin, "referer": self.referer,
                                      "x-csrf-token": self.csrf})
                return self._pick(r, ("url",))
        return ""

    def create_app_tpl(self, name, desc, avatar):
        """console launcher「一键创建智能体」的模板接口(botmux 抓包确认的做法)。
        模板建出的应用**出生即带**机器人能力、长连接订阅方式与基础事件——
        走这条就不再依赖 robot/switch 那步,而那一步在企业租户里可能卡审批。
        → app_id;被服务端明确拒绝时返回 ""(调用方回退普通自建);结果未知时抛
        (应用可能已建成,回退再建会产出重复应用)。"""
        import uuid
        try:
            r = self.post("/developers/v1/manifest/upsert_by_template", {
                "appManifestTemplateID": "developer_console",
                "createAppUserCustomField": {
                    "i18n": {"zh_cn": {"name": name, "description": desc}},
                    "avatar": avatar, "primaryLang": "zh_cn",
                },
                "cid": str(uuid.uuid4()), "HTTPHead": {},
            })
        except Failed as e:
            # 业务错误码(code=N)与 HTTP 404 是「明确拒绝」,应用肯定没建出来,可回退。
            # 传输错误、5xx 是「结果未知」,原样抛——那时回退是在赌它没建成。
            m = str(e)
            if "code=" in m or "HTTP 404" in m:
                return ""
            raise
        app_id = self._pick(r, ("ClientID", "clientID", "clientId", "appId"))
        if not app_id.startswith("cli_"):
            raise Failed("模板建应用返回成功但没有 ClientID(结果未知);"
                         "去开放平台确认有没有同名应用,再决定重试")
        return app_id

    def create_app(self, name, desc, avatar=""):
        body = {
            "appSceneType": 0,          # 企业自建
            "name": name, "desc": desc,
            "i18n": {"zh_cn": {"name": name, "description": desc}},
            "primaryLang": "zh_cn",
        }
        if avatar:
            body["avatar"] = avatar
        r = self.post("/developers/v1/app/create", body)
        app_id = self._pick(r, ("ClientID", "clientID", "clientId", "appId"))
        if not app_id.startswith("cli_"):
            raise Failed("建应用后没有拿到 ClientID:" + json.dumps(r, ensure_ascii=False)[:150])
        return app_id

    def list_apps(self):
        """→ (我名下的企业自建应用, 后台说一共有几个)。

        空体即可,后台按当前登录人返回。两个数可能不等——默认只给一页,而调用方必须
        把这个差说出来:静默截断的表现是「我那个应用不在列表里」,人于是去建一个新的,
        开放平台上就多一个重复应用。

        只留企业自建(appSceneType=0):商店应用配不了长连接,列出来只会让人选错。
        """
        r = self.post("/developers/v1/app/list", {})
        d = r.get("data") or {}
        out = []
        for a in (d.get("apps") or []):
            if not isinstance(a, dict):
                continue
            aid = str(a.get("appID") or a.get("appId") or "")
            if not aid.startswith("cli_") or a.get("appSceneType") not in (0, None):
                continue
            i18n = (a.get("i18n") or {}).get("zh_cn") or {}
            out.append({
                "app_id": aid,
                "name": a.get("name") or i18n.get("name") or aid,
                "desc": a.get("desc") or i18n.get("description") or "",
                # ability 里有 bot 才说明机器人能力开过。摆出来是因为它解释了一类困惑:
                # 没开的话 im:* 那几项在权限表里根本不可选,应用「看着好好的却配不上」。
                "has_bot": "bot" in (a.get("ability") or []),
                "created": a.get("createTime") or 0,
            })
        return out, int(d.get("totalCount") or len(out))

    def enable_bot(self, app_id):
        # 排在配权限之前:没开机器人能力时 im:* 那几项在权限表里不可选。
        self.post(f"/developers/v1/robot/switch/{app_id}", {"clientId": app_id, "enable": True})

    def set_scopes(self, app_id):
        """→ (配上几项, 没找到的清单)。没找到的不静默——少一项权限的后果是运行时
        某类消息读不到,而那时已经离配置很远了。"""
        allp = self.post(f"/developers/v1/scope/all/{app_id}")
        flat = {}

        def take(n):
            sid = n.get("id") or n.get("scopeID") or n.get("scopeId")
            key = n.get("scopeName") or n.get("name") or n.get("apiName") or n.get("scope")
            if sid is not None and isinstance(key, str):
                flat.setdefault(key, str(sid))

        _walk(allp.get("data") or allp, take)
        ids, missing = [], []
        for cands, why in SCOPES:
            sid = next((flat[c] for c in cands if c in flat), "")
            if sid:
                ids.append(sid)
            else:
                missing.append(f"{cands[0]}({why})")
        if ids:
            self.post(f"/developers/v1/scope/update/{app_id}", {
                "clientId": app_id, "appScopeIDs": ids, "userScopeIDs": [],
                "scopeIds": [], "operation": "add", "isDeveloperPanel": True,
            })
        return len(ids), missing

    def _events_now(self, app_id):
        """→ 当前已订阅的事件 key 集合。

        响应形状照 botmux extractOpenPlatformEventState:事件在 events/appEvents/
        userEvents 三个数组里,元素是字符串或带 id 的对象;另有 *EventDetails 分组,
        每组的 items 同上。2026-08-26 之前这里找的是 eventKey/event_key——那个字段
        在响应里不存在,于是永远「一个都没订」,update 之后回读照样「缺失」,
        模板应用出生自带的订阅也被判成没有。
        """
        cur = self.post(f"/developers/v1/event/{app_id}", {"needEventDetail": True})
        d = cur.get("data") if isinstance(cur.get("data"), dict) and cur.get("data") else cur
        have = set()

        def ids(v):
            for item in (v if isinstance(v, list) else []):
                if isinstance(item, str) and item:
                    have.add(item)
                elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                    have.add(item["id"])

        for k in ("events", "appEvents", "userEvents"):
            ids(d.get(k))
        for k in ("eventDetails", "appEventDetails", "userEventDetails"):
            grps = d.get(k)
            for g in (grps if isinstance(grps, list) else []):
                if isinstance(g, dict):
                    ids(g.get("items"))
        return have

    def set_events(self, app_id):
        """订阅方式改长连接并订阅事件。这一步配错的表现是「什么都配好了但一条消息也收不到」。

        请求体是 console 前端的增量契约:operation:add 只补缺的,事件分 appEvents/
        userEvents 两组(botmux buildEventSubscriptionPayload 同款)。2026-08-26 之前
        这里写的是 eventKeys——那个字段在开放平台不存在,一律 9499 Bad Request;
        botmux 注释里记过同类错(eventNames/eventNameList 也不存在,失败还被吞成
        warning,新建应用落地就没有任何订阅)。

        改完回读确认,缺了就抛:不回读的话,失败静默降级成一个「建好了却不回话」的 bot,
        而那正是这套配置里最难排查的一类故障。
        """
        self.post(f"/developers/v1/event/switch/{app_id}",
                  {"clientId": app_id, "eventMode": EVENT_MODE_WS})
        need = [e for e in EVENTS if e not in self._events_now(app_id)]
        if need:
            self.post(f"/developers/v1/event/update/{app_id}",
                      {"clientId": app_id, "operation": "add",
                       "events": [], "appEvents": need, "userEvents": [],
                       "eventMode": EVENT_MODE_WS})
            still = [e for e in EVENTS if e not in self._events_now(app_id)]
            if still:
                raise Failed(f"事件订阅没生效:{'、'.join(still)}(update 接了请求但回读缺失)",
                             app_id=app_id, step="事件订阅")
        return len(need)

    def _visibility_online(self, app_id):
        """读线上可见范围,→ (白名单块, 黑名单块)。解析不出就抛——发版会**整体覆写**
        可见范围,读不准还发,等于把应用从所有人面前收走(或把拉黑的人放出来)。
        宁可不发版(fail-closed,botmux parseOnlineVisibility 同款语义)。"""
        r = self.post(f"/developers/v1/visible/online/{app_id}")
        d = r.get("data")
        if not isinstance(d, dict):
            raise Failed("读不到线上可见范围(响应没有 data)")
        white = d.get("whiteList") if "whiteList" in d else d
        black = d.get("blackList")
        return (_vis_block(white, "whiteList"),
                _vis_block(black, "blackList") if isinstance(black, dict) else dict(_VIS_EMPTY))

    def publish(self, app_id):
        """建版本并提交发布。→ (版本号, 给人看的提示或 "")。

        请求体是 console launcher 的极简结构(botmux CDP 抓包确认):appVersion +
        两端默认能力 bot + visibleSuggest/blackVisibleSuggest。2026-08-26 之前发的
        versionName/updateDesc/i18n 那套字段在这个接口上不存在,一律 9499——
        与 eventKeys 是同一类错。

        三条硬语义(都来自 botmux 踩过的坑):
          · 版本号取**所有**版本(含未发布草稿)的最大三段号 +1,没有则 0.0.1。
            只看已发布的会与草稿撞号,平台以「版本号未递增」拒掉,应用永远停在未启用。
          · visibleSuggest 是**全量覆写**:首次发布给创建者一个人;已有版本的应用
            必须先读回线上可见范围整块镜像,读不到就中止——不发一个会收窄可见性的版本。
          · 不加 applyReasonConfig/isAutoAudit 之类字段:加了会进人工审核,
            应用停在未启用,事件配置进了草稿也不生效。
        """
        lst = self.post(f"/developers/v1/app_version/list/{app_id}")
        versions = ((lst.get("data") or {}).get("versions")
                    if isinstance(lst.get("data"), dict) else None) or []
        triples = []
        for v in versions:
            s = (v or {}).get("appVersion") if isinstance(v, dict) else None
            parts = str(s or "").split(".")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                triples.append(tuple(int(p) for p in parts))
        version = "0.0.1" if not triples else \
            ".".join(str(x) for x in max(triples)[:2] + (max(triples)[2] + 1,))

        note = ""
        if versions:
            white, black = self._visibility_online(app_id)
        else:
            uid = self.who.get("uid") or ""
            white = {"departments": [], "members": [uid] if uid else [],
                     "groups": [], "isAll": 0}
            black = dict(_VIS_EMPTY)
            if not uid:
                note = ("没读到你的 userId,首次发布的可见范围是空的——应用可能不会"
                        "自动启用,发完去后台把自己加进可见范围")

        created = self.post(f"/developers/v1/app_version/create/{app_id}", {
            "appVersion": version,
            "mobileDefaultAbility": "bot", "pcDefaultAbility": "bot",
            "changeLog": "由 Desk 创建",
            "visibleSuggest": white, "blackVisibleSuggest": black,
        })
        vid = self._pick(created, ("versionID", "versionId", "version_id", "id"))
        if not vid:
            raise Failed("建版本后没有拿到 versionID(可能留下未发布草稿,去后台确认)")
        self.post(f"/developers/v1/publish/commit/{app_id}/{vid}", {"clientId": app_id})
        return version, note

    def secret(self, app_id):
        r = self.post(f"/developers/v1/secret/{app_id}")
        s = self._pick(r, ("secret", "appSecret", "Secret"))
        if not s:
            raise Failed("没有读到 App Secret")
        return s


def run(brand, name, desc, on_qr=None, on_status=None, on_create=None, on_app=None):
    """整条链。→ {"app_id","secret","version","scopes","missing","who"}

    任何一步失败抛 Failed,带着 app_id 与 step。
    """
    say = on_status or (lambda _s: None)
    sess = login(brand, on_qr=on_qr, on_status=on_status)
    c = Console(sess, brand)
    if c.who.get("name"):
        say(f"当前身份:{c.who['name']}　企业:{c.who.get('tenant') or '?'}")

    app_id, step, from_tpl = "", "建应用", False
    try:
        say("建应用…")
        # 首选「一键创建智能体」模板:建出来就带机器人能力与长连接,不依赖后面
        # robot/switch 那步——那一步在企业租户里可能卡「机器人能力要审批」。
        # 模板被明确拒绝才回退普通自建(botmux 同款次序)。
        avatar = c.upload_icon()
        if on_create:
            on_create()  # 写前标记：请求结果不明时不能直接再创建一个应用
        if avatar:
            app_id = c.create_app_tpl(name, desc, avatar)
            from_tpl = bool(app_id)
        if not app_id:
            say("模板路不可用,回退普通自建…" if avatar else "取不到图标,走普通自建…")
            app_id = c.create_app(name, desc, avatar)
        if on_app:
            on_app(app_id, name)
        say(f"已建:{app_id}" + ("（模板,自带机器人能力）" if from_tpl else ""))

        step = "开机器人能力"
        try:
            c.enable_bot(app_id)
        except Failed:
            if not from_tpl:
                raise
            # 模板应用出生就带机器人能力,这一步只是幂等重设;租户把 robot/switch
            # 卡在审批后面时它会报错,但能力本身在——放行,最后真连一次见分晓。
            say("robot/switch 被拒(模板应用自带机器人能力,继续)")

        step = "配权限"
        say("配权限…")
        n, missing = c.set_scopes(app_id)
        say(f"配上 {n} 项" + (f",没找到:{'、'.join(missing)}" if missing else ""))

        step = "事件订阅"
        say("事件订阅改长连接…")
        c.set_events(app_id)

        step = "发版"
        say("发版…")
        version, vnote = c.publish(app_id)
        say(f"已发版 {version}")
        if vnote:
            say(vnote)

        step = "读凭证"
        secret = c.secret(app_id)
        return {"app_id": app_id, "secret": secret, "name": name, "version": version,
                "scopes": n, "missing": missing, "who": c.who}
    except Failed as e:
        e.app_id = e.app_id or app_id
        e.step = e.step or step
        raise


def adopt(brand, choose, confirm=None, on_qr=None, on_status=None, on_app=None):
    """已有应用那条:扫码 → 列出名下的应用 → 挑一个 → 配到能用 → 读凭证。

    → {"app_id","secret","name","version","scopes","missing","was_ready","who"}；
    choose 返回 None 表示放弃,这时也返回 None。

    与 run() 的差别只在开头:一个建、一个挑。后面那几步是同一串调用,因为「让这个应用
    能当 bot 用」的定义与它是新是旧无关。

    交互全部经 choose / confirm 交给调用方:这个模块要能在没有 tty 的地方跑。
    """
    say = on_status or (lambda _s: None)
    ask_yes = confirm or (lambda _m, _d="y": _d == "y")
    sess = login(brand, on_qr=on_qr, on_status=on_status)
    c = Console(sess, brand)
    if c.who.get("name"):
        say(f"当前身份:{c.who['name']}　企业:{c.who.get('tenant') or '?'}")

    app_id, step = "", "列应用"
    try:
        apps, total = c.list_apps()
        if not apps:
            raise Failed("这个账号名下没有企业自建应用。要新建的话走「一键接入」")
        if len(apps) < total:
            # 不静默:少列出来的那几个,人会当成「不存在」,转头去建一个重复的。
            say(f"注意:后台说一共 {total} 个,这里只列出 {len(apps)} 个")
        picked = choose(apps, total)
        if not picked:
            return None
        app_id = picked["app_id"]
        name = picked["name"]
        if on_app:
            on_app(app_id, name)
        desc = picked["desc"] or "由 Desk 托管的工作助手"

        # 写前闸。下面三步全是对这个应用的真实改动,而它可能正被别的服务用着——
        # 尤其订阅方式:它正以 webhook 给谁供事件的话,切成长连接就把那个服务弄断了。
        # 2026-08-26 有过一次:对一个在役应用动了订阅方式,用户不得不把应用整个停用。
        # 默认答案是 n:多敲一个 y 的代价,远小于弄断一个在役服务。
        step = "确认改动"
        if not ask_yes(f"将对「{name}」做:开机器人能力(如未开)、补 im 权限、"
                       f"订阅方式切成长连接并订阅事件。"
                       f"若它正被别的服务使用(尤其以 webhook 收事件),切订阅方式会弄断那个服务。"
                       f"确认这个应用只给 Desk 用吗?", "n"):
            say("没动任何配置。")
            return None

        step = "开机器人能力"
        was_bot = picked["has_bot"]
        if was_bot:
            say("机器人能力:已开")
        else:
            c.enable_bot(app_id)
            say("机器人能力:已开通")

        step = "配权限"
        n, missing = c.set_scopes(app_id)
        say(f"权限:配上 {n} 项" + (f",没找到:{'、'.join(missing)}" if missing else ""))

        step = "事件订阅"
        added = c.set_events(app_id)
        say("事件订阅:已是长连接" + (f",新订了 {added} 个事件" if added else ",事件已订过"))

        # 发版。权限与事件的改动不发版不生效,而发版可能要管理员审批——一个本来就配好
        # 的应用不该因为走了一趟向导就多排一次队。
        #
        # 但「它本来配好了没有」我们其实不知道:scope/update 是「设成这样」,问不出它原来
        # 是哪样。知道的只有两件:机器人能力原本开没开、事件是不是新订的。拿这两件定
        # **默认答案**,不拿它替人做决定——这是能力边界,不该装作判得出来。
        step = "发版"
        likely_ready = was_bot and added == 0
        version = ""
        if ask_yes("现在创建并发布一个新版本吗?（权限与事件的改动要发版才生效；"
                   "企业内可能要管理员审批）"
                   + ("　看着它本来就配好了,可以先不发" if likely_ready else ""),
                   "n" if likely_ready else "y"):
            version, vnote = c.publish(app_id)
            say(f"已提交版本 {version}")
            if vnote:
                say(vnote)
        else:
            say("跳过发版——刚才若有改动,要等发版才生效")

        step = "读凭证"
        secret = c.secret(app_id)
        return {"app_id": app_id, "secret": secret, "name": name, "version": version,
                "scopes": n, "missing": missing, "was_ready": likely_ready, "who": c.who}
    except Failed as e:
        e.app_id = e.app_id or app_id
        e.step = e.step or step
        raise


if __name__ == "__main__":
    def _arg(k, d=""):
        a = sys.argv
        return a[a.index("--" + k) + 1] if ("--" + k) in a and a.index("--" + k) + 1 < len(a) else d

    try:
        out = run(_arg("domain", "feishu"), _arg("name", "Desk Bot"),
                  _arg("desc", "由 Desk 托管的工作助手"),
                  on_qr=lambda qr: print(qr, file=sys.stderr),
                  on_status=lambda s: print("  " + s, file=sys.stderr))
        out.pop("secret", None)      # 单独跑时不打印凭证,它只该经调用方交给 Desk
        print(json.dumps(out, ensure_ascii=False))
    except Failed as e:
        print(json.dumps({"error": str(e), "app_id": e.app_id, "step": e.step},
                         ensure_ascii=False))
        sys.exit(1)
