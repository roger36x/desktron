"""Claude 长期令牌：单独存放、只注入 Shepherd Claude、不进入 Bash 工具环境。"""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import claude_token  # noqa: E402

TOKEN = "sk-ant-oat01-" + "x" * 40


def test_write_read_status_and_clear_never_expose_token(tmp_path):
    path = str(tmp_path / "cfg" / "claude-oauth-token")
    assert claude_token.read(path) == "" and claude_token.status(path)["configured"] is False
    claude_token.write("  " + TOKEN + "\n", path)
    assert claude_token.read(path) == TOKEN
    if os.name != 'nt':
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    info = claude_token.status(path)
    assert info["configured"] is True and TOKEN not in repr(info)
    assert claude_token.clear(path) is True and claude_token.read(path) == ""


@pytest.mark.parametrize("bad", ["", "short", "two words-" + "x" * 30, "line\n" + "x" * 30])
def test_invalid_token_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        claude_token.write(bad, str(tmp_path / "t"))


def test_apply_adds_token_keeps_explicit_value_and_reports_corrupt_file(tmp_path):
    path = str(tmp_path / "t")
    assert claude_token.apply({"A": "1"}, path) == {"A": "1"}
    claude_token.write(TOKEN, path)
    assert claude_token.apply({"A": "1"}, path)[claude_token.ENV_NAME] == TOKEN
    assert claude_token.apply({claude_token.ENV_NAME: "explicit"}, path)[claude_token.ENV_NAME] == "explicit"
    Path(path).write_text("not a token\n", encoding="utf-8")
    logged = []
    assert claude_token.ENV_NAME not in claude_token.apply({}, path, log=lambda *a: logged.append(a))
    assert logged and "无效" in logged[0][1] and "not a token" not in logged[0][1]


def test_cli_set_reads_stdin_not_argv(tmp_path, monkeypatch):
    path = tmp_path / "t"
    # ntpath.expanduser uses USERPROFILE, not HOME. Never write the real profile.
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "PYTHONIOENCODING": "utf-8"}
    script = str(LIB / "claude_token.py")
    result = subprocess.run([sys.executable, script, "set"], input=TOKEN, capture_output=True,
                            text=True, encoding='utf-8', env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    saved = tmp_path / ".config/desktron/claude-oauth-token"
    assert saved.read_text(encoding='utf-8').strip() == TOKEN and TOKEN not in result.stdout + result.stderr
    status = subprocess.run([sys.executable, script, "status"], capture_output=True, text=True,
                            encoding='utf-8', env=env, timeout=10)
    assert "已配置" in status.stdout and TOKEN not in status.stdout


def test_token_is_injected_only_for_shepherd_claude_paths():
    shepherd = (LIB / "engine_adapters/shepherd.py").read_text(encoding="utf-8")
    block = shepherd[shepherd.index("'DESKTRON_PUBLIC_ENGINE': self.name}"):]
    block = block[:block.index("def on_process")]
    assert "if binding['engine'] == 'claude':" in block and "claude_token.apply(env" in block
    recap = (LIB / "shepherd/recap.py").read_text(encoding="utf-8")
    assert "if binding['engine'] == 'claude':" in recap and "claude_token.apply(env" in recap
    # 不经由注入所有引擎的通用凭证文件。
    assert "claude-oauth-token" not in (LIB / "credstore.py").read_text(encoding="utf-8")


def _bash_env_body(runner):
    runner_source = (LIB / "shepherd" / runner).read_text(encoding="utf-8")
    assert "claudeBashEnvBody" in runner_source
    source = (LIB / "shepherd/claude_sdk_options.mjs").read_text(encoding="utf-8")
    body = re.search(r"export const claudeBashEnvBody = `(.*?)`;", source, re.S).group(1)
    return body.replace("\\$", "$").replace("\\\\", "\\")


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
@pytest.mark.parametrize("runner", ["claude_resident_runner.mjs", "claude_runner.mjs"])
def test_bash_tool_subprocess_does_not_inherit_token(tmp_path, runner):
    env_file = tmp_path / "bash-env"
    env_file.write_text(_bash_env_body(runner), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "BASH_ENV": str(env_file),
           claude_token.ENV_NAME: TOKEN, "KEEP_ME": "1"}
    out = subprocess.run(["bash", "-c", "env"], capture_output=True, text=True, env=env, timeout=10).stdout
    assert TOKEN not in out and claude_token.ENV_NAME not in out
    assert "KEEP_ME=1" in out


FAKE_CLAUDE = r'''#!{python}
import json, os, sys, time
TOKEN = os.environ.get("FAKE_TOKEN", "")
if sys.argv[1:2] == ["setup-token"]:
    assert "CLAUDE_CONFIG_DIR" not in os.environ and "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    sys.stdout.write("\x1b[2J\x1b[HWelcome. Press Enter to continue\r\n"); sys.stdout.flush()
    sys.stdin.readline()
    sys.stdout.write("Browser didn't open? Use the url below to sign in\r\nhttps://claude.ai/oauth/authorize?code=x\r\n")
    if os.environ.get("FAKE_MODE") == "deny":
        time.sleep(30); sys.exit(1)
    if os.environ.get("FAKE_MODE") == "chunked":
        split = len(TOKEN) - 5
        sys.stdout.write("Your OAuth token (valid for 1 year):\r\n" + TOKEN[:split]); sys.stdout.flush()
        time.sleep(0.3)
        sys.stdout.write(TOKEN[split:] + "\r\nStore this token securely.\r\n"); sys.stdout.flush()
        time.sleep(30)
    half = len(TOKEN) // 2
    sys.stdout.write("\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m\r\n"
                     "Your\x1b[1COAuth\x1b[1Ctoken\x1b[1C(valid\x1b[1Cfor\x1b[1C1\x1b[1Cyear):\r\n\x1b[33m" + TOKEN[:half] + "\x1b[39m\r\n  \x1b[33m"
                     + TOKEN[half:] + "\x1b[39m\r\nStore this token securely. You won't be able to see it again.\r\n")
    sys.stdout.flush()
    if os.environ.get("FAKE_MODE") == "stubborn":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:   # 不理会 SIGTERM，并持续输出
            sys.stdout.write("spinner\r\n"); sys.stdout.flush(); time.sleep(0.01)
    time.sleep(30)
else:
    ok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == TOKEN and os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR", "/x"))
    print(json.dumps({"subtype": "success" if ok else "error", "is_error": not ok,
                      "result": "OK" if ok else "Failed to authenticate " + TOKEN}))
'''


def _fake(tmp_path, monkeypatch, mode="ok"):
    if os.name == 'nt':
        pytest.skip('POSIX PTY and executable-shebang fixture; Windows set/status covered separately')
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE.replace("{python}", sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_TOKEN", TOKEN)
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "desk-session-config"))
    return str(exe)


def test_extract_token_handles_ink_cursor_moves_instead_of_spaces():
    # 2026-10-09 真实输出：Ink 以光标右移代替空格，旧匹配因此失败，已生成的令牌未能保存。
    gap = "\x1b[1C"
    screen = ("\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m\r\n"
              + gap.join(["Your", "OAuth", "token", "(valid", "for", "1", "year):"]) + "\r\n"
              + "\x1b[33m" + TOKEN + "\x1b[39m\r\n"
              + gap.join(["Store", "this", "token", "securely."]) + "\x1b(B\r\n")
    assert claude_token.extract_token(screen) == TOKEN
    tail = claude_token._redacted_tail(screen)
    assert TOKEN not in tail and "Your OAuth token (valid for 1 year):" in tail and "(B" not in tail


def test_extract_token_falls_back_only_after_complete_output_boundary():
    screen = "token created\n\x1b[33m" + TOKEN + "\x1b[39m\n"
    assert claude_token.extract_token(screen) == ""
    assert claude_token.extract_token(screen + "Store this token securely.") == TOKEN


@pytest.mark.parametrize("header", ["", "Your OAuth token (valid for 1 year):\n"])
def test_extract_token_never_accepts_a_partial_pty_chunk(header):
    screen = header + TOKEN[:-5]
    assert claude_token.extract_token(screen) == ""
    screen += TOKEN[-5:] + "\nStore this token secu"
    assert claude_token.extract_token(screen) == ""
    assert claude_token.extract_token(screen + "rely.") == TOKEN


def test_windows_auto_login_reports_unsupported_before_launch(monkeypatch):
    monkeypatch.setattr(claude_token.oscompat, 'IS_WINDOWS', True)
    def must_not_launch(*args):
        pytest.fail('unsupported PTY login must not resolve or launch Claude')
    monkeypatch.setattr(claude_token, '_claude_binary', must_not_launch)
    with pytest.raises(RuntimeError, match='POSIX PTY'):
        claude_token.login()


def test_login_waits_for_the_rest_of_a_chunked_token(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, mode="chunked")
    path = str(tmp_path / "saved")
    assert claude_token.login(exe, timeout=10, path=path, say=lambda _m: None)
    assert claude_token.read(path) == TOKEN


def test_write_does_not_reuse_an_existing_fixed_temporary_file(tmp_path):
    path = tmp_path / "saved"
    old_tmp = tmp_path / "saved.tmp"
    old_tmp.write_text("unrelated", encoding="utf-8")
    old_tmp.chmod(0o644)
    claude_token.write(TOKEN, str(path))
    assert old_tmp.read_text(encoding="utf-8") == "unrelated"
    assert claude_token.read(str(path)) == TOKEN
    if os.name != 'nt':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".claude-oauth-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_write_does_not_follow_an_existing_fixed_temporary_symlink(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("untouched", encoding="utf-8")
    (tmp_path / "saved.tmp").symlink_to(target)
    claude_token.write(TOKEN, str(tmp_path / "saved"))
    assert target.read_text(encoding="utf-8") == "untouched"
    assert claude_token.read(str(tmp_path / "saved")) == TOKEN


def test_extract_token_joins_wrapped_colored_output():
    screen = ("Your OAuth token (valid for 1 year):\n\x1b[33m" + TOKEN[:10] + "\x1b[39m\n   "
              + TOKEN[10:] + "\nStore this token securely.")
    assert claude_token.extract_token(screen) == TOKEN
    assert claude_token.extract_token("Your OAuth token (valid for 1 year):\nStore this token securely") == ""


def test_login_saves_token_without_printing_it_then_verify(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    path = str(tmp_path / "saved")
    said = []
    assert claude_token.login(exe, timeout=20, path=path, say=said.append) is True
    assert claude_token.read(path) == TOKEN
    assert TOKEN not in "\n".join(said) and any("https://claude.ai/oauth" in s for s in said)
    ok, message = claude_token.verify(exe, path=path)
    assert ok and TOKEN not in message


def test_login_timeout_reports_redacted_tail(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, mode="deny")
    path = str(tmp_path / "saved")
    said = []
    assert claude_token.login(exe, timeout=3, path=path, say=said.append) is False
    assert not os.path.exists(path) and "没有取得令牌" in said[-1]


def test_verify_failure_message_hides_token(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    path = str(tmp_path / "saved")
    claude_token.write("sk-ant-oat01-" + "y" * 40, path)
    ok, message = claude_token.verify(exe, path=path)
    assert not ok and TOKEN not in message and "y" * 30 not in message


def test_token_is_saved_before_cleanup_and_cleanup_is_bounded(tmp_path, monkeypatch):
    # 2026-10-09：取到令牌后先收尾再保存，子进程卡在退出时令牌丢失。现在先保存，收尾有时限。
    import time
    exe = _fake(tmp_path, monkeypatch, mode="stubborn")
    path = str(tmp_path / "saved")
    started = time.monotonic()
    assert claude_token.login(exe, timeout=20, path=path, say=lambda _m: None) is True
    assert claude_token.read(path) == TOKEN
    assert time.monotonic() - started < 15
