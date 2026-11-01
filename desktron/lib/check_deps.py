#!/usr/bin/env python3
"""读 manifest.yaml 的 requirements,逐项探测运行时/引擎是否就绪、版本是否达标。
无第三方依赖(不用 PyYAML;requirements 是受控的扁平结构,正则解析)。
退出码:0=必备运行时齐备;1=有必备运行时缺失或低于 min。引擎缺失/版本不符只报告不算失败(不强制降级)。"""
import os
import re
import shutil
import subprocess
import sys

import engine_protocol


def parse_reqs(repo):
    path = repo if repo.endswith(".yaml") else os.path.join(repo, "manifest.yaml")
    txt = open(path, encoding="utf-8").read()
    m = re.search(r"^requirements:\s*\n(.*?)(?=^\S|\Z)", txt, re.S | re.M)
    if not m:
        return []
    out = []
    for it in re.split(r"^\s*-\s+", m.group(1), flags=re.M)[1:]:
        d = {}
        for k in ("kind", "name", "min", "pin", "install", "hint", "platform",
                  "install_windows", "hint_windows"):
            mm = re.search(rf"^\s*{k}:\s*(.+?)\s*$", it, re.M)
            if mm:
                # 单双引号都要脱。原来只脱双引号,于是值里含双引号时(install 命令带
                # echo "…" 就是)只能用单引号包裹,而包裹的那对会被原样读进命令里,
                # 预填出去是跑不通的。YAML 两种引号等价,这里的简易解析要跟上。
                v = mm.group(1).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                d[k] = v
        if not d.get("name"):
            continue
        # platform 限定条目只在对应系统出现(如 swiftc 仅 macOS、cargo 仅 Windows)。
        if d.get("platform") and d["platform"] != sys.platform:
            continue
        # Windows 的安装命令在 PowerShell 中执行，与 POSIX shell 写法不同，单独声明。
        windows = sys.platform == "win32"
        for k in ("install", "hint"):
            alt = d.pop(k + "_windows", None)
            if windows and alt:
                d[k] = alt
        out.append(d)
    return out


def ver_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def probe(name):
    """返回 (是否存在, 版本字符串)。版本取 `<name> --version` 输出里第一个 x.y[.z]。"""
    if not shutil.which(name):
        return False, ""
    try:
        r = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=8)
        # 跑不起来就不算存在。which 命中只说明路径上有那个名字——链接断了、架构不对、
        # 依赖缺失时它照样在,而 --version 会非零退出。原来忽略 returncode,于是这类
        # 「装了但用不了」被判成 ok,自动补装那一步根本不会触发,人要等到真正用它时
        # 才撞上(2026-09-03 另一会话 review 指出)。
        if r.returncode != 0:
            return False, ""
        pattern = (r"\b(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\b" if name == 'codex'
                   else r"(\d+\.\d+(?:\.\d+)?)")
        mm = re.search(pattern, r.stdout + r.stderr)
        return True, (mm.group(1) if mm else "")
    except Exception:
        # 超时或起不来:同样按不存在算,理由同上。
        return False, ""


def _npm_global_prefix():
    try:
        r = subprocess.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def engine_detail(name, ver, fallback):
    """引擎展示细节。codex 额外提示 Desk 的 ~/.local 安装位与 npm 默认 prefix 不一致的问题。"""
    if name != "codex":
        return fallback
    home = os.path.expanduser("~")
    desk_prefix = os.path.join(home, ".local")
    desk_codex = os.path.join(desk_prefix, "bin", "codex")
    codex_path = os.path.realpath(shutil.which("codex") or "")
    if codex_path != os.path.realpath(desk_codex):
        return fallback
    npm_prefix = _npm_global_prefix()
    if not npm_prefix or os.path.realpath(npm_prefix) == os.path.realpath(desk_prefix):
        return fallback
    install_cmd = 'npm i -g --prefix "$HOME/.local" @openai/codex'
    return (f"{fallback}; Desk 优先使用 ~/.local/bin/codex,但 npm -g 默认 prefix={npm_prefix}; "
            f"更新请用: {install_cmd}")


def emit_engines(repo):
    """供 install 自动装引擎:打印 name<TAB>install(只含声明了 install 的 engine)。"""
    for r in parse_reqs(repo):
        if r.get("kind") == "engine" and r.get("install"):
            print(f"{r['name']}\t{r['install']}")
    return 0


def emit_missing_runtime(repo):
    """供 install 自动补运行时:只打印「确实缺或版本过低」的那几条,name<TAB>install。

    与 emit_engines 分开:引擎缺了可以只装一部分(至少一家能用就行),运行时缺了什么
    都做不成。而且这一份要先探测再决定——全都打印出来的话,装机会把已经装好的
    python3 再装一遍。"""
    for r in parse_reqs(repo):
        if r.get("kind") != "runtime" or not r.get("install"):
            continue
        present, ver = probe(r["name"])
        mn = r.get("min")
        if present and (not mn or ver_tuple(ver) >= ver_tuple(mn)):
            continue
        print(f"{r['name']}\t{r['install']}")
    return 0


def engine_ready(name):
    """供安装器判断已存在的命令是否还满足 Desk 协议，而非只看 which。"""
    if name not in engine_protocol.LABELS or not probe(name)[0]:
        return 1
    return 0 if engine_protocol.availability(name).get("available") else 1


def main():
    if len(sys.argv) > 2 and sys.argv[2] == "--emit-engines":
        return emit_engines(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "--emit-missing-runtime":
        return emit_missing_runtime(sys.argv[1])
    if len(sys.argv) > 3 and sys.argv[2] == "--engine-ready":
        return engine_ready(sys.argv[3])
    reqs = parse_reqs(sys.argv[1])
    hard_missing = 0
    rows = []
    for r in reqs:
        name, kind = r["name"], r.get("kind", "")
        present, ver = probe(name)
        mn, pin = r.get("min"), r.get("pin")
        if not present:
            if kind == "runtime":
                hard_missing += 1
                rows.append(("缺", name, f"必备,装: {r.get('hint', '')}"))
            elif kind == "engine":
                rows.append(("缺", name, f"引擎未装,装: {r.get('install', r.get('hint', ''))}"))
            else:  # build
                rows.append(("warn", name, f"未装(仅 app-install 需要): {r.get('hint', '')}"))
        # 声明了最低版本却读不到版本号，不能当作可用。命令可能只是一个坏掉的 shim，
        # 或输出格式已经变化；继续安装只会把失败推迟到更难定位的位置。
        elif mn and (not ver or ver_tuple(ver) < ver_tuple(mn)):
            if kind == "runtime":
                hard_missing += 1
            detail = f"版本读不到;{r.get('hint', '')}" if not ver else f"{ver} < 要求 {mn};{r.get('hint', '')}"
            rows.append(("warn", name, detail))
        elif name in engine_protocol.LABELS and kind == 'engine':
            admission = engine_protocol.availability(name)
            detail = (engine_detail(name, ver, (ver or '版本未知') + '; 协议能力已确认')
                      if admission['available'] else admission['message'])
            rows.append(('ok' if admission['available'] else 'warn', name, detail))
        elif pin and ver and ver != pin:
            rows.append(("warn", name, f"{ver} ≠ 钉版 {pin}(不强制降级)"))
        else:
            rows.append(("ok", name, engine_detail(name, ver, ver or "已装")))

    w = max((len(n) for _, n, _ in rows), default=4)
    for st, name, detail in rows:
        print(f"  [{st:>4}] {name:<{w}}  {detail}")
    if hard_missing:
        print(f"\n{hard_missing} 个必备运行时缺失或过低,补齐后再装。", file=sys.stderr)
    return 1 if hard_missing else 0


if __name__ == "__main__":
    sys.exit(main())
