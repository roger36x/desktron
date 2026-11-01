"""Version-independent admission for Codex-family app-server protocols.

Exports local schemas in disposable state, without a model request or user HOME.
Runtime terminal/source/permission validation remains authoritative.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from codex_protocol_contract import BUNDLES, Unproved, assess
from engine_protocol import isolated_env, failure
from owned_process import command, protect


def export_schema(binary, destination, home, timeout=15):
    proc = subprocess.Popen(command([binary, 'app-server', 'generate-json-schema',
                                    '--experimental', '--out', str(destination)]),
                            cwd=home, env=isolated_env(home), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    protect(proc)
    try:
        return proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()  # Exact supervisor and its children, not process scanning.
            proc.wait(timeout=5)


def read_documents(directory):
    documents = {}
    for name in BUNDLES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise Unproved('missing or oversized schema')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise Unproved('schema document is not an object')
        documents[name] = value
    return documents


def availability(binary=None, *, engine='codex'):
    if engine != 'codex':
        raise ValueError('unknown app-server engine')
    contract = f'desk-{engine}-app-server-v1'
    def fail(reason, message, **extra):
        return failure(engine, reason, message, contract=contract, **extra)
    binary = binary or shutil.which(engine)
    if not binary or not os.path.isfile(binary):
        return fail('missing', '未找到 CLI，请检查安装')
    try:
        with tempfile.TemporaryDirectory(prefix=f'desk-{engine}-protocol-') as folder:
            root = Path(folder); home = root / 'home'; home.mkdir(mode=0o700)
            destination = root / 'schema'
            if export_schema(os.path.abspath(binary), destination, home):
                return fail('protocol_unverified',
                    '无法取得协议能力声明（并非版本号不受支持），请检查 CLI 安装或更新 Desk')
            documents = read_documents(destination)
            failures = assess(documents, engine)
            schema_hash = hashlib.sha256(json.dumps(documents, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
    except subprocess.TimeoutExpired:
        return fail('probe_timeout', '协议能力探测超时，请检查本机环境')
    except OSError:
        return fail('probe_failed', '协议能力探测无法完成，请检查安装和执行权限')
    except (Unproved, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return fail('protocol_unverified',
            '协议声明无法按当前契约确认（未判定为版本不兼容），请更新 Desk')
    if failures:
        return fail('protocol_incompatible', '缺少或改变了必需协议能力：' + ', '.join(failures),
                    missing_capabilities=failures, schema_hash=schema_hash)
    return dict(available=True, contract=contract, admission='protocol',
                schema_hash=schema_hash, assurance='static-contract')
