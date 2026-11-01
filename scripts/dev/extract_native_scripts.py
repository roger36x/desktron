#!/usr/bin/env python3
"""从 macOS 原生壳(main.swift)提取页面脚本，生成 Windows 壳共用的 desktop/shared/js/*.js。

两端必须执行同一份页面脚本：App Control 的定位、可操作性等待与回执语义都写在这些脚本里。
Swift 侧仍以字符串内嵌(零外部依赖构建)，Windows 侧编译时 include_str! 这些文件。
测试 test_windows_shell_scripts.py 会重新提取并比对，任何一侧改动未同步都会失败。

用法：python3 scripts/dev/extract_native_scripts.py [--check]
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SWIFT = os.path.join(ROOT, "desktop", "native", "main.swift")
OUT = os.path.join(ROOT, "desktop", "shared", "js")

# 函数名 → 输出文件；raw=True 表示 Swift 原始字符串 #"""…"""#。
FUNCTIONS = {
    "appControlDOMScript": ("app-control-dom.js", True),
    "larkMessageReferenceScript": ("lark-message-reference.js", True),
    "larkReferenceContextScript": ("lark-reference-context.js", True),
    "larkSelectChatScript": ("lark-select-chat.js", False),
}
# 非原始字符串里的插值 → Windows 侧替换的占位符(值已按 JSON 字符串转义)。
PLACEHOLDERS = {
    r"\(cname)": "__DESKTRON_TARGET_CHAT__",
    r"\(tname)": "__DESKTRON_TARGET_TAG__",
    r"\(shouldClickResult)": "__DESKTRON_CLICK_RESULT__",
}
# 内联在其他函数里的短脚本：以首行与尾行定位。
INLINE = {
    "network-tracker.js": ("(()=>{if(globalThis.__deskNetworkTracker)return;", "publish();document.addEventListener('readystatechange',publish);})();"),
    "desk-surface.js": ("const clean = value => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 120);",
                        "observation_version: 'navigation:' + (hash >>> 0).toString(16)};"),
}


def _dedent(lines):
    closing = lines[-1]
    indent = len(closing) - len(closing.lstrip(" "))
    body = lines[:-1]
    return "\n".join(line[indent:] if line.startswith(" " * indent) else line.lstrip(" ")
                     for line in body) + "\n"


def _block(source, name, raw):
    start = source.index("func %s(" % name)
    opener = '#"""' if raw else '"""'
    begin = source.index(opener, start) + len(opener)
    begin = source.index("\n", begin) + 1
    closer = '"""#' if raw else '"""'
    end = source.index(closer, begin)
    line_start = source.rindex("\n", 0, end) + 1
    lines = source[begin:end].split("\n")
    lines[-1] = source[line_start:end]
    return _dedent(lines)


def _unescape(text):
    for needle, placeholder in PLACEHOLDERS.items():
        text = text.replace(needle, placeholder)
    if re.search(r"\\\(", text):
        raise ValueError("脚本里出现未登记的 Swift 插值")
    out, i = [], 0
    simple = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch); i += 1; continue
        nxt = text[i + 1]
        if nxt in simple:
            out.append(simple[nxt]); i += 2; continue
        if nxt == "u" and text[i + 2] == "{":
            close = text.index("}", i)
            out.append(chr(int(text[i + 3:close], 16))); i = close + 1; continue
        if nxt == "\n":   # 行尾续行
            i += 2; continue
        raise ValueError("无法识别的 Swift 转义：\\" + nxt)
    return "".join(out)


def _inline(source, first, last):
    start = source.index(first)
    end = source.index(last, start) + len(last)
    line_start = source.rindex("\n", 0, start) + 1
    indent = start - line_start
    lines = source[line_start:end].split("\n")
    return "\n".join(line[indent:] if line.startswith(" " * indent) else line.lstrip(" ")
                     for line in lines) + "\n"


def extract():
    with open(SWIFT, encoding="utf-8") as stream:
        source = stream.read()
    result = {}
    for name, (filename, raw) in FUNCTIONS.items():
        text = _block(source, name, raw)
        result[filename] = text if raw else _unescape(text)
    for filename, (first, last) in INLINE.items():
        result[filename] = _inline(source, first, last)
    return result


def main(argv):
    check = "--check" in argv
    stale = []
    for filename, text in extract().items():
        path = os.path.join(OUT, filename)
        current = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if current == text:
            continue
        stale.append(filename)
        if not check:
            os.makedirs(OUT, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
    if check and stale:
        print("与 main.swift 不一致：" + ", ".join(stale), file=sys.stderr)
        return 1
    print(("已生成 " if not check else "一致 ") + str(len(FUNCTIONS) + len(INLINE)) + " 个脚本")
    return 0


if __name__ == "__main__":
    # Chinese status lines must not depend on the console code page (cp1252 on CI runners).
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
