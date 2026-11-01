"""技能领域路由:repo/全局技能枚举、全局技能正文、命令清单(技能即命令)。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, re, urllib.parse
from deskcfg import REPO_ROOT
from domains import _skill_frontmatter, _skill_candidate_rank


def owns(p, method):
    return p in ("/workbench/gskill", "/workbench/commands", "/workbench/skills")


def handle(h, p, q, method):
    if p == "/workbench/gskill":
        # 全局已安装技能正文:读 ~/.claude/skills/<name>/SKILL.md(name 受限,防越界)
        params = urllib.parse.parse_qs(q)
        name = (params.get("name") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
            return h._send_json({"error": "bad name"}, 400)
        gf = os.path.join(os.path.expanduser("~/.claude/skills"), name, "SKILL.md")
        try:
            with open(gf, encoding="utf-8", errors="replace") as fh:
                txt = fh.read(512 * 1024)
        except Exception:
            return h._send_json({"error": "not found"}, 404)
        data = txt.encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers(); h.wfile.write(data)
        return
    if p == "/workbench/commands":
        out = []
        cdir = os.path.join(REPO_ROOT, ".claude", "commands")
        if os.path.isdir(cdir):
            for f in sorted(os.listdir(cdir)):
                if not f.endswith(".md"):
                    continue
                try:
                    with open(os.path.join(cdir, f), encoding="utf-8") as fh:
                        txt = fh.read(32768)
                except Exception:
                    continue
                desc, body = "", txt
                if txt.startswith("---"):
                    end = txt.find("\n---", 3)
                    if end > 0:
                        fm = txt[3:end]
                        body = txt[end + 4:].lstrip("\n")
                        for ln in fm.splitlines():
                            if ln.strip().startswith("description:"):
                                desc = ln.split(":", 1)[1].strip()
                out.append({"name": f[:-3], "desc": desc, "body": body})
        return h._send_json(out)
    if p == "/workbench/skills":
        # 技能即命令(对齐真实工具的 /skill-name):枚举 repo + 全局技能目录,读 SKILL.md frontmatter。
        # 按 frontmatter name 去重,保留 root 优先级 + 规范目录名最高的一份。
        roots = (os.path.join(REPO_ROOT, ".claude", "skills"),
                 os.path.expanduser("~/.claude/skills"))
        by_name = {}
        for root_idx, d in enumerate(roots):
            if not os.path.isdir(d):
                continue
            for dirname in sorted(os.listdir(d)):
                meta = _skill_frontmatter(d, dirname)
                if not meta:
                    continue
                nm, desc = meta
                rank = (root_idx, _skill_candidate_rank(dirname, nm))
                cur = by_name.get(nm)
                if cur is None or rank < cur[0]:
                    by_name[nm] = (rank, desc[:120])
        out = [{"name": nm, "desc": by_name[nm][1]} for nm in sorted(by_name)]
        return h._send_json(out)
    return h._send_json({"error": "unknown"}, 404)
