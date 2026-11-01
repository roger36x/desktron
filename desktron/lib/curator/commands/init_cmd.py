"""curator init <dir> —— 从包内模板脚手架一个实例。"""

from __future__ import annotations

import os
import shutil
from importlib import resources

from ..util import CuratorError


def _template_root():
    return resources.files("curator") / "template"


def run_init(args) -> int:
    dest = os.path.abspath(args.dir)
    if os.path.exists(dest) and os.listdir(dest):
        if not args.force:
            raise CuratorError(f"{dest} 非空;加 --force 覆盖")
    os.makedirs(dest, exist_ok=True)

    src = _template_root()
    with resources.as_file(src) as src_path:
        for root, _dirs, files in os.walk(src_path):
            rel = os.path.relpath(root, src_path)
            target_dir = dest if rel == "." else os.path.join(dest, rel)
            os.makedirs(target_dir, exist_ok=True)
            for fn in files:
                if fn == "__pycache__":
                    continue
                shutil.copy2(os.path.join(root, fn), os.path.join(target_dir, fn))

    print(f"已脚手架 curator 实例:{dest}")
    print("下一步:编辑 ontology.yaml 声明本领域实体/边/doc_type,再 curator add。")
    print("此目录若是 git 仓,跑 curator install-hooks 装提交期准入闸(挡 git 外 band 内容上远端)。")
    return 0


def add_parser(sub):
    p = sub.add_parser("init", help="从模板脚手架一个实例")
    p.add_argument("dir")
    p.add_argument("--force", action="store_true", help="目标非空时覆盖")
    p.set_defaults(func_init=run_init)
