"""测试与本机数据隔离。

term-meta.json、adapter/、mailbox 这些都挂在 deskcfg.DATA_HOME 下，而它在模块 import 时
一次求值。测试即使把适配器指向 tmp_path，term_meta_register 这类调用仍按模块常量写进真实
数据目录——本机侧栏因此长出 e1 / eng-1 / esid-1 / eng-2 / pi-1 这些夹具行，
在界面上和真会话排在一起，右键删掉之后跑一次测试又回来(2026-09-10 实见:term-meta.json 的
mtime 与测试进程的启动时刻同秒)。

conftest 在收集测试模块之前加载，这里把环境变量设好，之后无论谁先 import deskcfg，
拿到的都是临时目录。已经自己做隔离的用例照旧生效:它们显式设值与还原，不受这一层影响。
"""
import os
import tempfile

import pytest

_DATA = os.path.join(tempfile.mkdtemp(prefix="desk-tests-"), "data")
os.makedirs(_DATA, exist_ok=True)
os.environ.setdefault("DESKTRON_DATA_HOME", _DATA)


def pytest_configure(config):
    config.addinivalue_line('markers', 'desktop: explicit browser or native GUI test')


def pytest_collection_modifyitems(config, items):
    # Direct pytest is safe too; installing Playwright must not silently opt in.
    if os.environ.get('DESKTRON_DESKTOP_TESTS') == '1':
        return
    blocked = pytest.mark.skip(reason='桌面测试需显式运行 scripts/dev/run-tests.py --desktop')
    for item in items:
        if item.get_closest_marker('desktop') is not None:
            item.add_marker(blocked)
