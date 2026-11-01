"""飞书原页上下文：正文对模型可见，用户气泡仍只保存用户自己的话。"""
import base64
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    from engine_adapters.bus import EventBus
    from engine_adapters.pi import PiAdapter
finally:
    sys.argv = _argv


def test_lark_web_context_is_transient_grounding_and_keeps_public_user_text(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "prompts").mkdir()
    (workspace / "prompts" / "style.md").write_text("", encoding="utf-8")
    adapter = PiAdapter(str(tmp_path / "data"), str(workspace), EventBus())
    sid = adapter.create_session()["id"]
    adapter.grounding = lambda *_args, **_kwargs: {"constant": "", "scene": ""}
    adapter.build_cmd = lambda text, *_args: ["pi", text]
    captured = {}

    def dispatch(ctx, _cmd, _env, _frame):
        captured.update(ctx)
        return None

    adapter._turn_dispatch = dispatch
    material = "产品讨论\n张三 10:30\n今天先发布灰度版本"
    attachment = {
        "type": "context",
        "name": "飞书聊天上下文.txt",
        "media_type": "text/plain",
        "data_b64": base64.b64encode(material.encode()).decode(),
    }
    adapter._run(sid, "请总结结论", {}, [attachment])

    user = next(m for m in adapter.messages(sid) if m["info"]["role"] == "user")
    assert user["parts"][0]["text"] == "请总结结论"
    assert any(p.get("context_kind") == "lark_web" for p in user["parts"])
    assert captured["_user_text"] == "请总结结论"
    assert captured['text'] == '请总结结论'
    assert material in captured['opts']['_reference_context']
    assert '只作为参考资料' in captured['opts']['_reference_context']
