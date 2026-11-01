"""claude 会话记录的落盘位置:目录名由 cwd 编码而来,但会话被 resume 后不跟着 cwd 走。

回归的是 2026-08-12 查到的一处路径分叉:一段会话在 /Users/alice/work/desktron
起头(终端里的编码会话),之后被 GUI 以 --resume 接着跑,进程 cwd 已经是工作区
/Users/alice/.local/share/desktron/workspace,而引擎仍把新内容追加进出生时那个
目录下的同一个文件。于是同一段会话的记录里两种 cwd 并存,而按工作区编码算出来的目录里
根本没有这个文件。

所以适配器不能只按自己的 cwd 算一次:算不中要按会话 id 全局找一遍(id 是 uuid,跨目录不撞)。
子代理、目标、记录大小这些都从同一条解析结果派生,一处算错则全线落空。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
    import term  # noqa: E402
finally:
    sys.argv = _argv

WORKSPACE = "/Users/somebody/.local/share/desktron/workspace"
SRCREPO = "/Users/somebody/work/desktron"      # 会话出生的目录(终端里的编码会话)
ESID = "ed6d6e9e-2020-4f58-89a2-5750d28db8a4"
SID = "adp_test0001"


class ProjEncoding(unittest.TestCase):
    """目录名编码:非字母数字一律换成 -,不合并连续分隔符。"""

    def test_encoding_is_one_to_one(self):
        self.assertEqual(term._claude_proj_enc(WORKSPACE),
                         "-Users-somebody--local-share-desktron-workspace")
        self.assertEqual(term._claude_proj_enc(SRCREPO), "-Users-somebody-work-desktron")

    def test_underscore_is_encoded_too(self):
        # 曾只换 /、. 而漏了 _,含下划线的项目路径因此算错目录
        self.assertEqual(term._claude_proj_enc("/a/b_c"), "-a-b-c")


class TranscriptPath(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="claude-home-")
        self.data = tempfile.mkdtemp(prefix="claude-data-")
        self._exp = os.path.expanduser
        os.path.expanduser = (lambda p: p.replace("~", self.home, 1)
                              if isinstance(p, str) and p.startswith("~") else self._exp(p))
        self.ad = ea.ClaudeAdapter(self.data, WORKSPACE, ea.EventBus())
        self.ad.index = {SID: {"engine_sid": ESID}}

    def tearDown(self):
        os.path.expanduser = self._exp
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def _projects(self, wd):
        d = os.path.join(self.home, ".claude", "projects", term._claude_proj_enc(wd))
        os.makedirs(d, exist_ok=True)
        return d

    def _put(self, wd, esid=ESID, lines=("{}",)):
        d = self._projects(wd)
        path = os.path.join(d, esid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_hits_workspace_dir_first(self):
        want = self._put(WORKSPACE)
        self.assertEqual(self.ad._transcript_path(ESID), want)

    def test_falls_back_to_birth_dir(self):
        """按工作区算不中时,按会话 id 全局找——记录留在会话出生的那个目录里。"""
        want = self._put(SRCREPO)
        self.assertEqual(self.ad._transcript_path(ESID), want)

    def test_workspace_dir_wins_over_others(self):
        want = self._put(WORKSPACE)
        self._put(SRCREPO)
        self.assertEqual(self.ad._transcript_path(ESID), want)

    def test_absent_returns_workspace_guess(self):
        """两处都没有:回工作区那个路径,调用方按「文件不存在」处理。"""
        self.assertEqual(self.ad._transcript_path(ESID),
                         os.path.join(self.home, ".claude", "projects",
                                      term._claude_proj_enc(WORKSPACE), ESID + ".jsonl"))

    def test_stat_follows_fallback(self):
        path = self._put(SRCREPO)
        st = self.ad.transcript_stat(SID)
        self.assertEqual(st.get("size"), os.path.getsize(path))

    def test_subagents_follow_fallback(self):
        """子代理目录挂在记录旁边:记录找错地方,派出去的子代理就一个也列不出来。"""
        self._put(SRCREPO)
        d = os.path.join(self._projects(SRCREPO), ESID, "subagents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent-a19c0691.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-08-12T09:20:28.315Z",
                                "message": {"content": "查 rollout 路径归属错位"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"}]}}) + "\n")
        with open(os.path.join(d, "agent-a19c0691.meta.json"), "w", encoding="utf-8") as f:
            json.dump({"agentType": "general-purpose", "toolUseId": "toolu_01KPc3Lx",
                       "description": "查 rollout 路径归属错位"}, f)
        got = self.ad.subagents(SID)
        self.assertEqual([x["id"] for x in got], ["a19c0691"])
        self.assertEqual(got[0]["kind"], "general-purpose")
        self.assertEqual(got[0]["task"], "查 rollout 路径归属错位")
        self.assertEqual(got[0]["last"], "调用 Bash")

    def test_unknown_session_yields_nothing(self):
        self.ad.index = {}
        self.assertEqual(self.ad.subagents(SID), [])
        self.assertEqual(self.ad.transcript_stat(SID), {})


if __name__ == "__main__":
    unittest.main()
