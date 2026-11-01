"""IDE 文件树按 VS Code Seti 的主流类型显示独立图标。"""
import base64
import os
import re
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "frontend", "app-assets", "files", "src")
ENTRY = open(os.path.join(SRC, "entry.js"), encoding="utf-8").read()
SETI = open(os.path.join(SRC, "seti-icons.js"), encoding="utf-8").read()
BUILD = open(os.path.join(SRC, "build.mjs"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "frontend", "pages", "files.html"), encoding="utf-8").read()


class MainstreamMappings(unittest.TestCase):
    def test_主流语言与资产类型都有独立映射(self):
        for key in ("python", "javascript", "typescript", "react", "json", "markdown",
                    "yaml", "html", "css", "shell", "docker", "go", "rust", "java",
                    "db", "image", "pdf", "archive", "notebook"):
            self.assertRegex(SETI, rf"\b{re.escape(key)}:\s*\[")

    def test_特殊文件名优先于扩展名(self):
        for name in ("package.json", "tsconfig.json", "dockerfile", ".gitignore",
                     "makefile", "pyproject.toml", "cargo.toml"):
            self.assertIn(f'"{name}"', SETI)
        self.assertLess(SETI.index("if (NAMES[base])"), SETI.index("const parts = base.split"))

    def test_未识别类型回退默认图标(self):
        self.assertIn('|| "default"', SETI)
        self.assertIn('default: ["E023"', SETI)


class SharedRendering(unittest.TestCase):
    def test_目录树搜索结果标签与面包屑共用映射(self):
        self.assertIn('icon(it.type, it.name, isOpen, hasChildren)', ENTRY)
        self.assertIn('icon("file", it.name || it.path)', ENTRY)
        self.assertIn('${setiFileIcon(t.name)}</span>', ENTRY)
        self.assertIn("fileIcon.innerHTML = setiFileIcon(p)", ENTRY)

    def test_深浅主题使用_seti_原生两档颜色(self):
        self.assertIn('font-family: "Desk Seti"', HTML)
        self.assertIn("color: var(--seti-dark)", HTML)
        self.assertIn(':root[data-theme="light"] .seti-file-icon', HTML)
        self.assertIn("color: var(--seti-light)", HTML)


class VendoredFont(unittest.TestCase):
    def test_字体随构建产物发布且保留授权(self):
        self.assertIn('"seti.woff.b64"', BUILD)
        self.assertIn('path.join(OUT, "seti.woff")', BUILD)
        self.assertIn('"SETI-LICENSE.txt"', BUILD)
        with open(os.path.join(SRC, "seti.woff.b64"), encoding="ascii") as f:
            raw = f.read()
        self.assertGreater(len(base64.b64decode(raw)), 30000)
        with open(os.path.join(SRC, "SETI-LICENSE.txt"), encoding="utf-8") as f:
            notice = f.read()
        self.assertIn("Copyright (c) 2014 Jesse Weed", notice)


if __name__ == "__main__":
    unittest.main()
