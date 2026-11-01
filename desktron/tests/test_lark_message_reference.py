"""回答中的飞书消息引用：链接识别、本地反查与邻近上下文。"""
import json
import os
import sys
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from connectors.im import lark  # noqa: E402


def _put(root, minute, mid, position):
    folder = os.path.join(root, "lark", "im")
    os.makedirs(folder, exist_ok=True)
    item = {
        "message_id": mid,
        "chat_id": "oc_team",
        "message_position": str(position),
        "message_app_link": (
            "https://applink.feishu.cn/client/chat/open?"
            f"openChatId=oc_team&position={position}"
        ),
        "create_time": f"2026-09-16 10:{minute:02d}",
        "sender": {"name": f"成员{minute}", "sender_type": "user"},
        "msg_type": "text",
        "content": f"消息 {minute}",
        "deleted": False,
        "updated": False,
    }
    path = os.path.join(folder, f"2026-09-16-{mid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False)
    return path, item


def test_only_specific_message_links_are_claimed():
    assert lark.parse_message_reference(
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_x&messageId=om_x")
    assert lark.parse_message_reference(
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_x&position=18")
    assert lark.parse_message_reference(
        "https://applink.feishu.cn/client/message/link/open?token=abc")
    assert not lark.parse_message_reference(
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_x")
    assert not lark.parse_message_reference(
        "https://evil.example/client/chat/open?messageId=om_x")


def test_message_id_uses_sync_cache_and_returns_context(tmp_path):
    for minute in range(5):
        _put(str(tmp_path), minute, f"om_{minute}", 100 + minute)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "lark-chat-names.json").write_text(
        json.dumps({"oc_team": "产品讨论"}, ensure_ascii=False), encoding="utf-8")
    result = lark.message_reference(
        str(tmp_path),
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_team&messageId=om_2",
        str(cache),
    )
    assert result["ok"] is True
    assert result["source"] == "sync"
    assert result["chat_name"] == "产品讨论"
    assert [x["message_id"] for x in result["messages"]] == [f"om_{i}" for i in range(5)]
    assert [x["message_id"] for x in result["messages"] if x["target"]] == ["om_2"]


def test_current_user_message_is_marked_for_own_bubble(tmp_path):
    _put(str(tmp_path), 1, "om_other", 101)
    path, mine = _put(str(tmp_path), 2, "om_mine", 102)
    mine["sender"]["id"] = "ou_me"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mine, f, ensure_ascii=False)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "lark-self.json").write_text('{"open_id":"ou_me"}', encoding="utf-8")
    result = lark.message_reference(
        str(tmp_path),
        "https://applink.feishu.cn/client/chat/open?openChatId=oc_team&messageId=om_mine",
        str(cache),
    )
    by_id = {item["message_id"]: item for item in result["messages"]}
    assert by_id["om_mine"]["is_self"] is True
    assert by_id["om_other"]["is_self"] is False


def test_message_view_preserves_reactions_for_chat_bubbles():
    item = {
        "message_id": "om_reacted",
        "sender": {"id": "ou_sender", "name": "成员", "sender_type": "user"},
        "content": "收到",
        "reactions": {
            "counts": [{"reaction_type": "OK", "count": 2}],
            "details": [{"emoji_type": "OK", "operator": {"name": "甲"}}],
        },
    }
    view = lark._message_view(item, "om_reacted")
    assert view["sender_id"] == "ou_sender"
    assert view["reactions"]["counts"][0]["reaction_type"] == "OK"
    assert view["target"] is True


def test_online_message_fetch_keeps_default_reaction_enrichment():
    ok = json.dumps({"ok": True, "data": {"messages": [{"message_id": "om_x"}]}})
    with mock.patch.object(lark.subprocess, "run", return_value=mock.Mock(stdout=ok, stderr="")) as run:
        item, error = lark._fetch_message("om_x")
    assert error == ""
    assert item["message_id"] == "om_x"
    assert "--no-reactions" not in run.call_args.args[0]


def test_position_only_link_can_reverse_lookup_cached_record(tmp_path):
    path, _item = _put(str(tmp_path), 8, "om_pos", 208)
    url = "https://applink.feishu.cn/client/chat/open?openChatId=oc_team&position=208"
    with mock.patch.object(lark, "_rg_paths", return_value=[path]):
        result = lark.message_reference(str(tmp_path), url)
    assert result["ok"] is True
    assert result["messages"][0]["message_id"] == "om_pos"
    assert result["messages"][0]["target"] is True


def test_unresolved_link_stays_in_sidebar_with_external_fallback(tmp_path):
    url = "https://applink.feishu.cn/client/message/link/open?token=missing"
    with mock.patch.object(lark, "_rg_paths", return_value=[]):
        result = lark.message_reference(str(tmp_path), url)
    assert result["ok"] is False
    assert result["code"] == "message_unresolved"
    assert result["open_url"] == url


def test_thread_reply_link_highlights_reply_inside_thread(tmp_path):
    path, root = _put(str(tmp_path), 9, "om_root", 209)
    root["thread_replies"] = [
        {
            "message_id": "om_reply",
            "create_time": "2026-09-16 10:10",
            "sender": {"name": "回复者", "sender_type": "user"},
            "msg_type": "text",
            "content": "线程回复",
            "message_app_link": (
                "https://applink.feishu.cn/client/thread/open?"
                "open_thread_id=omt_topic&thread_position=0"
            ),
        }
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(root, f, ensure_ascii=False)
    url = root["thread_replies"][0]["message_app_link"]
    with mock.patch.object(lark, "_rg_paths", return_value=[path]):
        result = lark.message_reference(str(tmp_path), url)
    assert [x["message_id"] for x in result["messages"]] == ["om_root", "om_reply"]
    assert [x["message_id"] for x in result["messages"] if x["target"]] == ["om_reply"]


def test_thread_link_fetches_api_when_sync_cache_misses(tmp_path):
    url = (
        "https://applink.feishu.cn/client/thread/open?"
        "open_chat_id=oc_team&open_thread_id=omt_topic&thread_position=-1"
    )
    remote = [
        {
            "message_id": "om_first",
            "chat_id": "oc_team",
            "thread_id": "omt_topic",
            "thread_message_position": "0",
            "message_app_link": (
                "https://applink.feishu.cn/client/thread/open?"
                "open_thread_id=omt_topic&thread_position=0"
            ),
            "create_time": "2026-09-16 10:10",
            "sender": {"name": "回复者", "sender_type": "user"},
            "msg_type": "text",
            "content": "在线读取的话题消息",
        }
    ]
    with mock.patch.object(lark, "_rg_paths", return_value=[]), \
         mock.patch.object(lark, "_fetch_thread", return_value=(remote, "")) as fetch:
        result = lark.message_reference(str(tmp_path), url)
    fetch.assert_called_once_with("omt_topic")
    assert result["ok"] is True
    assert result["source"] == "api"
    assert result["messages"][0]["message_id"] == "om_first"
    assert result["messages"][0]["target"] is True


def test_feed_route_owns_message_preview_endpoint():
    src = open(os.path.join(LIB, "feed", "api.py"), encoding="utf-8").read()
    assert 'p == "/workbench/lark-message"' in src
    assert "message_reference(sync_root, url" in src
