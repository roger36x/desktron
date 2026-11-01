"""Installed-engine admission. Release labels are diagnostic, never capabilities.

App-server engines export a schema; SDK engines exercise a bounded local contract.
Probes use disposable state and no user credentials. A probe is not a replacement
for runtime source, permission or terminal validation. No version-keyed cache.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from owned_process import command, protect

LABELS = {'claude': 'Claude', 'pi': 'Pi', 'codex': 'Codex'}
# Pi's cold module graph can exceed 15s on Windows even when its complete
# contract passes. This is a local admission budget, not a model/API timeout.
SDK_PROBE_TIMEOUTS = {'pi': 45, 'claude': 15}
PROBE_PHASES = {
    'package': '读取 SDK 清单', 'store_import': '加载会话存储', 'store': '验证会话存储',
    'imports': '加载 SDK 模块', 'exports': '验证 SDK 接口', 'settings': '验证设置接口',
    'http': '初始化 HTTP', 'services': '初始化 SDK 服务', 'session': '初始化会话',
    'dispose': '释放探测会话', 'claude_init': '初始化 CLI', 'done': '完成',
}


def probe_progress(home):
    """Only fixed phase names and bounded integer timings may reach diagnostics."""
    try:
        path = Path(home) / 'engine-probe-progress.json'
        if path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        phase, elapsed = value.get('phase'), value.get('elapsed_ms')
        if not isinstance(phase, str) or phase not in PROBE_PHASES or type(elapsed) is not int or not 0 <= elapsed <= 300000:
            return {}
        return {'probe_phase': phase, 'probe_phase_started_ms': elapsed}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def discard_probe_home(home, attempts=20, delay=0.1):
    """Remove the isolated probe HOME without letting cleanup rewrite the probe result.

    On Windows a just-killed child or an on-access scanner can hold a file briefly
    (WinError 32). That must not turn a timeout into "probe could not start", so
    retry for a bounded time, then log and leave the private temp directory.
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(home)
            return True
        except FileNotFoundError:
            return True
        except OSError as error:
            if attempt + 1 == attempts:
                try:
                    import deskcfg
                    deskcfg.log('engine-protocol', f'探测临时目录未能删除：{type(error).__name__}')
                except Exception:
                    pass
                return False
            time.sleep(delay)
    return False


def failure(engine, reason, detail, **extra):
    return dict(available=False, reason=engine + '_' + reason,
                message=f'{LABELS[engine]} {detail}；消息未派发。', **extra)


def isolated_env(home):
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'SYSTEMROOT') if key in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), XDG_CACHE_HOME=str(home),
               TMPDIR=str(home), CODEX_HOME=str(home),
               PI_CODING_AGENT_DIR=str(home), PI_OFFLINE='1',
               CLAUDE_CONFIG_DIR=str(home), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               DESKTRON_PROTOCOL_PROGRESS=str(Path(home) / 'engine-probe-progress.json'))
    if os.name == 'nt':
        # Node/Python use USERPROFILE and TEMP on Windows, not only HOME/TMPDIR.
        # Do not let an offline probe fall back to the real profile or caches.
        env.update(USERPROFILE=str(home), APPDATA=str(Path(home) / 'appdata'),
                   LOCALAPPDATA=str(Path(home) / 'localappdata'), TEMP=str(home), TMP=str(home))
    return env


def run_probe(argv, home, *, timeout=15):
    """Bound process lifetime and output; never copy native stderr into a UI error."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(command(argv), cwd=home, env=isolated_env(home),
                                stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            protect(proc)
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            error.desk_progress = probe_progress(home)
            error.desk_elapsed_ms = round((time.monotonic() - started) * 1000)
            raise
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        output.seek(0)
        raw = output.read(65537)
        if code or len(raw) > 65536:
            raise ValueError('probe failed or output exceeds limit')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('invalid probe report')
        return {**result, **probe_progress(home),
                'probe_elapsed_ms': round((time.monotonic() - started) * 1000)}


def sdk_availability(engine, sdk_root, node_binary=None, binary=None, *, storage_only=False):
    root = Path(sdk_root or '')
    node = node_binary or shutil.which('node')
    if not node or not os.path.isfile(node):
        return failure(engine, 'node_missing', '未找到 Node.js，请检查安装')
    if engine == 'claude' and not storage_only and (not binary or not os.path.isfile(binary)):
        return failure(engine, 'missing', '未找到 CLI，请检查安装')
    if not (root / 'package.json').is_file():
        hint = '请在终端运行 shepherd prepare-claude' if engine == 'claude' else '请检查安装'
        return failure(engine, 'sdk_missing', '未找到 SDK，' + hint)
    contract = f'desk-{engine}-sdk-v1'
    timeout = SDK_PROBE_TIMEOUTS[engine]
    try:
        home = tempfile.mkdtemp(prefix=f'desk-{engine}-protocol-')
        try:
            report = run_probe([node, str(Path(__file__).with_name('engine_sdk_probe.mjs')),
                                engine, str(root.resolve()), binary or '',
                                'storage' if storage_only else 'execute'], home, timeout=timeout)
        finally:
            discard_probe_home(home)
        diagnostics = {'probe_timeout_s': timeout}
        phase = report.get('probe_phase')
        if isinstance(phase, str) and phase in PROBE_PHASES:
            diagnostics['probe_phase'] = phase
            elapsed = report.get('probe_phase_started_ms')
            if type(elapsed) is int and 0 <= elapsed <= 300000:
                diagnostics['probe_phase_started_ms'] = elapsed
        elapsed = report.get('probe_elapsed_ms')
        if type(elapsed) is int and 0 <= elapsed <= 300000:
            diagnostics['probe_elapsed_ms'] = elapsed
        if report.get('contract') != contract or report.get('available') is not True:
            missing = report.get('missing_capabilities')
            if not isinstance(missing, list) or not missing or any(
                    not isinstance(x, str) or len(x) > 120 for x in missing):
                raise ValueError('invalid capability report')
            return failure(engine, 'protocol_incompatible', '缺少或改变了必需能力：' + ', '.join(missing),
                           contract=contract, missing_capabilities=missing,
                           sdk_version=report.get('sdk_version', ''), **diagnostics)
        return dict(available=True, contract=contract, admission='protocol',
                    assurance='local-contract', sdk_version=report.get('sdk_version', ''), **diagnostics)
    except subprocess.TimeoutExpired as error:
        progress = getattr(error, 'desk_progress', {})
        phase = progress.get('probe_phase') if isinstance(progress, dict) else None
        detail = f'协议能力探测超过 {timeout} 秒'
        diagnostics = {'probe_timeout_s': timeout}
        if isinstance(phase, str) and phase in PROBE_PHASES:
            diagnostics['probe_phase'] = phase
            detail += f'（阶段：{PROBE_PHASES[phase]}）'
        elapsed = getattr(error, 'desk_elapsed_ms', None)
        if type(elapsed) is int and 0 <= elapsed <= 300000:
            diagnostics['probe_elapsed_ms'] = elapsed
        return failure(engine, 'probe_timeout', detail + '，请检查本机环境', **diagnostics)
    except OSError:
        return failure(engine, 'probe_failed', '协议能力探测无法启动，请检查安装和执行权限')
    except (ValueError, TypeError):
        return failure(engine, 'protocol_unverified', '无法确认协议能力（并非版本号不受支持），请检查安装或更新 Desk')


def availability(engine):
    """Same result for the dependency doctor and the execution connectors."""
    if engine == 'codex':
        from codex_protocol import availability as app_server
        return app_server(engine=engine)
    if engine == 'claude':
        from shepherd.claude_connector import ClaudeConnector
        return ClaudeConnector.availability()
    if engine == 'pi':
        from shepherd.pi_connector import PiConnector
        return PiConnector.availability()
    raise ValueError('unknown engine')
