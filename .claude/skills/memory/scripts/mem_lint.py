#!/usr/bin/env python3
"""记忆合规校验。薄封装 curator lint:扫工作层成品桶(private/local)的必备字段、置信度伴随字段、
表达黑名单、鲜度、id 唯一性。temp 免闸不扫。ERROR 阻断(非零退出),WARN 不阻断。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mem_common import work_root, ensure_instance, run_curator


def main():
    root = work_root()
    ensure_instance(root)
    run_curator(root, "lint", [])


if __name__ == "__main__":
    main()
