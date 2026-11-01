"""Pi Coding Agent 1.0.x RPC 适配器。

RPC 进程按会话常驻；Desk 统一层负责排队、附件、SSE、落盘与异常收尾。Pi 侧以稳定 UUID
写 v3 JSONL，以 agent_settled 作为真正的回合边界。所有工具调用先经过 pi_gate.ts，审批经
extension_ui_request/response 回到 Desk，不依赖 Pi TUI。
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import copy
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from credstore import secrets_env
from .bus import SESSION_WINDOW, strip_scene_envelope
from .cli import CliAdapter
from .model_catalog import openai_model_sort_key
from .persist import _PERSIST, _PERSIST_LOCK, _persist_drop
from engines.stores import pi_session_path
from shepherd.execution_budget import ToolOutputTracker
from pi_reasoning import is_openai_reasoning_provider, is_openai_summary_titles_only
from pi_extensions import (
    append_system_prompt_if_missing, prompt_isolation_args, provider_extension_args,
    subagent_env, subagent_extension_args, web_extension_args, without_legacy_web_secrets,
)


from .pi_subagent_view import PiSubagentViewMixin


PI_SAFETY_TEXT = "命令和输出不得包含凭证、token 或敏感数据。"
PI_DELTA_TEXT = ("Bash 没有 `description` 字段。执行 shell 命令时，第一行写 "
                 "`# <十来个中文字>` 说明目的。" + PI_SAFETY_TEXT)
SUBAGENT_DELIVERY_NOTICE = ("本轮只接收已经完成的只读协作结果。请结合结果完成当前任务，"
                            "不要再次启动子任务。")


def runtime_prompt_rules():
    """供提示词清单核对 Pi 的条件提示；运行时仍按条件选择。"""
    return "\n\n".join((PI_DELTA_TEXT, SUBAGENT_DELIVERY_NOTICE))


class PiAdapter(PiSubagentViewMixin, CliAdapter):
    name = "pi"
    binary = "pi"
    display = "Pi Coding"
    model_label = "pi · 多 provider"
    streams_text = True
    asks_permission = True
    use_stdin_prompt = True
    persist_supported = True
    interrupt_kill = True
    rewind_supported = False
    compact_supported = True
    fork_supported = True
    UI_MODE_DEFAULT = "default"
    THINK = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
    DEFAULT_THINK = "max"
    GATE_PREFIX = "DESKTRON_PERMISSION:"
    PROVIDER_TRACE_PREFIX = "DESKTRON_PROVIDER_TRACE:"
    PROVIDER_WAIT_VISIBLE_SECONDS = 60
    COMPACTION_EXTENSION_ERROR = re.compile(r"^(?:Compaction\b|上下文压缩)", re.I)
    PI_SAFETY = PI_SAFETY_TEXT
    PI_DELTA = PI_DELTA_TEXT
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pi_write_lock = threading.Lock()
        self._pi_spawn_lock = threading.Lock()
        self._pi_processes = {}
        self._pi_provider_fences = {}
        self._pi_provider_trace_fences = {}
        self._pi_provider_trace_epochs = {}
        self._shutting_down = False

    @classmethod
    def _option_provider(cls, opts):
        opts = opts or {}
        model = str(opts.get("model") or "").strip()
        provider = str(opts.get("provider") or "").strip()
        return provider or (model.split("/", 1)[0] if "/" in model else "")

    @classmethod
    def _thinking_for_options(cls, opts):
        opts = opts or {}
        effort = str(opts.get("effort") or "").strip()
        return effort or cls.DEFAULT_THINK

    @staticmethod
    def sanitize_process_env(env):
        return without_legacy_web_secrets(env)

    def grounding(self, sid, first_turn):
        grounding = super().grounding(sid, first_turn)
        if not str((grounding or {}).get("constant") or "").strip():
            raise RuntimeError("Pi 系统约定构造失败，已停止启动")
        return grounding

    def _turn_system_prompt(self, opts, grounding=None):
        from context_delivery import compose_system
        if grounding is None:
            grounding = (opts or {}).get('_grounding')
        return compose_system(self.system_body(opts), {**(opts or {}), '_grounding': grounding},
                              self.PI_DELTA)

    def _spawn_process(self, sid, cmd, **kwargs):
        from owned_process import command, protect
        if kwargs.get('env') is not None:
            kwargs['env'] = self.sanitize_process_env(kwargs['env'])
        # Publication and shutdown share this lock: a late preparation cannot
        # spawn an untracked writer after the shutdown snapshot was taken.
        with self._pi_spawn_lock:
            if self._shutting_down:
                raise RuntimeError('Desk 正在退出，Pi 请求未发送')
            self._pi_processes = {p: s for p, s in self._pi_processes.items()
                                  if p.poll() is None}
            live = {(s, getattr(p, "pid", None)) for p, s in self._pi_processes.items()}
            self._pi_provider_fences = {key: value for key, value in
                                        self._pi_provider_fences.items() if key in live}
            self._pi_provider_trace_fences = {key: value for key, value in
                                              self._pi_provider_trace_fences.items() if key in live}
            self._pi_provider_trace_epochs = {key: value for key, value in
                                              self._pi_provider_trace_epochs.items() if key in live}
            kwargs['start_new_session'] = True
            proc = subprocess.Popen(command(cmd, lock_path=os.path.join(
                self.dir, sid + '.pi-writer.lock')), **kwargs)
            protect(proc)
            self._pi_processes[proc] = sid
            return proc

    def abort(self, sid, recall=False, client_request_id=''):
        from pi_children import cancel_session
        ctx = self._live.get(sid) or {}
        if not client_request_id or client_request_id == ctx.get('client_request_id'):
            cancel_session(self.dir, sid, suppress_pending=True)
        return super().abort(sid, recall=recall, client_request_id=client_request_id)

    def shutdown(self, timeout=4):
        from pi_children import close_for
        with self._pi_spawn_lock:
            if self._shutting_down:
                close_for(self.dir)
                return
            self._shutting_down = True
            processes = list(self._pi_processes.items())
        self._peer_cancel_all()
        for sid, ctx in list(self._live.items()):
            self._checkpoint_live(ctx, force=True)
            self.abort(sid)
        for proc, sid in processes:
            if proc.poll() is None and sid not in self._live:
                self._soft_interrupt(sid, proc)  # Includes standalone /compact.
        deadline = time.monotonic() + max(0, timeout)
        while any(p.poll() is None for p, _ in processes) and time.monotonic() < deadline:
            time.sleep(.05)
        for proc, _ in processes:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # Keep observation active until parent Pi processes have stopped; a late
        # async-started identity can still receive its official stop request.
        close_for(self.dir)
        # No successful compaction or turn is fabricated here. Live snapshots
        # and the original Pi JSONL are recovered on the next explicit request.

    def create_session(self, domain="", task=False, opts=None):
        row = super().create_session(domain=domain, task=task, opts=opts)
        # Pi 允许预设精确 UUID。先钉住，首回合即使在 session 事件之前中断也能准确续接。
        with self._lock:
            self.index[row["id"]]["engine_sid"] = str(uuid.uuid4())
            self.index[row["id"]]["engine_started"] = False
            self._save_index()
            return copy.deepcopy(self.index[row["id"]])

    def engine_has_history(self, sid, engine_sid):
        row = self.index.get(sid) or {}
        if "engine_started" in row:
            return bool(row["engine_started"])
        # 兼容早期索引：真实记录存在才算已经启动，不能仅凭预分配 UUID 推断。
        return bool(engine_sid and pi_session_path(engine_sid))

    def _mark_engine_started(self, sid, esid):
        with self._lock:
            row = self.index.get(sid)
            if row is not None:
                row["engine_started"] = True
        self._set_engine_sid(sid, esid)

    def resident_engine_sids(self):
        with _PERSIST_LOCK:
            sids = [sid for sid, ep in _PERSIST.items() if ep.proc.poll() is None]
        return {esid for esid in (self._esid_of(sid) for sid in sids) if esid}

    def release_resident(self, esid):
        sid = self._sid_by_esid(esid)
        if not sid or sid not in _PERSIST:
            return False
        _persist_drop(sid)
        return True

    def recap_generate(self, sid, run, snap):
        """在临时 Pi 副本上生成 journal recap，不污染原会话且不允许执行工具。

        普通 recap 与主回合使用同一套扩展、工具定义、系统提示和会话号，只把模式换成
        json，并让 pi_gate 拒绝全部工具调用，这样请求前缀与主回合一致，能命中提示缓存。
        旧做法用 --fork、--no-tools 并改写系统提示，工具定义、系统提示和缓存键
        （OpenAI 的 prompt_cache_key 取会话号）都与主回合不同，每次都要全价重读整段会话。
        peer 问询跨越不受信边界，仍按原方式移除全部工具；Shepherd 托管的 Pi 会话主回合
        不经 build_cmd，前缀本来就对不上，也保持原方式。
        """
        esid = str(snap.get("engine_sid") or "")
        path = self._pi_path(esid)
        if not path:
            run.reason = "找不到 Pi 源会话记录"
            return ""
        if (snap.get("source") != getattr(self, "SRC_PEER", "peer")
                and snap.get("_shepherd_source") is None):
            return self._recap_cache_affine(sid, run, snap, path)
        return self._recap_isolated(sid, run, snap, path)

    def _recap_cache_affine(self, sid, run, snap, path):
        from .recap import recap_prompt, RECAP_TIMEOUT, _engine_env
        opts = dict(snap.get("opts") or {})
        if not str(opts.get("_grounding") or "").strip():
            run.reason = "Pi recap 缺少系统约定"
            return ""
        with tempfile.TemporaryDirectory(prefix="desktron-pi-recap-") as session_dir:
            # 原样复制会话文件并用 --session 打开：会话号保持不变，原会话不被写入。
            copy = os.path.join(session_dir, os.path.basename(path))
            shutil.copyfile(path, copy)
            os.chmod(copy, 0o600)
            cmd = self.build_cmd("", "", opts)
            try:
                cmd[cmd.index("--mode") + 1] = "json"
            except (ValueError, IndexError):
                run.reason = "Pi recap 命令缺少 --mode"
                return ""
            cmd += ["--session", copy, "--", recap_prompt(snap)]
            env = _engine_env(self, opts)
            # 扩展在加载时读取会话身份（如子代理扩展）；与主回合一致，但不签发 App Control 能力。
            env.update({"DESKTRON_SESSION_ID": sid, "DESKTRON_ENGINE": self.name,
                        "DESKTRON_PUBLIC_ENGINE": self.name, "DESKTRON_REPO_ROOT": self.workspace,
                        "DESKTRON_PI_RECAP": "1"})
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    cwd=snap.get("cwd") or self.session_cwd(sid), env=env, start_new_session=True)
            except Exception as exc:
                run.reason = "Pi 支线启动失败:" + type(exc).__name__
                return ""
            run.bind(proc)
            result = {}
            done = threading.Event()

            def read():
                # 只取第一条助手完成帧：拿到即结束进程，不让被拒的工具调用引出第二次请求。
                try:
                    for line in proc.stdout:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        message = event.get("message") or {}
                        if event.get("type") == "message_end" and message.get("role") == "assistant":
                            result["message"] = message
                            break
                finally:
                    done.set()

            threading.Thread(target=read, daemon=True).start()
            try:
                if not done.wait(RECAP_TIMEOUT):
                    run.expire(f"Pi 支线超时 {RECAP_TIMEOUT}s")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                run.unbind(proc)
            if run.cancelled.is_set():
                run.reason = run.reason or "Pi 支线被取消"
                return ""
            message = result.get("message")
            if message is None:
                run.reason = "Pi 未返回助手完成帧" + (f"（退出码 {proc.returncode}）" if proc.returncode else "")
                return ""
            usage = message.get("usage") or {}
            if isinstance(usage, dict):
                run.cache_usage = {key: usage.get(key) for key in ("input", "output", "cacheRead", "cacheWrite")
                                   if isinstance(usage.get(key), (int, float)) and not isinstance(usage.get(key), bool)}
            return self._pi_recap_text(message, run)

    def _pi_recap_text(self, message, run):
        """A recap is successful only after a normal, tool-free final frame."""
        if message.get("stopReason") == "error" or str(message.get("errorMessage") or "").strip():
            run.reason = "Pi 服务端错误"
            return ""
        if self._pi_has_tools(message):
            run.reason = "Pi recap 试图调用工具，已拒绝"
            return ""
        if not self._pi_is_final_answer(message):
            run.reason = "Pi recap 未正常结束"
            return ""
        answer = self._pi_text(message.get("content") or []).strip()
        if not answer:
            run.reason = "Pi 助手完成帧没有正文"
        return answer

    def _recap_isolated(self, sid, run, snap, path):
        """peer 问询与 Shepherd：在无工具分支上作答。"""
        from .recap import recap_prompt, RECAP_TIMEOUT, _engine_env
        opts = dict(snap.get("opts") or {})
        with tempfile.TemporaryDirectory(prefix="desktron-pi-recap-") as session_dir:
            peer = snap.get("source") == getattr(self, "SRC_PEER", "peer")
            fork_path = path
            if peer:
                # Freeze the append-only source before the target user turn is
                # admitted. The background model never reads the live JSONL.
                fork_path = os.path.join(session_dir, "source.jsonl")
                shutil.copyfile(path, fork_path)
                os.chmod(fork_path, 0o600)
                run.mark_prepared()
            cmd = [self.binary, "--mode", "json", "--fork", fork_path,
                   "--session-dir", session_dir, "--offline", "--no-extensions",
                   "--no-prompt-templates", "--no-themes", "--no-tools", "--no-approve"]
            cmd += prompt_isolation_args()
            cmd += provider_extension_args()
            model = str(opts.get("model") or "").strip()
            provider = str(opts.get("provider") or "").strip()
            if model:
                cmd += ["--model", model]
            if provider and "/" not in model:
                cmd += ["--provider", provider]
            effort = self._thinking_for_options(opts)
            if effort in self.THINK:
                cmd += ["--thinking", effort]
            body = self.system_body(opts)
            grounding = str(opts.get("_grounding") or "").strip()
            if not grounding:
                run.reason = "Pi recap 缺少系统约定"
                return ""
            body = (body + "\n\n" + grounding) if body else grounding
            # recap 没有工具，只保留数据边界；不要把 Bash 用法带进一个 --no-tools 回合。
            import prompt_locale
            safety = prompt_locale.text(self.PI_SAFETY, prompt_locale.of(opts))
            body = (body + "\n\n" + safety) if body else safety
            cmd += ["--append-system-prompt", body, "--", recap_prompt(snap)]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    cwd=snap.get("cwd") or self.session_cwd(sid),
                    env=_engine_env(self, opts), start_new_session=True)
            except Exception as exc:
                run.reason = "Pi 支线启动失败:" + type(exc).__name__
                return ""
            run.bind(proc)
            stdout = stderr = ""
            try:
                try:
                    stdout, stderr = proc.communicate(timeout=RECAP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    run.expire(f"Pi 支线超时 {RECAP_TIMEOUT}s")
                    try:
                        _out, stderr = proc.communicate(timeout=5)
                    except Exception:
                        pass
                    return ""
            finally:
                run.unbind(proc)
            if run.cancelled.is_set():
                run.reason = run.reason or "Pi 支线被取消"
                return ""
            if proc.returncode not in (0, None):
                run.reason = f"Pi 支线退出码 {proc.returncode}"
                return ""
            answer = ""
            assistant_end = False
            for line in (stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                message = event.get("message") or {}
                if event.get("type") != "message_end" or message.get("role") != "assistant":
                    continue
                assistant_end = True
                usage = message.get("usage") or {}
                if isinstance(usage, dict):
                    run.cache_usage = {key: usage.get(key) for key in ("input", "output", "cacheRead", "cacheWrite")
                                       if isinstance(usage.get(key), (int, float)) and not isinstance(usage.get(key), bool)}
                error_message = str(message.get("errorMessage") or "").strip()
                if message.get("stopReason") == "error" or error_message:
                    run.reason = "Pi 服务端错误"
                    return ""
                text = self._pi_text(message.get("content") or [])
                if text:
                    answer = text
            if not answer.strip():
                run.reason = ("Pi 助手完成帧没有正文" if assistant_end
                              else "Pi 未返回助手完成帧")
            return answer.strip()

    # Pi 的模型接口只给服务商代号。常见品牌写法固定在这里，其余按代号逐词首字母大写，
    # 与 Pi 自己 /login 列表里的名称一致(如 qwen-token-plan-individual → Qwen Token Plan Individual)。
    def native_writer_lock(self, sid):
        return os.path.join(self.dir, str(sid) + '.pi-writer.lock')

    PROVIDER_LABELS = {"openai": "OpenAI", "openrouter": "OpenRouter", "deepseek": "DeepSeek",
                       "anthropic": "Anthropic", "google": "Google", "xai": "xAI",
                       "github-copilot": "GitHub Copilot", "zai": "Z.ai", "moonshot": "Moonshot"}

    @classmethod
    def provider_label(cls, provider):
        provider = str(provider or "")
        return cls.PROVIDER_LABELS.get(provider) or " ".join(
            part[:1].upper() + part[1:] for part in re.split(r"[-_]+", provider) if part)

    @staticmethod
    def _openai_model_sort_key(model):
        """Order GPT generations and named releases without trusting registry insertion order."""
        return openai_model_sort_key(model)

    @classmethod
    def _ordered_model_catalog(cls, rows):
        """Sort OpenAI accounts in place while preserving provider placement and unknown ties."""
        ordered = list(rows)
        provider_slots = {}
        for index, model in enumerate(ordered):
            provider = str(model.get("provider") or "")
            if provider == "openai":
                provider_slots.setdefault(provider, []).append(index)
        for slots in provider_slots.values():
            models = sorted((ordered[index] for index in slots), key=cls._openai_model_sort_key)
            for index, model in zip(slots, models):
                ordered[index] = model
        return ordered

    def options_catalog(self):
        import settings
        current = settings._pi_current_model()
        labels = {level: level.title() for level in self.THINK}
        models = []
        for model in self._ordered_model_catalog(settings._pi_model_catalog()):
            levels = [x for x in (model.get("thinking_levels") or ["off"]) if x in self.THINK]
            efforts = [{"id": level, "label": labels[level]} for level in levels]
            # New sessions default to the highest advertised level; a saved
            # per-session effort or an explicit user selection still wins later.
            effort = levels[-1]
            option = {"id": model["id"],
                      "label": model.get("name") or model["id"],
                      "provider": model.get("provider") or "",
                      "providerLabel": self.provider_label(model.get("provider")),
                      "default": model["id"] == current,
                      "defaultEffort": effort,
                      "efforts": efforts,
                      "input": model.get("input") or [],
                      "contextWindow": model.get("context_window") or 0}
            models.append(option)
        modes = [
            {"id": "default", "label": "manual mode"},
            {"id": "acceptEdits", "label": "auto-accept edit mode"},
            {"id": "bypassPermissions", "label": "bypass permissions mode"},
        ]
        return {"models": models, "modes": modes, "sandboxes": [], "ui": self.ui_contract()}

    def normalize_options(self, opts):
        out = super().normalize_options(opts)
        # Pi 按当前模型的 contextWindow 与原生 reserveTokens 自动计算压缩点。
        # 清除旧会话遗留的 Desk 绝对窗口，避免它继续影响进程指纹或误导界面。
        out.pop("autocompact", None)
        return out

    def config_label(self, sid=None):
        import settings
        model = self.session_model(sid) if sid else (self.last_model or settings._pi_current_model())
        return f"pi · {model}" if model else "pi"

    def env_notice(self):
        import settings
        state = settings._pi_credentials()
        if state.get("logged_in") or state.get("unknown"):
            return None
        return {"state": "warning", "stage": "auth", "label": "Pi 尚无可用 provider",
                "details": ["没有探测到可调用模型的凭证。",
                            "打开终端运行 pi，在交互界面输入 /login；也可在本机凭证文件配置 API key。"],
                "action": {"label": "去终端登录", "kind": "terminal"}}

    @staticmethod
    def _persist_key(cmd):
        # 活进程已经绑定会话；首轮的 --session-id 与重连时的 --session path 不应让第二轮
        # 被误判为选项变化。系统提示正文不放进 key，但摘要必须在：约定真实变化时，下一回合
        # 换一个进程使新提示生效；正文不变时仍稳定复用。
        drop_pairs = ("--resume", "--session", "--session-id")
        out, index = [], 0
        while index < len(cmd):
            arg = cmd[index]
            if arg in drop_pairs:
                index += 2
                continue
            if arg == "--append-system-prompt" and index + 1 < len(cmd):
                digest = hashlib.sha256(str(cmd[index + 1]).encode("utf-8")).hexdigest()
                out.extend((arg, digest))
                index += 2
                continue
            out.append(arg)
            index += 1
        return tuple(out)

    def build_cmd(self, text, engine_sid, opts):
        opts = opts or {}
        # 只显式加载 Desk 自己的扩展:pi_gate 是权限/数据闸;pi_harness_loop 是面向
        # 发散型模型的动作循环与 verify 硬闸,内部按 DESKTRON_PI_HARNESS_MODELS 门控。
        harness_dir = os.path.dirname(__file__)
        cmd = [self.binary, "--mode", "rpc", "--offline", "--no-extensions",
               "--extension", os.path.join(harness_dir, "pi_gate.ts"),
               "--extension", os.path.join(harness_dir, "pi_harness_loop.ts"),
               "--extension", os.path.join(harness_dir, "pi_autocompact.ts"),
               "--no-approve"]
        cmd += prompt_isolation_args()
        cmd += provider_extension_args()
        web_env = secrets_env(os.environ)
        cmd += web_extension_args(web_env)
        if not opts.get('_subagent_delivery'):
            cmd += subagent_extension_args()
        path = pi_session_path(engine_sid) if engine_sid else ""
        if path:
            cmd += ["--session", path]
        elif engine_sid:
            cmd += ["--session-id", engine_sid]
        model = str(opts.get("model") or "").strip()
        provider = str(opts.get("provider") or "").strip()
        if model:
            cmd += ["--model", model]
        if provider and "/" not in model:
            cmd += ["--provider", provider]
        selected_provider = provider or (model.split("/", 1)[0] if "/" in model else "")
        selected_model = model.split("/", 1)[1] if "/" in model else model
        effort = self._thinking_for_options(opts)
        if effort in self.THINK:
            cmd += ["--thinking", effort]
        body = self._turn_system_prompt(opts, opts.get("_grounding"))
        if opts.get('_subagent_delivery'):
            import prompt_locale
            body += "\n\n" + prompt_locale.text(SUBAGENT_DELIVERY_NOTICE, prompt_locale.of(opts))
        # 预热是显式选择的端点优化，不因安装 Pi 或设置 Ollama provider 自动启用。
        warm_enabled = os.environ.get("PI_KV_WARMUP", "").strip().lower() in {"1", "true", "on", "yes"}
        warm_provider = os.environ.get("PI_KV_WARM_PROVIDER", "").strip()
        warm_model = os.environ.get("PI_KV_WARM_MODEL", "").strip()
        if (warm_enabled and warm_provider and warm_model
                and selected_provider == warm_provider and selected_model == warm_model):
            cmd += ["--extension", os.path.join(harness_dir, "pi_kv_warmup.ts"),
                    "--desk-kv-warm-provider", warm_provider,
                    "--desk-kv-warm-model", warm_model]
        cmd += ["--append-system-prompt", body]
        return cmd

    @staticmethod
    def _pi_user_content(content):
        """Pi 历史重建时摘掉情景前缀，不改动同一消息里的附件。"""
        if isinstance(content, str):
            return strip_scene_envelope(content)
        if not isinstance(content, list):
            return content
        out, first_text = [], True
        for block in content:
            if first_text and isinstance(block, dict) and block.get("type") == "text":
                block = {**block, "text": strip_scene_envelope(block.get("text") or "")}
                first_text = False
            out.append(block)
        return out

    def spawn_env(self, opts):
        mode = str((opts or {}).get("mode") or self.UI_MODE_DEFAULT)
        if mode not in ("default", "acceptEdits", "bypassPermissions"):
            mode = self.UI_MODE_DEFAULT
        return {"DESKTRON_PI_PERMISSION_MODE": mode, "PI_OFFLINE": "1", **subagent_env(self)}

    def persist_env_key(self, env):
        return str((env or {}).get("DESKTRON_PI_PERMISSION_MODE") or self.UI_MODE_DEFAULT)

    def build_user_frame(self, text, saved):
        images = []
        for kind, _path, _name, media_type, data in saved:
            if kind == "image" and data:
                images.append({"type": "image", "data": data,
                               "mimeType": media_type or "image/png"})
        frame = {"type": "prompt", "message": text or ""}
        if images:
            frame["images"] = images
        return frame

    def _soft_interrupt(self, sid, proc):
        if not proc or proc.poll() is not None or not proc.stdin:
            return False
        if not self._shutting_down:
            return self._send_interrupt(proc)
        # A full stdin pipe must not hold the quit handler before it can arm
        # its hard fallback. Admission is sealed: a delayed abort cannot reach
        # a subsequent turn. Ordinary Esc retains its existing ordering.
        threading.Thread(target=self._send_interrupt, args=(proc,), daemon=True).start()
        return True

    def _send_interrupt(self, proc):
        try:
            with self._pi_write_lock:
                proc.stdin.write(json.dumps({"type": "abort"}) + "\n")
                proc.stdin.flush()
            # Abort must also release dialogs created before signal-aware gates
            # were loaded. Never approve a pending tool as part of interruption.
            with self._plock:
                pending = [key for key, value in self._approvals.items() if value[0] is proc]
            for request_id in pending:
                self.respond_permission(request_id, False, resolution="cancelled")
            return True
        except Exception:
            return False

    # ---- 事件翻译 ----

    def _pid(self, ctx, idx):
        return "%s_m%d_c%d" % (ctx["msg_id"], ctx.get("_pi_msg", 0), int(idx or 0))

    def handle_event(self, obj, ctx):
        from .lifecycle import _current
        if not _current(self, ctx):
            return
        kind = obj.get("type") or ""
        ctx.setdefault("_progress_at", float(ctx.get("t0") or 0) / 1000 or time.time())
        if kind == "tool_execution_start":
            call_id = str(obj.get("toolCallId") or "")
            if self._pi_tool_output_tracker(ctx).start(call_id):
                ctx["_progress_at"] = time.time()
        elif kind == "tool_execution_end":
            call_id = str(obj.get("toolCallId") or "")
            if self._pi_tool_output_tracker(ctx).end(call_id):
                ctx["_progress_at"] = time.time()
        if kind == "response":
            self._pi_response(obj, ctx)
        elif kind == "message_start":
            message = obj.get("message") or {}
            if message.get("role") == "assistant":
                ctx["_pi_msg"] = ctx.get("_pi_msg", 0) + 1
                ctx["_pi_in_tool_message"] = False
                ctx["_pi_text_parts"] = []
                ctx["_pi_message_provider"] = str(message.get("provider") or "")
                ctx["_pi_openai_thinking"] = {}
                # overflow 之后的新 assistant 是恢复尝试。保留待 settled 的 CONTEXT，
                # 但清掉上一次请求的 Provider 槽，防止新出现的无结构错误继承旧 code。
                if ctx.get("_pi_pending_context_failure") or ctx.get("_request_failure"):
                    ctx.pop("_pi_provider_failure", None)
                self._pi_model(ctx, message)
        elif kind == "message_update":
            self._pi_stream(ctx, obj.get("assistantMessageEvent") or {})
        elif kind == "message_end":
            message = obj.get("message") or {}
            if message.get("role") == "assistant":
                self._pi_flush_openai_thinking(ctx, message.get("content"))
                content = message.get("content")
                has_content = bool(content.strip()) if isinstance(content, str) else any(
                    isinstance(block, dict) and (block.get("type") == "toolCall" or
                    isinstance(block.get("text") or block.get("thinking"), str)
                    and bool(str(block.get("text") or block.get("thinking") or "").strip()))
                    for block in (content or []))
                if has_content and message.get("stopReason") not in ("error", "aborted") \
                        and not message.get("errorMessage"):
                    ctx["_progress_at"] = time.time()
                if self._pi_has_tools(message):
                    self._pi_mark_commentary(ctx)
                elif self._pi_is_final_answer(message):
                    self._pi_commit_answer(ctx)
                self._pi_model(ctx, message)
                if message.get("stopReason") in ("error", "aborted") or message.get("errorMessage"):
                    if message.get("stopReason") != "aborted" and not ctx.get("aborted"):
                        self._pi_discard_failed_message_parts(ctx)
                    # CONTEXT 是 Pi 会在 agent_end 后自行压缩并重试的暂态。Provider 控制帧
                    # 已把原生 code 放进 ctx；先保留它，等 agent_settled 再决定是否终态化。
                    self._pi_failed(ctx, message.get("errorMessage") or "Pi 模型返回错误")
                elif message.get("stopReason") in ("length", "max_tokens"):
                    self._pi_output_limited(ctx, message)
                else:
                    self._pi_recovered(ctx)
        elif kind == "tool_execution_start":
            self._pi_tool_run(ctx, obj)
        elif kind == "tool_execution_update":
            self._pi_tool_update(ctx, obj)
        elif kind == "tool_execution_end":
            self._pi_tool_done(ctx, obj)
        elif kind == "turn_end":
            self._pi_usage(ctx, obj)
        elif kind == "auto_retry_start":
            from .lifecycle import retry_update
            try:
                attempt = max(0, int(obj.get("attempt") or 0))
                maximum = max(0, int(obj.get("maxAttempts") or 0))
                delay = max(0, int(obj.get("delayMs") or 0))
            except (TypeError, ValueError):
                return
            retry_update(self, ctx, {
                "provider": "pi", "source": "native", "signal": kind,
                "attempt": attempt, "max": maximum, "delay_ms": delay,
                "deadline_at": int(time.time() * 1000) + delay,
                "error": str(obj.get("errorMessage") or "")[:300]}, scope="request")
        elif kind == "queue_update":
            # Native follow-ups are not Desk's pending-send queue. Never copy
            # their text into a second transcript or dispatch them from Desk.
            ctx["native_queue"] = {"steering": len(obj.get("steering") or []),
                                   "follow_up": len(obj.get("followUp") or [])}
            self._checkpoint_live(ctx, force=True)
        elif kind == "summarization_retry_scheduled":
            from .lifecycle import retry_update
            retry_update(self, ctx, {
                "source": "native", "signal": kind, "provider": "pi",
                "attempt": max(0, int(obj.get("attempt") or 0)),
                "max": max(0, int(obj.get("maxAttempts") or 0)),
                "delay_ms": max(0, int(obj.get("delayMs") or 0)),
                "deadline_at": int(time.time()*1000) + max(0, int(obj.get("delayMs") or 0))},
                scope="summarization")
        elif kind in ("summarization_retry_attempt_start", "summarization_retry_finished"):
            from .lifecycle import retry_clear
            retry_clear(self, ctx, scope="summarization")
        elif kind == "auto_retry_end":
            from .lifecycle import retry_clear
            # End of retry is not end of the user turn. Even success=false may
            # be cancellation; only agent_settled releases the RPC turn.
            retry_clear(self, ctx)
        elif kind == "extension_ui_request":
            self._pi_ui_request(ctx, obj)
        elif kind == "extension_error":
            err = str(obj.get("error") or "Pi 扩展异常")[:240]
            # Desk 显式加载多份扩展：pi_gate 才是权限/数据闸；pi_autocompact 的摘要错误
            # 属于压缩阶段，常见根因是 provider 额度、限流、网络或上下文问题。不能把它
            # 展示成权限闸异常，否则用户会误以为是审批/安全策略坏了。
            if self.COMPACTION_EXTENSION_ERROR.search(err):
                failure = {"source": "engine", "code": "COMPACTION_EXTENSION_ERROR",
                           "message": err}
                ctx["_pi_compaction_error"] = err
                ctx["_pi_compaction_failure"] = failure
                self._ctx_env_publish(ctx, {"state": "error", "stage": "compaction",
                                            "label": "上下文压缩失败", "details": [err],
                                            "error": failure})
                return
            # 权限闸扩展异常后若继续运行，后续工具会绕过准入；因此本回合必须
            # fail-closed，而不是只画一条告警。
            ctx["engine_error"] = err
            ctx["engine_failure"] = {
                "source": "engine", "code": "PERMISSION_EXTENSION_ERROR", "message": err}
            self._ctx_env_publish(ctx, {"state": "error", "stage": "permission",
                                            "label": "Pi 权限闸异常", "details": [err],
                                            "error": ctx["engine_failure"]})
            proc = ctx.get("proc")
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        elif kind == "compaction_start":
            from .lifecycle import compact_update
            # 摘要请求是回合内的结构维护，不是 assistant 正文请求。Provider 可能先用
            # extension notify 报错，再用 compaction_end 给最终结果；二者必须落在压缩级
            # 状态里，不能写进主回合 engine_error。
            ctx["_pi_compacting"] = True
            ctx.pop("_pi_compaction_error", None)
            ctx.pop("_pi_compaction_failure", None)
            compact_update(self, ctx, {
                "sessionID": ctx["sid"], "running": True, "trigger": obj.get("reason") or "auto",
                "started": int(time.time() * 1000)})
        elif kind == "compaction_end":
            from .session_naming import invalidate
            from .lifecycle import compact_update
            ctx.pop("_pi_compacting", None)
            notified = str(ctx.pop("_pi_compaction_error", "") or "").strip()
            failure = ctx.pop("_pi_compaction_failure", None)
            failure = failure if isinstance(failure, dict) else None
            status = str(obj.get("status") or "").lower()
            raw_error = obj.get("errorMessage") or obj.get("error")
            if isinstance(raw_error, dict):
                if not failure:
                    failure = {"source": "engine",
                               "code": str(raw_error.get("code") or "UNKNOWN").upper(),
                               "message": str(raw_error.get("message") or raw_error.get("code") or "")[:300]}
                raw_error = raw_error.get("message") or raw_error.get("code") or ""
            completed = (status == "completed" or
                         (not status and obj.get("result") is not None))
            aborted = bool(obj.get("aborted")) or status in ("aborted", "declined")
            # 明确成功优先于同一次请求早先发出的 Provider notify；它可能只是内部重试
            # 的中间错误。没有成功证据时才用暂存通知补足旧协议的 errorMessage。
            error = str(raw_error or ("" if completed or aborted else notified) or "").strip()[:300]
            if not completed and not aborted and not error:
                error = "原生压缩没有返回成功凭据"
            failed = not completed and (status == "failed" or bool(error))
            will_retry = bool(obj.get("willRetry"))
            if completed and not failed and not aborted:
                invalidate(self, ctx["sid"])
            compact_props = {
                "sessionID": ctx["sid"], "running": False, "trigger": obj.get("reason") or "auto",
                "result": ("retrying" if will_retry else "failed" if failed else
                           "noop" if status == "declined" else "cancelled")
                          if failed or aborted else obj.get("result") or {},
                "error": error}
            if failure and (failed or aborted):
                compact_props["failure"] = failure
            compact_update(self, ctx, compact_props)
            if failed:
                ctx["_pi_compaction_failed"] = True
            elif completed and not aborted:
                ctx.pop("_pi_compaction_failed", None)
                self._ctx_env_publish(ctx, {
                    "state": "ready", "stage": "compaction", "label": "上下文压缩已恢复",
                    "resolvedThrough": int(time.time() * 1000)})
        elif kind == "agent_settled":
            self._pi_flush_openai_thinking(ctx)
            # turn_end 是一次模型调用的结束，agent_settled 才是整个用户回合的权威边界。
            # 工具循环可能产生多个 turn_end；最后再发一次，既覆盖完整耗时，也保证 stats
            # 一定先于 idle 到达。即使某版 Pi 的 turn_end 用量形状变化，完成耗时仍不丢。
            # 若 overflow 没有成功产出新的 assistant，Pi 已完成压缩/重试决策，此时才把
            # 暂存的 CONTEXT 失败提升为用户可见终态。
            self._pi_finalize_context_failure(ctx)
            ctx["native_terminal"] = {"source": kind,
                "status": "interrupted" if ctx.get("aborted") else
                          "failed" if ctx.get("engine_error") else "completed"}
            self._pi_stats(ctx)
            ctx["_close_stdin"] = True

    def _pi_response(self, obj, ctx):
        command = obj.get("command") or ""
        if command == "prompt":
            if obj.get("success"):
                esid = self._esid_of(ctx["sid"])
                if esid:
                    ctx["esid"] = esid
                    self._mark_engine_started(ctx["sid"], esid)
            else:
                err = str(obj.get("error") or (obj.get("data") or {}).get("error")
                          or "Pi 拒绝了 prompt")[:300]
                self._pi_failed(ctx, err, final=True)
                ctx["_close_stdin"] = True
        elif command == "get_commands" and obj.get("success"):
            commands = (obj.get("data") or {}).get("commands") or []
            self._save_slash([str(x.get("name")) for x in commands if x.get("name")])

    def _pi_model(self, ctx, message):
        provider = str(message.get("provider") or "")
        model = str(message.get("model") or "")
        if provider:
            ctx['_notice_provider'] = provider
        if model:
            actual = f"{provider}/{model}" if provider else model
            ctx["actual_model"] = actual
            self.set_session_model(ctx.get("sid"), actual, since=ctx.get("t0"))

    def _pi_failed(self, ctx, message, *, final=False):
        """按原生来源码归类失败；展示文案不参与 AUTH/TIMEOUT/ABORTED 判断。"""
        from .lifecycle import request_failed, request_recovered
        existing = ctx.get("engine_failure") or {}
        if existing.get("code") in ("PERMISSION_EXTENSION_ERROR", "PROTOCOL"):
            return  # A later attempt cannot replace a safety/protocol failure.
        raw = str(message or "").strip()[:300]
        notified = str(ctx.get('_pi_notification_error') or "").strip()[:300]
        provider = ctx.get("_pi_provider_failure")
        provider = provider if isinstance(provider, dict) else {}
        code = str(provider.get("code") or "").upper()
        msg = str(provider.get("message") or "").strip()[:300] or raw
        if msg in ("This operation was aborted", "The operation was aborted", "Pi 模型返回错误"):
            msg = notified or msg
        # 只有明确的审批终态加 Provider 原生 ABORTED 才归为审批终止。TIMEOUT/NETWORK/AUTH
        # 等始终保留 Provider 分类；没有结构化来源时也保留原始错误，不猜类别。
        resolution = ctx.get("_pi_permission_resolution")
        approval_abort = resolution in ("timeout", "denied") and code == "ABORTED"
        if approval_abort:
            ctx.pop('_pi_notification_error', None)
            ctx.pop("_pi_engine_failed", None)
            expired = resolution == "timeout"
            ctx["engine_error"] = ("审批已过期，操作未执行" if expired else
                                   "审批已拒绝，操作未执行")
            ctx["engine_failure"] = {
                "source": "permission",
                "code": "APPROVAL_TIMEOUT" if expired else "APPROVAL_DENIED",
                "message": ctx["engine_error"]}
            return
        if not msg:
            return
        if ctx.get("aborted"):
            request_recovered(ctx)
            # Claude/Codex 都以 Desk 已记录的中断意图为准。供应商可能返回英文 abort、
            # 本地化的“请求已取消”或以后新增的文案；它们都不是引擎故障。
            ctx.pop("_pi_engine_failed", None)
            ctx.pop("_pi_pending_context_failure", None)
            ctx.pop("engine_error", None)
            ctx.pop("engine_failure", None)
            ctx.pop("_pi_provider_failure", None)
            return
        if code == "CONTEXT" and not final:
            request_recovered(ctx)  # The latest attempt supersedes an older candidate.
            # Pi 在 message_end 之后才调用 _checkCompaction；此刻发布 session.environment
            # 会把一次可恢复 overflow 提前画成终态并写入 Desk 投影。只保存结构化事实，
            # 成功的重试由 _pi_recovered 清除，失败则由 agent_settled 收口。
            ctx["_pi_pending_context_failure"] = {
                "source": "provider", "provider": "pi",
                "code": "CONTEXT", "message": msg}
            return
        # Latest attempt wins over an earlier overflow, but a request error is
        # not a user-turn terminal. Pi decides retry/compaction before settled.
        ctx.pop("_pi_pending_context_failure", None)
        failure = ({"source": "provider", "provider": "pi", "code": code,
                    "message": msg} if code else
                   {"source": "engine", "code": "UNKNOWN", "message": msg})
        if not final and code != "PROTOCOL":
            request_failed(ctx, failure)
            return
        request_recovered(ctx)
        ctx["engine_error"] = msg
        ctx["_pi_engine_failed"] = True
        ctx["engine_failure"] = failure
        head = self._notice_head(msg)
        self._ctx_env_publish(ctx, {
            "state": "error", "stage": "turn", "label": head,
            "details": [msg] if msg != head else [], "error": failure})

    def _pi_output_limited(self, ctx, message=None):
        """Native stopReason=length is a confirmed but incomplete answer."""
        from shepherd.pi_terminal import output_limit_failure
        code, text = output_limit_failure(message)
        ctx.pop('_pi_notification_error', None)
        ctx.pop("_pi_pending_context_failure", None)
        ctx.pop("_pi_provider_failure", None)
        ctx["engine_error"] = text
        ctx["engine_failure"] = {"source": "engine", "code": code, "message": text}
        self._ctx_env_publish(ctx, {
            "state": "error", "stage": "turn", "label": text,
            "details": [], "error": ctx["engine_failure"]})

    def _pi_finalize_context_failure(self, ctx):
        """Pi settled/Desk closure: commit latest attempt, retaining overflow compatibility."""
        from .lifecycle import commit_request_failure
        candidate = commit_request_failure(ctx)
        if candidate:
            ctx["_pi_provider_failure"] = candidate if candidate.get("source") == "provider" else {}
            self._pi_failed(ctx, candidate["message"], final=True)
            return
        failure = ctx.pop("_pi_pending_context_failure", None)
        if not isinstance(failure, dict):
            return
        if ctx.get("aborted") or ctx.get("engine_error") or ctx.get("engine_failure"):
            ctx.pop("_pi_provider_failure", None)
            return
        ctx["_pi_provider_failure"] = failure
        self._pi_failed(ctx, failure.get("message") or "Pi 上下文超限", final=True)

    def _pi_recovered(self, ctx):
        """重试后重新产出即表示连接恢复，撤掉旧错误且不把本轮误判为失败。"""
        from .lifecycle import retry_clear, request_recovered
        request_recovered(ctx)
        ctx.pop("_native_recovery", None)
        cleared_wait = retry_clear(self, ctx)
        cleared_wait = ctx.pop("_provider_wait", None) is not None or cleared_wait
        if cleared_wait:
            self._checkpoint_live(ctx, force=True)
        ctx.pop('_pi_notification_error', None)
        ctx.pop("_pi_permission_resolution", None)
        ctx.pop("_pi_pending_context_failure", None)
        ctx.pop("_pi_provider_failure", None)
        failure = ctx.get("engine_failure") or {}
        if (failure.get("source") == "permission"
                or failure.get("code") in ("PERMISSION_EXTENSION_ERROR", "PROTOCOL")):
            return
        if not ctx.pop("_pi_engine_failed", False):
            return
        ctx.pop("engine_failure", None)
        ctx.pop("engine_error", None)
        self._ctx_env_publish(ctx, {
            "state": "ready", "stage": "engine", "label": "引擎已恢复"})

    @staticmethod
    def _pi_public_subagent_name(value):
        """受管 Agent 用进程私有名避开项目同名配置；Desk 只持久化公开身份。"""
        if not isinstance(value, str):
            return value
        match = re.fullmatch(
            r"(desk-(?:scout|reviewer|oracle))--desktron-[a-f0-9]{32}", value)
        return match.group(1) if match else value

    @classmethod
    def _pi_tool_input(cls, tool, value):
        """把 Pi 的 path / camelCase 编辑参数投影成 Desk 统一工具契约。"""
        if not isinstance(value, dict):
            return {}
        data = dict(value)
        if str(tool or "").lower() == "subagent" and "agent" in data:
            data["agent"] = cls._pi_public_subagent_name(data["agent"])
        name = str(tool or "").lower()
        if name in ("write", "edit", "multiedit"):
            path = data.get("file_path") or data.get("path") or data.get("filePath")
            if path:
                data["file_path"] = path
        if name in ("edit", "multiedit"):
            if "old_string" not in data and "oldText" in data:
                data["old_string"] = data["oldText"]
            if "new_string" not in data and "newText" in data:
                data["new_string"] = data["newText"]
            edits = data.get("edits")
            if isinstance(edits, list):
                normalized = []
                for edit in edits:
                    if not isinstance(edit, dict):
                        continue
                    item = dict(edit)
                    if "old_string" not in item and "oldText" in item:
                        item["old_string"] = item["oldText"]
                    if "new_string" not in item and "newText" in item:
                        item["new_string"] = item["newText"]
                    normalized.append(item)
                data["edits"] = normalized
        return data

    @staticmethod
    def _pi_has_tools(message):
        return message.get("stopReason") == "toolUse" or any(
            isinstance(block, dict) and block.get("type") == "toolCall"
            for block in (message.get("content") or [])
        )

    @classmethod
    def _pi_is_final_answer(cls, message):
        # Older Pi journals omitted stopReason on completed assistant messages. They are already
        # durable history, so a tool-free/error-free entry is final there; live pending messages
        # never call this predicate before message_end.
        return (message.get("stopReason") in (None, "stop") and not message.get("errorMessage")
                and not cls._pi_has_tools(message))

    def _pi_commit_answer(self, ctx):
        """普通文字流先保守放过程区；模型正常无工具结束后才迁入正文。"""
        for pid in ctx.get("_pi_text_parts") or []:
            part = ctx.get("parts", {}).get(pid)
            if part is None or part.get("type") != "reasoning":
                continue
            part["type"] = "text"
            self._part_replaced(ctx, part)

    def _pi_discard_failed_message_parts(self, ctx):
        """失败终态只保留结构化错误，不把当前未验证模型片段写进 Desk 历史。"""
        prefix = self._pid(ctx, 0).rsplit("_c", 1)[0] + "_c"
        removed = {pid for pid in (ctx.get("order") or []) if pid.startswith(prefix)}
        if not removed:
            return
        # 先用 replace 清空已展示的流片段；随后从 live/final 快照删除。前端在 idle 对账时
        # 只会看到结构化失败，不会把半截文本或未闭合工具调用恢复成会话内容。
        for pid in removed:
            part = ctx.get("parts", {}).get(pid)
            if part is not None:
                blank = {"id": pid, "type": "reasoning", "text": "",
                         "messageID": ctx["msg_id"], "sessionID": ctx["sid"], "replace": True}
                self.bus.publish({"type": "message.part.updated", "properties": {"part": blank}})
            ctx.get("parts", {}).pop(pid, None)
        ctx["order"] = [pid for pid in (ctx.get("order") or []) if pid not in removed]
        ctx["_pi_text_parts"] = [pid for pid in (ctx.get("_pi_text_parts") or []) if pid not in removed]
        tool_parts = ctx.get("_pi_tool") or {}
        for call_id, pid in list(tool_parts.items()):
            if pid in removed:
                tool_parts.pop(call_id, None)
        self._live_snap(ctx, force=True)

    def _pi_mark_commentary(self, ctx):
        # Pi 没有 commentary 通道；同一模型消息带工具调用，文字即本次执行的过程说明。
        # 不按语言或措辞猜思考，也不改写 Pi 原始历史。只调整 Desk 展示位置。
        ctx["_pi_in_tool_message"] = True
        prefix = self._pid(ctx, 0).rsplit("_c", 1)[0] + "_c"
        for pid, part in ctx["parts"].items():
            if pid.startswith(prefix) and part.get("type") == "text":
                part["type"] = "reasoning"
                self._part_updated(ctx, part)

    def _pi_openai_thinking_done(self, ctx, pid, text):
        ctx.setdefault("_pi_openai_thinking", {}).pop(pid, None)
        if not text or is_openai_summary_titles_only(ctx.get("_pi_message_provider"), text):
            return
        part = self._ensure_part(ctx, pid, "reasoning", text=text)
        part["text"] = text
        self._part_updated(ctx, part)

    def _pi_flush_openai_thinking(self, ctx, content=None):
        if not is_openai_reasoning_provider(ctx.get("_pi_message_provider")):
            return
        buffered = ctx.get("_pi_openai_thinking") or {}
        if not buffered:
            return
        blocks = content if isinstance(content, list) else []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict) or block.get("type") != "thinking":
                continue
            pid = self._pid(ctx, index)
            if pid in buffered:
                self._pi_openai_thinking_done(ctx, pid, str(block.get("thinking") or ""))
        for pid, text in list(buffered.items()):
            self._pi_openai_thinking_done(ctx, pid, text)

    def _pi_stream(self, ctx, event):
        kind = event.get("type") or ""
        semantic = (kind in ("toolcall_start", "toolcall_end") or
                    kind in ("text_delta", "thinking_delta", "toolcall_delta")
                    and bool(str(event.get("delta") or "")))
        if semantic:
            ctx["_progress_at"] = time.time()
            from .lifecycle import retry_clear
            cleared = ctx.pop("_provider_queue", None) is not None
            cleared = ctx.pop("_native_recovery", None) is not None or cleared
            cleared = ctx.pop("_provider_wait", None) is not None or cleared
            if not retry_clear(self, ctx) and cleared:
                self._checkpoint_live(ctx, force=True)
        idx = event.get("contentIndex")
        pid = self._pid(ctx, idx)
        if kind in ("text_start", "text_delta"):
            # 是否为最终回答要到 message_end 才确定。此前先进入过程区；只记录普通 text
            # 的块号，原生 thinking 即使正常结束也不会被迁成正文。
            part = self._ensure_part(ctx, pid, "reasoning", text="")
            text_parts = ctx.setdefault("_pi_text_parts", [])
            if pid not in text_parts:
                text_parts.append(pid)
            if kind == "text_delta":
                delta = str(event.get("delta") or "")
                part["text"] = part.get("text", "") + delta
                # Pi stages text in the process area until message_end decides
                # whether tools follow. Preserve that UI rule, but time the
                # native semantic event as text rather than presentation type.
                self._delta(ctx, pid, "reasoning", delta, timing_field="text")
        elif kind in ("thinking_start", "thinking_delta"):
            if is_openai_reasoning_provider(ctx.get("_pi_message_provider")):
                buffered = ctx.setdefault("_pi_openai_thinking", {})
                if kind == "thinking_delta":
                    buffered[pid] = buffered.get(pid, "") + str(event.get("delta") or "")
                else:
                    buffered.setdefault(pid, "")
                return
            part = self._ensure_part(ctx, pid, "reasoning", text="")
            if kind == "thinking_delta":
                delta = str(event.get("delta") or "")
                part["text"] = part.get("text", "") + delta
                self._delta(ctx, pid, "reasoning", delta)
        elif kind in ("text_end", "thinking_end"):
            if kind == "thinking_end" and is_openai_reasoning_provider(
                    ctx.get("_pi_message_provider")):
                buffered = ctx.setdefault("_pi_openai_thinking", {})
                full = event.get("content")
                text = full if isinstance(full, str) else buffered.get(pid, "")
                self._pi_openai_thinking_done(ctx, pid, text)
                return
            part = ctx["parts"].get(pid)
            full = event.get("content")
            if part is not None and isinstance(full, str):
                part["text"] = full
                self._part_updated(ctx, part)
        elif kind == "toolcall_start":
            self._pi_mark_commentary(ctx)
            part = self._ensure_part(ctx, pid, "tool", tool=event.get("toolName") or "tool",
                                     state={"status": "running", "input": {}})
            call_id = str(event.get("id") or "")
            if call_id:
                ctx.setdefault("_pi_tool", {})[call_id] = pid
            self._part_updated(ctx, part)
        elif kind == "toolcall_end":
            self._pi_mark_commentary(ctx)
            call = event.get("toolCall") or {}
            part = self._ensure_part(ctx, pid, "tool", tool=call.get("name") or "tool",
                                     state={"status": "running", "input": {}})
            part["tool"] = call.get("name") or part.get("tool") or "tool"
            if isinstance(call.get("arguments"), dict):
                part["state"]["input"] = self._pi_tool_input(part["tool"], call["arguments"])
            call_id = str(call.get("id") or "")
            if call_id:
                ctx.setdefault("_pi_tool", {})[call_id] = pid
            self._part_updated(ctx, part)

    def _pi_tool_part(self, ctx, obj):
        call_id = str(obj.get("toolCallId") or "")
        pid = (ctx.get("_pi_tool") or {}).get(call_id)
        if not pid:
            pid = ctx["msg_id"] + "_tool_" + (call_id or uuid.uuid4().hex[:10])
            tool = obj.get("toolName") or "tool"
            self._ensure_part(ctx, pid, "tool", tool=tool,
                              state={"status": "running",
                                     "input": self._pi_tool_input(tool, obj.get("args"))})
            if call_id:
                ctx.setdefault("_pi_tool", {})[call_id] = pid
        return ctx["parts"].get(pid)

    @staticmethod
    def _pi_tool_output_tracker(ctx):
        tracker = ctx.get("_pi_tool_output_tracker")
        if not isinstance(tracker, ToolOutputTracker):
            tracker = ctx["_pi_tool_output_tracker"] = ToolOutputTracker()
        return tracker

    def _pi_tool_run(self, ctx, obj):
        part = self._pi_tool_part(ctx, obj)
        if not part:
            return
        part["tool"] = obj.get("toolName") or part.get("tool") or "tool"
        if isinstance(obj.get("args"), dict):
            part["state"]["input"] = self._pi_tool_input(part["tool"], obj["args"])
        part["state"]["status"] = "running"
        ctx["last_tool"] = part["id"]
        self._part_updated(ctx, part)

    # Pi 子代理会按约 100ms 推一次进度；长任务的 partialResult 还是不断增长的正文。
    # 每帧原样送进 SSE 会让手机持续解析 20KB JSON、重算工具签名并强制写 live checkpoint，
    # 数分钟后仅中间帧就可达上百 MB。中间态只用于「正在运行」详情，低频有界预览足够；
    # 终态仍在 _pi_tool_done 一次性保留原来的 20KB，历史展开能力不变。
    TOOL_UPDATE_INTERVAL = 0.75
    TOOL_CHECKPOINT_INTERVAL = 5.0
    TOOL_UPDATE_CAP = 2000

    def _pi_tool_update(self, ctx, obj):
        part = self._pi_tool_part(ctx, obj)
        if not part:
            return
        result = obj.get("partialResult") or {}
        output = self._pi_text(result.get("content") or [])
        call_id = str(obj.get("toolCallId") or "")
        if output:
            # 权威 ctx 与 live checkpoint 保留完整 partial；SSE 仅发有界预览。
            if self._pi_tool_output_tracker(ctx).update(call_id, result):
                ctx["_progress_at"] = time.time()
            part["state"]["output"] = output
        now = time.monotonic()
        checkpoints = ctx.setdefault("_pi_tool_checkpoint_at", {})
        if now - checkpoints.get(part["id"], now - self.TOOL_CHECKPOINT_INTERVAL) \
                >= self.TOOL_CHECKPOINT_INTERVAL:
            checkpoints[part["id"]] = now
            self._live_snap(ctx, force=True)   # 低频写完整 ctx，强退恢复不退化成 UI 预览
        emitted = ctx.setdefault("_pi_tool_update_at", {})
        if now - emitted.get(part["id"], now - self.TOOL_UPDATE_INTERVAL) < self.TOOL_UPDATE_INTERVAL:
            return
        preview_output = str(part["state"].get("output") or "")
        if not preview_output:
            return
        digest = hashlib.sha1(preview_output.encode("utf-8", "replace")).hexdigest()
        signatures = ctx.setdefault("_pi_tool_update_sig", {})
        if signatures.get(part["id"]) == digest:
            return                         # 空 heartbeat 不刷新任何客户端
        signatures[part["id"]] = digest
        emitted[part["id"]] = now
        revisions = ctx.setdefault("_pi_tool_update_rev", {})
        revisions[part["id"]] = revisions.get(part["id"], 0) + 1
        # 进度帧只带身份、状态与有界预览，不重复发送可能很大的 input 等不变字段。
        # 前端专门合并这类帧，不能把最小 state 当成权威 part 覆盖原对象。
        preview = {k: part[k] for k in ("id", "type", "messageID", "sessionID", "tool") if k in part}
        preview["progressOnly"] = True    # 多客户端各自决定：折叠零绘制，展开显示预览
        preview["state"] = {
            "status": "running",
            "output": preview_output[:self.TOOL_UPDATE_CAP],
            "outputMore": len(preview_output) if len(preview_output) > self.TOOL_UPDATE_CAP else 0,
            "progressRevision": revisions[part["id"]],
            "progressTotal": len(preview_output),
        }
        self.bus.publish({"type": "message.part.updated", "properties": {"part": preview}})

    def _pi_tool_done(self, ctx, obj):
        part = self._pi_tool_part(ctx, obj)
        if not part:
            return
        call_id = str(obj.get("toolCallId") or "")
        part["call_id"] = call_id or part["id"]
        if call_id:
            (ctx.get("_pi_tool") or {}).pop(call_id, None)
        (ctx.get("_pi_tool_update_at") or {}).pop(part["id"], None)
        (ctx.get("_pi_tool_update_sig") or {}).pop(part["id"], None)
        (ctx.get("_pi_tool_update_rev") or {}).pop(part["id"], None)
        (ctx.get("_pi_tool_checkpoint_at") or {}).pop(part["id"], None)
        part["state"]["status"] = "error" if obj.get("isError") else "done"
        result = obj.get("result") or {}
        output = self._pi_text(result.get("content") or []) or part["state"].get("output") or ""
        if output:
            part["state"]["output"] = output
            self._bg_note(ctx["sid"], output)
        part["state"].pop("outputMore", None)
        # Pi edit 已在执行时算出真实文件位置。只保留数字，不复制 details 里的 patch/diff；
        # 前端展开即可同步编号，不必再通过 /workbench/fs 回读整份文件。
        details = result.get("details") or {}
        first_line = details.get("firstChangedLine") if isinstance(details, dict) else None
        if not obj.get("isError") and str(part.get("tool") or "").lower() == "edit" \
                and isinstance(first_line, int) and not isinstance(first_line, bool) and first_line > 0:
            part["state"]["line_start"] = first_line
        if ctx.get("last_tool") == part["id"]:
            ctx["last_tool"] = None
        self._part_updated(ctx, part)

    @staticmethod
    def _pi_text(content):
        if isinstance(content, str):
            return content[:20000]
        return "".join(str(block.get("text") or "") for block in (content or [])
                       if isinstance(block, dict) and block.get("type") == "text")[:20000]

    def _pi_usage(self, ctx, obj):
        usage = (obj.get("message") or {}).get("usage") or obj.get("usage") or {}
        current = {
            "total": int(usage.get("totalTokens") or 0),
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
            "cached": int(usage.get("cacheRead") or 0),
            "cache_write": int(usage.get("cacheWrite") or 0),
        }
        # Pi emits one turn_end per provider request, not per Desk user turn. A
        # tool loop can therefore contain several usage frames; overwriting the
        # previous frame under-counts the turn. Accumulate every native request
        # and persist the sum once at agent_settled.
        accumulated = ctx.setdefault("_pi_usage_accum", {
            "total": 0, "input": 0, "output": 0, "cached": 0, "cache_write": 0})
        for key, value in current.items():
            accumulated[key] += value
        ctx["_pi_tokens"] = accumulated["total"]
        ctx["_pi_input_tokens"] = current["input"]
        ctx["_pi_cache_read"] = current["cached"]
        ctx["_token_usage"] = dict(accumulated)
        cost = usage.get("cost") or {}
        current_cost = cost.get("total") if isinstance(cost, dict) else cost
        try:
            ctx["_pi_cost"] = float(ctx.get("_pi_cost") or 0) + float(current_cost or 0)
        except (TypeError, ValueError):
            pass
        # 只记数值和低基数关联字段，不落正文、Prompt、工具参数、凭证或原始 Binding ID。
        # request_seq 区分同一用户回合内的工具续轮；其余字段用于判断缓存失配是否由
        # 模型/Provider/系统前缀变化或 Resident 冷启动导致。
        fence = ctx.get("_provider_wait_fence") or {}
        trace = {
            "ev": "pi.usage", "input": current["input"],
            "cacheRead": current["cached"], "total": current["total"],
        }
        seq = ctx.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
            trace["seq"] = seq
        trace_key = ctx.get("_provider_trace_current")
        provider_request_seq = trace_key[1] if (isinstance(trace_key, tuple) and len(trace_key) == 2
                                               and isinstance(trace_key[1], int)) else None
        request_seq = provider_request_seq
        if not isinstance(request_seq, int) or isinstance(request_seq, bool) or request_seq < 1:
            request_seq = fence.get("request_seq")
        if isinstance(request_seq, int) and not isinstance(request_seq, bool) and request_seq > 0:
            trace["request_seq"] = request_seq
        model = str(ctx.get("actual_model") or ctx.get("_usage_model") or "").strip()
        provider = str(ctx.get("_notice_provider") or ctx.get("_usage_provider") or "").strip()
        if model:
            trace["model"] = model
        if provider:
            trace["provider"] = provider
        for source, target in (("_usage_binding_hash", "binding"),
                               ("_usage_system_hash", "system_hash")):
            value = str(ctx.get(source) or "").strip()
            if value:
                trace[target] = value
        if isinstance(ctx.get("_usage_resident_reused"), bool):
            trace["resident_reused"] = ctx["_usage_resident_reused"]
        self._trace(ctx["sid"], trace)
        # The native turn_end usage has no request identity. Request-level telemetry is
        # written only from the extension's identity-bearing `kind=usage` notification;
        # correlating here would be adjacency rather than evidence.
        self._pi_stats(ctx)

    def _pi_stats(self, ctx):
        started = ctx.get("t0") or 0
        elapsed = max(0, int(time.time() * 1000) - int(started)) if started else 0
        ctx["ms"] = elapsed
        self.bus.publish({"type": "session.stats", "properties": {
            "sessionID": ctx["sid"], "tokens": int(ctx.get("_pi_tokens") or 0),
            "ms": elapsed, "cost": ctx.get("_pi_cost") or 0}})

    # ---- 扩展 UI / 权限回路 ----

    def _pi_provider_notice_accept(self, ctx, payload):
        """Fence provider observations by request and emission order.

        The extension writes these notifications from timer and stream callbacks.
        A delayed callback from an older request must not restore a wait after
        newer model/tool progress. Legacy unsequenced notices remain accepted
        only until this native process has demonstrated the fenced protocol.
        """
        epoch = payload.get("request_epoch")
        request_seq = payload.get("request_seq")
        notice_seq = payload.get("notice_seq")
        sequenced = epoch is not None or request_seq is not None or notice_seq is not None
        fence = ctx.get("_provider_wait_fence")
        proc = ctx.get("proc")
        process_key = (ctx.get("sid"), getattr(proc, "pid", None))
        process_fence = (self._pi_provider_fences.get(process_key)
                         if process_key[1] is not None else None)
        is_start = payload.get("kind") == "request_start"
        if not sequenced:
            return fence is None and process_fence is None and not is_start
        if (not isinstance(epoch, str) or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", epoch)
                or isinstance(request_seq, bool) or not isinstance(request_seq, int)
                or request_seq < 1 or isinstance(notice_seq, bool)
                or not isinstance(notice_seq, int) or notice_seq < 1):
            return False
        if is_start:
            if notice_seq != 1:
                return False
            if isinstance(process_fence, dict) and (process_fence.get("epoch") != epoch
                    or request_seq <= int(process_fence.get("request_seq") or 0)):
                return False
            if isinstance(fence, dict) and (fence.get("epoch") != epoch
                    or request_seq <= int(fence.get("request_seq") or 0)):
                return False
            if process_key[1] is not None:
                self._pi_provider_fences[process_key] = {
                    "epoch": epoch, "request_seq": request_seq}
        else:
            # Sequenced controls never establish their own request boundary.
            # The producer emits request_start first on the same ordered channel,
            # so a pre-start frame is necessarily delayed or cross-wired.
            if (not isinstance(fence, dict) or fence.get("epoch") != epoch
                    or request_seq != int(fence.get("request_seq") or 0)
                    or notice_seq <= int(fence.get("notice_seq") or 0)):
                return False
        ctx["_provider_wait_fence"] = {"epoch": epoch, "request_seq": request_seq,
                                       "notice_seq": notice_seq}
        return True

    def _pi_ui_request(self, ctx, obj):
        method = str(obj.get("method") or "")
        message = str(obj.get("message") or "")
        if method == "notify" and message.startswith(self.PROVIDER_TRACE_PREFIX):
            if len(message) > 8192:
                return
            try:
                payload = json.loads(message[len(self.PROVIDER_TRACE_PREFIX):])
            except (TypeError, ValueError):
                return
            if not isinstance(payload, dict) or payload.get("kind") not in ("request", "wire", "usage"):
                return
            request_seq = payload.get("request_seq")
            epoch = payload.get("request_epoch")
            if (isinstance(request_seq, bool) or not isinstance(request_seq, int)
                    or not 1 <= request_seq <= 100000
                    or not isinstance(epoch, str) or not re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", epoch)):
                return
            allowed = {
                "input", "cache_read", "total", "transport", "continuation_used",
                "fingerprint_epoch_fp", "request_epoch_fp",
                "session_fp", "cache_key_fp", "conversation_fp", "session_changed", "cache_key_changed",
                "conversation_changed", "change_reason", "system_segment_fp",
                "tools_segment_fp", "history_segment_fp", "reasoning_segment_fp",
                "tail_segment_fp", "identity_mode",
            }
            if set(payload) - allowed - {"kind", "request_epoch", "request_seq", "provider", "model"}:
                return
            safe = {}
            for key in allowed:
                value = payload.get(key)
                if key in ("input", "cache_read", "total"):
                    if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                            or not 0 <= value <= 10**9):
                        return
                elif key.endswith("_fp"):
                    if value is not None and (not isinstance(value, str)
                            or not re.fullmatch(r"[0-9a-f]{32}", value)):
                        return
                elif key in ("session_changed", "cache_key_changed", "conversation_changed",
                             "continuation_used"):
                    if value is not None and not isinstance(value, bool):
                        return
                elif key == "transport":
                    if value is not None and value not in ("unknown", "http_sse", "websocket", "app_server"):
                        return
                elif key == "change_reason":
                    if value is not None and value not in (
                            "unknown", "new_conversation", "new_provider_request", "fork",
                            "model_route_change", "resident_recreate", "retry_same_payload"):
                        return
                elif key == "identity_mode":
                    if value is not None and value not in ("legacy", "native"):
                        return
                if value is not None:
                    safe[key] = value
            if payload["kind"] == "usage" and any(
                    key not in safe for key in ("input", "cache_read", "total")):
                return
            traces = ctx.setdefault("_provider_traces", {})
            trace_key = (epoch, request_seq)
            proc = ctx.get("proc")
            process_key = (ctx.get("sid"), getattr(proc, "pid", None))
            process_fence = self._pi_provider_trace_fences.get(process_key)
            if payload["kind"] == "request":
                seen_epochs = self._pi_provider_trace_epochs.setdefault(process_key, set())
                if not process_fence and request_seq != 1:
                    return
                if process_fence:
                    if process_fence[0] == epoch:
                        if request_seq <= process_fence[1]:
                            return
                    elif request_seq != 1 or epoch in seen_epochs:
                        return
                if len(seen_epochs) >= 64 and epoch not in seen_epochs:
                    return
                self._pi_provider_trace_fences[process_key] = trace_key
                seen_epochs.add(epoch)
                traces[trace_key] = safe
                ctx["_provider_trace_current"] = trace_key
            elif payload["kind"] == "usage":
                request = traces.pop(trace_key, None)
                if not isinstance(request, dict):
                    return
                if ctx.get("_provider_trace_current") == trace_key:
                    ctx.pop("_provider_trace_current", None)
                request.update({key:safe[key] for key in ("input", "cache_read", "total")
                                if key in safe})
                request.update(seq=int(ctx.get("seq") or 0), request_seq=request_seq)
                model = str(ctx.get("actual_model") or ctx.get("_usage_model") or "").strip()
                provider = str(ctx.get("_notice_provider") or ctx.get("_usage_provider") or "").strip()
                if model:
                    request["model"] = model
                if provider:
                    request["provider"] = provider
                if isinstance(ctx.get("_usage_resident_reused"), bool):
                    request["resident_reused"] = ctx["_usage_resident_reused"]
                try:
                    performance = ctx.get("_performance_telemetry") or self.performance
                    for source, target in (("_usage_binding_value", "binding_fp"),
                                           ("_usage_system_value", "system_fp")):
                        value = ctx.get(source)
                        if value is not None and value != "":
                            request[target] = performance.fingerprint(target, value)
                    performance.append("pi_usage", ctx["sid"], request)
                except (OSError, ValueError):
                    pass
                return
            else:
                if trace_key not in traces:
                    return
                previous = ctx.get("_provider_identity_last") or {}
                same_epoch = (previous.get("fingerprint_epoch_fp")
                              == safe.get("fingerprint_epoch_fp"))
                for field, changed in (("session_fp", "session_changed"),
                                       ("conversation_fp", "conversation_changed")):
                    if field in safe:
                        safe[changed] = bool(same_epoch and previous.get(field)
                                             and previous[field] != safe[field])
                ctx["_provider_identity_last"] = {
                    key: safe[key] for key in ("fingerprint_epoch_fp", "session_fp",
                                               "conversation_fp") if key in safe}
                traces[trace_key].update(safe)
            while len(traces) > 8:
                traces.pop(next(iter(traces)), None)
            return
        if method == "notify":
            level = obj.get('notifyType') or obj.get('level') or 'info'
            if level == 'error' and message and ctx.get("_pi_compacting"):
                # 摘要请求的协议诊断由紧随其后的 compaction_end 收口。这里仅暂存
                # 已脱敏的公开错误，不污染普通模型请求使用的 notification_error。
                ctx["_pi_compaction_error"] = message[:300]
                self._ctx_env_publish(ctx, {
                    'state': 'warning', 'stage': 'compaction',
                    'label': '上下文压缩异常', 'details': [message[:300]]})
                return
            if level == 'error' and message:
                # notify 是同一模型请求的提前诊断，不应绕开统一失败/恢复状态机。普通瞬时
                # 错误会在后续成功时撤掉；没有原生 code 的通知只归为 engine/UNKNOWN。
                ctx['_pi_notification_error'] = message[:300]
                self._pi_failed(ctx, message)
            elif message and level != 'info':
                self._ctx_env_publish(ctx, {
                    'state': 'warning', 'stage': 'engine', 'scope': 'turn', 'label': message[:300]})
            elif message:
                # Informational extension notifications do not describe a
                # failed engine. Keep metadata for diagnosis without replacing
                # an independent visible problem or persisting message text.
                self._trace(ctx['sid'], {'ev':'pi.extension.notify', 'level':'info'})
            return
        if method not in ("confirm", "select", "input", "editor"):
            return
        request_id = str(obj.get("id") or "")
        if not request_id:
            return
        title = str(obj.get("title") or "")
        payload = None
        if method == "confirm" and title.startswith(self.GATE_PREFIX):
            try:
                payload = json.loads(str(obj.get("message") or "{}"))
            except ValueError:
                payload = {}
            tool = str(payload.get("tool") or title[len(self.GATE_PREFIX):] or "tool")
            props = {"id": request_id, "sessionID": ctx["sid"], "tool": tool,
                     "input": payload.get("input") or {},
                     "reason": f"是否允许 Pi 执行 {tool}？"}
            gate = True
        else:
            question = title or str(obj.get("message") or "Pi 需要你的输入")
            options = obj.get("options") or []
            if method == "confirm":
                options = ["确认", "取消"]
            props = {"id": request_id, "sessionID": ctx["sid"], "tool": "AskUserQuestion",
                     "input": {"questions": [{"question": question, "header": question[:20],
                                                "options": [{"label": str(x), "description": ""}
                                                            for x in options],
                                                "multiSelect": False}]},
                     "reason": str(obj.get("message") or question)}
            gate = False
        with self._plock:
            self._approvals[request_id] = (ctx.get("proc"), method, copy.deepcopy(obj), gate,
                                           ctx["sid"], copy.deepcopy(props), ctx)
        if ctx.get("aborted"):
            self.respond_permission(request_id, False, resolution="cancelled")
            return
        self.bus.publish({"type": "permission.asked", "properties": props})
        timeout = min(self.PERMISSION_TIMEOUT, max(1, int(obj.get("timeout") or 300000) // 1000))
        timer = threading.Timer(max(1, timeout - 1),
                                lambda: self.respond_permission(request_id, False, resolution="timeout"))
        timer.daemon = True
        timer.start()

    @staticmethod
    def _pi_answer(answers, response=""):
        if response:
            return response
        if not isinstance(answers, dict) or not answers:
            return ""
        value = next(iter(answers.values()))
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return str(value or "")

    def _pending_ask(self, sid):
        with self._plock:
            return [copy.deepcopy(value[5]) for value in self._approvals.values()
                    if value[4] == sid]

    def _pi_queue(self, ctx, payload):
        from .lifecycle import _current
        if not _current(self, ctx):
            return
        phase = str((payload or {}).get("phase") or "")
        if phase not in ("queue_begin", "request_wait_in_queue", "queue_end", "request_dequeue"):
            return
        waiting = phase in ("queue_begin", "request_wait_in_queue")
        position = (payload or {}).get("position")
        state = {"phase": phase}
        if isinstance(position, int) and position >= 0:
            state["position"] = position
        if waiting:
            ctx["_provider_queue"] = state
        else:
            ctx.pop("_provider_queue", None)
        self._checkpoint_live(ctx, force=True)
        self.bus.publish({"type": "session.queue", "properties": {
            "sessionID": ctx["sid"], "waiting": waiting,
            "generation": int(ctx.get("seq") or 0), **state}})

    def _finish_turn(self, sid, ctx, abnormal):
        # 正常 RPC 由 agent_settled 收口；若进程提前退出，外层 turn 收尾就是最后的
        # 权威边界，必须在通用持久化前提交仍未恢复的 CONTEXT 和可见 OpenAI 思考。
        self._pi_flush_openai_thinking(ctx)
        self._pi_finalize_context_failure(ctx)
        if ctx.get("_provider_queue"):
            self._pi_queue(ctx, {"phase": "queue_end"})
        ctx.pop("_provider_wait", None)
        with self._plock:
            pending = [key for key, value in self._approvals.items() if value[4] == sid]
        for request_id in pending:
            self.respond_permission(request_id, False, resolution="cancelled")
        return super()._finish_turn(sid, ctx, abnormal)

    def respond_permission(self, request_id, allow, answers=None, response="", *, resolution="",
                           asks_provider=None):
        with self._plock:
            pending = self._approvals.pop(str(request_id or ""), None)
        if not pending:
            return False
        proc, method, original, gate, sid, props, ctx = pending
        def resolved(reason):
            provider = asks_provider or getattr(self, '_asks_provider', None)
            try:
                asks = provider(sid) if provider else self._pending_ask(sid)
            except Exception:
                try: asks = self._pending_ask(sid)
                except Exception: asks = []
            try:
                self.bus.publish({"type": "permission.resolved", "properties": {
                    "sessionID": sid, "id": request_id, "resolution": reason,
                    "asks": asks}})
            except Exception:
                pass
        if proc is None or proc.poll() is not None or not proc.stdin:
            resolved("connection_error")
            return False
        if not allow:
            frame = {"type": "extension_ui_response", "id": request_id, "cancelled": True}
        elif method == "confirm":
            answer = self._pi_answer(answers, response)
            confirmed = bool(allow) if gate or not answer else answer not in ("取消", "No", "no", "false")
            frame = {"type": "extension_ui_response", "id": request_id, "confirmed": confirmed}
        else:
            value = self._pi_answer(answers, response)
            frame = ({"type": "extension_ui_response", "id": request_id, "value": value}
                     if value else
                     {"type": "extension_ui_response", "id": request_id, "cancelled": True})
        try:
            with self._pi_write_lock:
                proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            reason = resolution or ("approved" if allow else "denied")
            if gate:
                # RPC 回执只含 confirmed/cancelled；Desk 保留自己已知的结构化审批终态，
                # 供随后 assistant abort 精确归因。普通 AskUserQuestion 不冒充工具审批。
                ctx["_pi_permission_resolution"] = reason
            resolved(reason)
            return True
        except Exception:
            resolved("connection_error")
            return False

    # ---- 会话记录投影 ----

    @staticmethod
    def _pi_path(esid):
        return pi_session_path(esid)

    def search_source_path(self, sid, record):
        esid = str((record or {}).get("engine_sid") or "")
        path = self._pi_path(esid) if esid else ""
        return path if path and os.path.isfile(path) else super().search_source_path(sid, record)

    def search_documents(self, sid, record, path):
        if path and path != self._msg_path(sid):
            try:
                from session_search import contract_documents
                return contract_documents(self._pi_group(path, sid) or [])
            except Exception:
                pass
        return super().search_documents(sid, record, path)

    @staticmethod
    def _active_rows(rows):
        entries = [row for row in rows if isinstance(row, dict) and row.get("type") != "session"]
        if not entries or not any("parentId" in row for row in entries):
            return entries
        by_id = {str(row.get("id")): row for row in entries if row.get("id")}
        leaf = next((row for row in reversed(entries) if row.get("id")), None)
        chain, seen = [], set()
        while leaf and str(leaf.get("id")) not in seen:
            eid = str(leaf.get("id"))
            seen.add(eid)
            chain.append(leaf)
            parent = leaf.get("parentId")
            leaf = by_id.get(str(parent)) if parent else None
        chain.reverse()
        return chain

    def _pi_parts(self, content, mid, sid, results=None, seq=0, commentary=False,
                  provider=""):
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        parts, results = [], (results or {})
        for index, block in enumerate(content or []):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            pid = f"{mid}_p{seq}_{index}"
            base = {"id": pid, "messageID": mid, "sessionID": sid}
            if kind == "text" and str(block.get("text") or "").strip():
                parts.append(dict(base, type="reasoning" if commentary else "text", text=str(block["text"])))
            elif kind == "thinking" and str(block.get("thinking") or "").strip():
                thinking = str(block["thinking"])
                if not is_openai_summary_titles_only(provider, thinking):
                    parts.append(dict(base, type="reasoning", text=thinking))
            elif kind == "image" and block.get("data"):
                media_type = block.get("mimeType") or block.get("media_type") or "image/png"
                url = self.asset_save(sid, block.get("data"), media_type)
                if url:
                    parts.append(dict(base, type="image", media_type=media_type, url=url))
            elif kind == "toolCall":
                tool = block.get("name") or "tool"
                state = {"status": "done",
                         "input": self._pi_tool_input(tool, block.get("arguments"))}
                result = results.get(str(block.get("id") or ""))
                if result is not None:
                    state["status"] = "error" if result.get("isError") else "done"
                    output = self._pi_text(result.get("content") or [])
                    if output:
                        state["output"] = output
                    details = result.get("details") or {}
                    first_line = details.get("firstChangedLine") if isinstance(details, dict) else None
                    if not result.get("isError") and str(tool).lower() == "edit" \
                            and isinstance(first_line, int) and not isinstance(first_line, bool) \
                            and first_line > 0:
                        state["line_start"] = first_line
                part = dict(base, type="tool", tool=tool, state=state, call_id=block.get("id"))
                parts.append(part)
        return parts

    def _pi_group(self, path, sid, sink=None):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                rows = []
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return None
        active = self._active_rows(rows)
        results = {}
        for row in active:
            message = row.get("message") or {}
            if row.get("type") == "message" and message.get("role") == "toolResult":
                results[str(message.get("toolCallId") or "")] = message
        out, current = [], None
        retry_candidate = None

        def flush_marker(row, trigger):
            nonlocal current, retry_candidate
            # Pi 只有成功写入摘要后才追加 compaction 行。若它紧跟一个空的失败 assistant，
            # 下一条无 user 间隔的 assistant 就是 overflow retry；届时用新结果取代旧暂态。
            info = (current or {}).get("info") or {}
            retry_candidate = (current if trigger == "auto" and info.get("failed")
                               and not (current or {}).get("parts") else None)
            current = None
            at = self._iso_ms(row.get("timestamp"))
            mid = "msg_c_" + str(row.get("id") or uuid.uuid4().hex)
            summary = str(row.get("summary") or "").strip()
            parts = ([{"id": mid + "_p0", "messageID": mid, "sessionID": sid,
                       "type": "text", "text": summary}] if summary else [])
            out.append({"info": {"id": mid, "role": "user", "sessionID": sid, "at": at,
                                  "compact": {"trigger": trigger,
                                              "pre": int(row.get("tokensBefore") or 0), "post": 0}},
                        "parts": parts})

        for seq, row in enumerate(active):
            row_type = row.get("type")
            if row_type == "compaction":
                flush_marker(row, "auto")
                continue
            if row_type == "branch_summary":
                flush_marker(row, "branch")
                continue
            if row_type == "custom_message" and row.get("display"):
                message = {"role": "user", "content": row.get("content") or "",
                           "timestamp": self._iso_ms(row.get("timestamp"))}
            elif row_type == "message":
                message = row.get("message") or {}
            else:
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            at = int(message.get("timestamp") or self._iso_ms(row.get("timestamp")) or 0)
            if role == "user":
                current = None
                retry_candidate = None
                turn_start = at  # 回合起点:用它算 assistant 的耗时
                mid = "msg_u_" + str(row.get("id") or seq)
                content = self._pi_user_content(message.get("content") or [])
                parts = self._pi_parts(content, mid, sid, results, seq)
                if parts:
                    out.append({"info": {"id": mid, "role": "user", "sessionID": sid, "at": at,
                                          "engine_entry_id": row.get("id")}, "parts": parts})
                continue
            if current is None:
                if retry_candidate is not None:
                    # 压缩后的继续调用已经到达；无论它成功或失败，压缩前那条空错误都只是
                    # 同一恢复序列的第一次尝试，不应在历史里再留一个失败回合。
                    retry_candidate["info"].pop("failed", None)
                    retry_candidate["info"].pop("error", None)
                    retry_candidate["info"].pop("interrupted", None)
                    retry_candidate = None
                mid = "msg_a_" + str(row.get("id") or seq)
                current = {"info": {"id": mid, "role": "assistant", "sessionID": sid, "at": at,
                                    "engine_entry_id": row.get("id"), "ms": 0}, "parts": []}
                out.append(current)
            current["info"]["engine_entry_id"] = row.get("id") or current["info"].get("engine_entry_id")
            # 成文时刻跟着最后一条走:at 取的是首条 assistant 的时间,离提问只差几秒。
            # 另落 done_at 给界面显示,at 仍是按时刻切的判据,不改它的语义。
            if at > int(current["info"].get("done_at") or current["info"].get("at") or 0):
                current["info"]["done_at"] = at
            # 耗时 = 当前 assistant 消息时间 - 回合起点(用户消息时间);多轮工具调用取最后一条
            if turn_start and at > turn_start:
                current["info"]["ms"] = at - turn_start
            parts = self._pi_parts(message.get("content") or [], current["info"]["id"],
                                   sid, results, seq,
                                   commentary=not self._pi_is_final_answer(message),
                                   provider=str(message.get("provider") or ""))
            current["parts"] += parts
            stop_reason = str(message.get("stopReason") or "")
            error_message = str(message.get("errorMessage") or "").strip()
            # Pi journal has no Desk interruption intent. Native aborted is therefore restored as
            # “interrupted”, never “failed”.
            native_failed = stop_reason == "error" or (bool(error_message) and stop_reason != "aborted")
            if stop_reason == "aborted":
                current["info"].pop("failed", None)
                current["info"].pop("error", None)
                current["info"]["interrupted"] = True
                if sink is not None:
                    sink["notice"] = ""
            elif native_failed:
                current["info"].pop("interrupted", None)
                current["info"]["failed"] = True
                detail = error_message[:300] or "Pi 模型返回错误"
                current["info"]["error"] = {"source": "engine", "code": "UNKNOWN", "message": detail}
                if sink is not None:
                    sink["notice"] = detail
            else:
                current["info"].pop("failed", None)
                current["info"].pop("error", None)
                current["info"].pop("interrupted", None)
                if sink is not None and (parts or stop_reason == "stop"):
                    sink["notice"] = ""
        projected = [message for message in out if message.get("parts")
                     or (message.get("info") or {}).get("compact")
                     or (message.get("info") or {}).get("failed")
                     or (message.get("info") or {}).get("interrupted")]
        return self._restore_peer_heads(sid, projected)

    def _pi_preserve_structured_failures(self, sid, messages):
        """刷新时用 Desk 已落盘的原生 code 覆盖 Pi 日志的保守 PROTOCOL。

        Pi 的 AssistantMessage 只持久化 errorMessage，结构化 Provider code 走 RPC 控制帧。
        仅当新投影中的同一时间窗仍是 failed 时才覆盖；压缩重试已经成功的回合不会复活。
        """
        try:
            old = self.messages(sid)
        except Exception:
            return messages
        saved = []
        for message in old:
            info = message.get("info") or {}
            failure = info.get("error")
            if (info.get("failed") and isinstance(failure, dict)
                    and failure.get("source") in ("provider", "permission")):
                saved.append((int(info.get("turn_t0") or 0),
                              int(info.get("at") or info.get("done_at") or 0),
                              copy.deepcopy(failure)))
        if not saved:
            return messages
        for message in messages:
            info = message.get("info") or {}
            if not info.get("failed"):
                continue
            at = int(info.get("at") or 0)
            match = next((failure for start, end, failure in reversed(saved)
                          if start and end and start <= at <= end), None)
            if match is not None:
                info["error"] = match
        return messages

    def import_session(self, esid, sid=None, limit=SESSION_WINDOW):
        path = self._pi_path(esid)
        if not path:
            return None
        if not sid:
            with self._lock:
                sid = next((key for key, row in self.index.items()
                            if row.get("engine_sid") == esid), None)
            if not sid:
                # 认领外部会话时不预造另一枚 UUID。
                sid = super().create_session(domain=self.adopt_domain(esid))["id"]
        sink = {}
        messages = self._pi_group(path, sid, sink=sink)
        if messages is None or self._import_poorer(sid, messages):
            return None
        if limit:
            starts = [index for index, message in enumerate(messages) if self._turn_start(message)]
            if len(starts) > limit:
                messages = messages[starts[-limit]:]
        live = self._live.get(sid)
        cut = (live or {}).get("t0") or 0
        if cut:
            messages = [message for message in messages
                        if not ((message.get("info") or {}).get("role") == "assistant"
                                and ((message.get("info") or {}).get("at") or 0) >= cut)]
        messages = self._pi_preserve_structured_failures(sid, messages)
        messages = self._reinstate_local_only(sid, messages)
        self._rewrite_msgs(sid, messages)
        title, title_user = "", False
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                rows = [json.loads(line) for line in f if '"session_info"' in line]
            for row in rows:
                if row.get("type") == "session_info":
                    title = str(row.get("name") or "").strip()
        except Exception:
            pass
        # session_info.name 是 pi 自己的命名通道(用户在 pi 里改名写这里)。分不清是用户写的
        # 还是 pi 自动写的时,一律当用户写的:自动命名宁可不显示,也不要盖掉一个真名字。
        title_user = bool(title)
        if not title:
            for message in messages:
                if not self._turn_start(message):
                    continue
                title = "".join(str(part.get("text") or "") for part in message.get("parts") or []
                                if part.get("type") == "text").strip().splitlines()[0]
                if title:
                    break
        with self._lock:
            row = self.index.get(sid)
            if row is not None:
                row["engine_sid"] = esid
                row["engine_started"] = True
                if title and not row.get("title"):
                    row["title"] = title[:60]
                if title_user:
                    row["title_user"] = True
                self._save_index()
        self._set_engine_sid(sid, esid)
        note = sink.get("notice")
        if note is not None:
            head = self._notice_head(note)
            if note:
                self._env_publish(sid, {"state": "error", "stage": "engine", "label": head,
                                        "details": [note] if note != head else []})
            else:
                cur = (self.index.get(sid) or {}).get("environment") or {}
                if cur.get("stage") == "engine":
                    self._env_publish(sid, {"state": "ready", "stage": "engine",
                                            "label": "引擎已恢复"})
        return {"id": sid, "count": len(messages)}

    def history(self, sid, have=0, limit=30):
        esid = self._esid_of(sid)
        path = self._pi_path(esid)
        if not path:
            return []
        return self._history_window(self._pi_group(path, sid), have, limit)

    # ---- 离线控制 RPC：压缩与当前分支克隆 ----

    def _control_env(self, sid):
        from pi_extensions import without_legacy_web_secrets
        env = without_legacy_web_secrets(secrets_env(os.environ))
        env.update(self.spawn_env(self.session_options(sid, {})))
        return env

    def _rpc_once(self, sid, frames, timeout=900):
        with self._lock:
            guard = self._turn_locks.setdefault(sid, threading.Lock())
        if not guard.acquire(blocking=False):
            return [], '当前回合仍在运行，不能同时执行 Pi 控制命令'
        try:
            if self._shutting_down:
                return [], 'Desk 正在退出，Pi 控制命令未发送'
            if sid in self._live:
                return [], '当前回合仍在运行，不能同时执行 Pi 控制命令'
            _persist_drop(sid)
            return self._rpc_once_locked(sid, frames, timeout)
        finally:
            guard.release()

    def _rpc_once_locked(self, sid, frames, timeout):
        esid = self._esid_of(sid)
        path = self._pi_path(esid)
        if not path:
            return [], "找不到 Pi 会话记录"
        cmd = [self.binary, "--mode", "rpc", "--session", path, "--offline",
               "--no-extensions", "--no-approve"]
        cmd += prompt_isolation_args()
        cmd += provider_extension_args()
        opts = self.session_options(sid, {})
        grounding = self.grounding(sid, first_turn=False)["constant"]
        cmd = append_system_prompt_if_missing(cmd, self._turn_system_prompt(opts, grounding))
        objects = []
        errors = []
        output = queue.Queue()
        proc = None
        try:
            proc = self._spawn_process(sid, cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1,
                                    cwd=self.session_cwd(sid), env=self._control_env(sid))

            def read_stdout():
                for line in proc.stdout:
                    output.put(line)
                output.put(None)

            def read_stderr():
                errors.extend(proc.stderr.readlines())

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            deadline = time.monotonic() + timeout
            for raw_frame in frames:
                frame = dict(raw_frame)
                request_id = str(frame.setdefault("id", f"desk-control-{uuid.uuid4().hex}"))
                proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    try:
                        line = output.get(timeout=remaining)
                    except queue.Empty as exc:
                        raise subprocess.TimeoutExpired(cmd, timeout) from exc
                    if line is None:
                        raise RuntimeError("Pi RPC 在返回控制命令结果前退出")
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    objects.append(obj)
                    if (obj.get("type") == "response"
                            and (str(obj.get("id") or "") == request_id
                                 or (not obj.get("id") and obj.get("command") == frame.get("type")))):
                        break
            proc.stdin.close()
            proc.wait(timeout=max(0.1, min(5, deadline - time.monotonic())))
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            return objects, str(exc)[:300]
        if proc.returncode not in (0, None):
            return objects, ("".join(errors) or f"Pi 退出 {proc.returncode}")[-300:]
        return objects, ""

    def compact_session(self, sid):
        if sid in self._live:
            return {"ok": False, "error": "当前回合仍在运行，不能同时压缩"}
        if not self._pi_path(self._esid_of(sid)):
            return {"ok": False, "error": "这段 Pi 会话还没有可压缩的历史"}
        started = int(time.time() * 1000)
        self._compact_state[sid] = {"running": True, "trigger": "manual", "started": started}
        self.bus.publish({"type": "session.compact", "properties": {
            "sessionID": sid, **self._compact_state[sid]}})
        try:
            objects, error = self._rpc_once(sid, [{"type": "compact"}])
        except Exception as exc:
            objects, error = [], str(exc)[:300]
        response = next((obj for obj in objects if obj.get("type") == "response"
                         and obj.get("command") == "compact"), {})
        result = response.get("data") or {}
        ok = bool(response.get("success")) and not error
        error = "" if ok else error or str(response.get("error") or "Pi 压缩失败")[:300]
        self._compact_state[sid] = {"running": False, "trigger": "manual", "started": started,
                                   "result": result if ok else "failed", "error": error}
        self.bus.publish({"type": "session.compact", "properties": {
            "sessionID": sid, **self._compact_state[sid]}})
        if ok:
            from .session_naming import invalidate
            invalidate(self, sid)
            self.import_session(self._esid_of(sid), sid=sid, limit=50)
        return {"ok": ok, "result": result,
                **({"error": error or str(response.get("error") or "Pi 压缩失败")[:300]} if not ok else {})}

    def fork_session(self, sid, msg_id=""):
        if sid in self._live or not self._pi_path(self._esid_of(sid)):
            return None
        _persist_drop(sid)
        objects, error = self._rpc_once(sid, [{"type": "clone"}, {"type": "get_state"}])
        if error:
            return None
        cloned = next((obj for obj in objects if obj.get("type") == "response"
                       and obj.get("command") == "clone"), {})
        state = next((obj for obj in reversed(objects) if obj.get("type") == "response"
                      and obj.get("command") == "get_state"), {})
        new_esid = str((state.get("data") or {}).get("sessionId") or "")
        if not cloned.get("success") or (cloned.get("data") or {}).get("cancelled") or not new_esid:
            return None
        source = self.index.get(sid) or {}
        new = super().create_session(domain=source.get("domain") or "",
                                     task=bool(source.get("task")), opts=source.get("opts"))
        new_sid = new["id"]
        with self._lock:
            row = self.index.get(new_sid)
            if row is not None:
                row["engine_sid"] = new_esid
                row["engine_started"] = True
                row["forked_from"] = sid
                if source.get("title"):
                    row["title"] = ("分支 · " + source["title"])[:60]
                self._save_index()
        self._set_engine_sid(new_sid, new_esid)
        self.import_session(new_esid, sid=new_sid, limit=SESSION_WINDOW)
        self.bus.publish({"type": "session.updated", "properties": {"sessionID": new_sid}})
        return dict(self.index.get(new_sid) or {}, id=new_sid)

    def slash_commands(self):
        # Desk 会话关闭 Pi 的技能自动加载；技能改为按需查找并直接读取 SKILL.md。
        rows = []
        prompts = os.path.expanduser("~/.pi/agent/prompts")
        try:
            for filename in sorted(os.listdir(prompts)):
                if filename.endswith(".md"):
                    name = filename[:-3]
                    if not any(row["n"] == name for row in rows):
                        rows.append({"n": name, "d": "Pi prompt template"})
        except OSError:
            pass
        return rows
