#!/usr/bin/env python3
"""HTML 会话视图三项功能的一次性核验:子代理、目标、压缩。

重启后跑一次就知道有没有真的交付。只发 GET,不改任何状态。
用法: python3 scripts/check_session_features.py [sid]
不给 sid 就取最近更新的那段 claude 会话。
"""
import json, os, sys, glob, urllib.request

BASE = "http://127.0.0.1:7765"
ok = fail = 0


def check(name, cond, note=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  通过  {name}" + (f"  ({note})" if note else ""))
    else:
        fail += 1
        print(f"  未过  {name}" + (f"  ({note})" if note else ""))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


INDEX = os.path.expanduser("~/.local/share/desktron/data/adapter/claude/sessions.json")


def esid_of(sid):
    try:
        return (json.load(open(INDEX, encoding="utf-8")).get(sid) or {}).get("engine_sid") or ""
    except Exception:
        return ""


def pick_sid():
    if len(sys.argv) > 1:
        return sys.argv[1]
    ix = json.load(open(INDEX, encoding="utf-8"))
    live = [v for v in ix.values() if v.get("engine_sid")]
    live.sort(key=lambda s: -(s.get("time", {}).get("updated") or 0))
    # 挑记录真的在的那一段:刚建还没跑过的会话有 engine_sid 却没有 transcript,
    # 选中它的话压缩与翻页两项无从验起
    for v in live:
        if glob.glob(os.path.expanduser(f"~/.claude/projects/*/{v['engine_sid']}.jsonl")):
            return v["id"]
    return live[0]["id"] if live else ""


def main():
    sid = pick_sid()
    if not sid:
        print("找不到可用会话"); return 1
    print(f"会话 {sid}\n")

    print("一、子代理")
    ags = get(f"/session/{sid}/agents")
    check("端点可用", isinstance(ags, list), f"{len(ags)} 个")
    if ags:
        a = ags[-1]
        check("字段齐全", all(k in a for k in ("id", "task", "last", "updated", "size")))
        log = get(f"/session/{sid}/agents/{a['id']}")
        kinds = {q.get("type") for m in log for q in (m.get("parts") or [])}
        check("能取到输出", bool(log), f"{len(log)} 条消息, parts: {sorted(kinds)}")
        check("前端渲染分支覆盖", kinds <= {"text", "reasoning", "tool", "image"},
              "出现未覆盖的类型" if not kinds <= {"text", "reasoning", "tool", "image"} else "")

    print("\n二、目标")
    g = get(f"/session/{sid}/goal")
    check("端点可用", isinstance(g, dict))
    if g:
        check("字段齐全", all(k in g for k in ("condition", "met", "at")),
              f"met={g.get('met')} 条件={str(g.get('condition'))[:24]}")

    print("\n三、压缩与翻页(离线跑分组,验的是重启后会加载的那份代码)")
    sys.argv = ["x", "frontend", "7999", "8000",
                os.path.expanduser("~/.local/share/desktron/workspace")]
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "desktron", "lib"))
    import engine_adapters as ea

    class Bus:
        def publish(self, e): pass

    # 数据目录用临时的:只借它的分组逻辑,不碰真索引,也不留下任何写入。
    # 会话与记录的对应关系另从真索引里查——用临时目录建出来的适配器索引是空的。
    ad = ea.ClaudeAdapter("/tmp/cd_chk", os.path.expanduser("~/.local/share/desktron/workspace"), Bus())
    esid = esid_of(sid)
    if esid:
        ad.index[sid] = {"id": sid, "engine_sid": esid}
    hit = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{esid}.jsonl")) if esid else []
    if not hit:
        check("找得到记录", False, "跳过压缩与翻页")
    else:
        msgs = ad._group_transcript(hit[0], sid, esid) or []
        cmp_rows = [m for m in msgs if m["info"].get("compact")]
        check("压缩行都带 pre 与 post",
              all(c["info"]["compact"].get("pre") and c["info"]["compact"].get("post")
                  for c in cmp_rows), f"{len(cmp_rows)} 条")
        ids = [m["info"]["id"] for m in cmp_rows]
        check("压缩行不重复", len(ids) == len(set(ids)))
        adj = sum(1 for i in range(1, len(msgs))
                  if msgs[i]["info"].get("compact") and msgs[i - 1]["info"].get("compact"))
        check("没有并排的两条压缩", adj == 0)
        autos = [i for i, m in enumerate(msgs)
                 if m["info"].get("compact", {}).get("trigger") == "auto"]
        check("自动压缩排在发言之前",
              all(i + 1 < len(msgs) and ad._turn_start(msgs[i + 1]) for i in autos),
              f"{len(autos)} 条")
        starts = [i for i, m in enumerate(msgs) if ad._turn_start(m)]
        first = msgs[starts[-50]:] if len(starts) > 50 else msgs
        have = sum(1 for m in first if ad._turn_start(m))
        page = ad.history(sid, have=have, limit=30)
        inter = {m["info"]["id"] for m in first} & {m["info"]["id"] for m in page}
        check("翻页不与首屏重复", not inter, f"首屏 {have} 回合, 第二页 {len(page)} 条")

    print(f"\n通过 {ok} 项, 未过 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
