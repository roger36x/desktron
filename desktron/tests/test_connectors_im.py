"""IM 接入层(connectors/im)接线契约:描述符注册、能力声明、领域侧供应商无名化不变量。
不变量条款:领域代码不出现供应商名字与子进程调用。"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from connectors.im import get, IMSpec, ThreadModel, PullMode, Surface, Identity  # noqa: E402
import connectors.im.lark as lark  # noqa: E402


def test_registry_default_is_lark():
    spec = get()
    assert isinstance(spec, IMSpec)
    assert spec.key == "lark"
    assert get("lark") is spec
    assert get("wecom") is None   # 未接入的供应商取不到,不给假对象


def test_lark_capability_declaration():
    """保留话题群消息只读预览，不再声明 Timeline 三源采集。"""
    spec = get("lark")
    assert spec.thread_model is ThreadModel.TOPIC_GROUP
    assert spec.pull is PullMode.POLL_CLI
    assert spec.surfaces == frozenset({Surface.CHAT})
    assert spec.identity is Identity.USER
    assert spec.impl is lark


def test_connector_owns_vendor_surface():
    """四类接口面收敛在 connector:CLI 调用、深链域名、池布局、缓存文件名。"""
    src = open(os.path.join(LIB, "connectors", "im", "lark.py"), encoding="utf-8").read()
    assert "lark-cli" in src
    assert "applink.feishu.cn" in src
    assert 'POOL_SUBDIR = "lark"' in src
    assert lark.pool_dir("/sync", "im") == os.path.join("/sync", "lark", "im")
    assert lark.chat_link("oc_x").startswith("https://applink.feishu.cn/")
    assert lark.doc_link("wiki", "tok") == "https://www.feishu.cn/wiki/tok"
    assert lark.doc_link_guess("wikAbc").endswith("/wiki/wikAbc")
    assert lark.doc_link_guess("Abc123").endswith("/docx/Abc123")


def test_reference_route_uses_connector_without_timeline():
    src = open(os.path.join(LIB, "feed", "api.py"), encoding="utf-8").read()
    assert "subprocess" not in src
    assert "lark.message_reference" in src
    assert "import lark_events" not in src
    assert 'os.path.join(sync_root, "lark"' not in src


def test_connector_no_domain_import():
    """依赖单向:connector 不 import 领域模块。"""
    src = open(os.path.join(LIB, "connectors", "im", "lark.py"), encoding="utf-8").read()
    for bad in ("import lark_events", "import lark_topics", "import lark_chatclass",
                "from lark_events", "from feed", "import workbench", "import tasks"):
        assert bad not in src, bad
