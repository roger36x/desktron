"""首次安装所需的确定性运行时准备。

只安装 Desk 自身启动所需的 Node；不安装或启动任何 AI 设置助手。下载失败时用公司代理
重试，产物落在 Desk 自己的数据目录，不修改系统 Node。
"""
import os
import hashlib
import platform
import shutil
import tarfile
import tempfile
import urllib.request


NODE_VERSION = "22.23.2"


def _data_root():
    # 安装器没有 ui_server argv；不要为了取路径导入有 argv 契约的 deskcfg。
    return os.path.dirname(os.environ.get("DESKTRON_DATA_HOME",
                           os.path.expanduser("~/.local/share/desktron/data")))


def _node_platform():
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "win"}.get(system, "")
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, "")
    if not os_name or not arch:
        raise RuntimeError("这台设备暂不支持自动安装 Node")
    return os_name, arch


def _managed_node_home():
    os_name, arch = _node_platform()
    return os.path.join(_data_root(), "runtime", "node-v%s-%s-%s" % (NODE_VERSION, os_name, arch))


def _node_exe(home):
    """发行包内 node 可执行文件位置：Windows zip 放在根目录，其他平台在 bin/ 下。"""
    if _node_platform()[0] == "win":
        return os.path.join(home, "node.exe")
    return os.path.join(home, "bin", "node")


def _version_ok(command, minimum=(22, 19)):
    try:
        import subprocess
        proc = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return False
        raw = (proc.stdout or proc.stderr or "").strip().lstrip("v").split(".")
        return tuple(int(x) for x in raw[:2]) >= minimum
    except Exception:
        return False


def _architecture_ok(command):
    try:
        import subprocess
        proc = subprocess.run([command, "--print", "process.arch"],
                              capture_output=True, text=True, timeout=10)
        return proc.returncode == 0 and proc.stdout.strip() == _node_platform()[1]
    except Exception:
        return False


def _node_ok(command):
    return _version_ok(command) and _architecture_ok(command)


def node_command():
    managed = _node_exe(_managed_node_home())
    if _node_ok(managed):
        return managed
    current = shutil.which("node")
    return current if current and _node_ok(current) else ""


def _safe_extract(archive, target):
    root = os.path.realpath(target) + os.sep
    if archive.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                dest = os.path.realpath(os.path.join(target, member.filename))
                if not (dest + os.sep).startswith(root) and dest + os.sep != root:
                    raise RuntimeError("Node 安装包包含不安全路径")
            zf.extractall(target)
        return
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            dest = os.path.realpath(os.path.join(target, member.name))
            if not (dest + os.sep).startswith(root):
                raise RuntimeError("Node 安装包包含不安全路径")
            if member.issym() or member.islnk():
                linked = os.path.realpath(os.path.join(os.path.dirname(dest), member.linkname))
                if not (linked + os.sep).startswith(root):
                    raise RuntimeError("Node 安装包包含不安全链接")
        tf.extractall(target)


def _download(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "Desktron-Setup/1"})
    errors = []
    # 默认按环境代理设置下载;DESKTRON_SETUP_PROXY 可额外指定一个备用代理。
    openers = [urllib.request.build_opener()]
    managed = os.environ.get("DESKTRON_NETWORK_MODE") in ("system", "direct", "manual")
    fallback = "" if managed else os.environ.get("DESKTRON_SETUP_PROXY", "").strip()
    if fallback:
        openers.append(urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": fallback, "https": fallback,
        })))
    for opener in openers:
        try:
            with opener.open(request, timeout=60) as response, open(target, "wb") as out:
                shutil.copyfileobj(response, out)
            return
        except Exception as exc:
            errors.append(str(exc))
            try:
                os.unlink(target)
            except OSError:
                pass
    raise RuntimeError("Node 下载失败：%s" % (errors[-1][:180] if errors else "网络不可用"))


def install_node():
    existing = node_command()
    if existing:
        return existing
    from durable_json import locked
    with locked(os.path.join(_data_root(), 'runtime', 'node-install')):
        # prepare、install、引导页并发时，第二个调用复用已验证的安装。
        return _install_node_locked()


def _verify_archive(archive, checksums, name):
    expected = []
    with open(checksums, encoding='utf-8') as stream:
        for line in stream:
            fields = line.split()
            if len(fields) == 2 and fields[1].lstrip('*') == name:
                expected.append(fields[0])
    digest = hashlib.sha256()
    with open(archive, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if len(expected) != 1 or expected[0] != digest.hexdigest():
        raise RuntimeError('Node 安装包校验失败；未解包或替换运行时，请重新尝试')


def _install_node_locked():
    existing = node_command()
    if existing:
        return existing
    os_name, arch = _node_platform()
    name = "node-v%s-%s-%s" % (NODE_VERSION, os_name, arch)
    suffix = ".zip" if os_name == "win" else ".tar.gz"
    url = "https://nodejs.org/dist/v%s/%s%s" % (NODE_VERSION, name, suffix)
    runtime = os.path.join(_data_root(), "runtime")
    final = _managed_node_home()
    os.makedirs(runtime, exist_ok=True)
    work = tempfile.mkdtemp(prefix="node-setup-", dir=runtime)
    keep_recovery = False
    try:
        archive = os.path.join(work, "node" + suffix)
        _download(url, archive)
        checksums = os.path.join(work, 'SHASUMS256.txt')
        _download('https://nodejs.org/dist/v%s/SHASUMS256.txt' % NODE_VERSION, checksums)
        _verify_archive(archive, checksums, name + suffix)
        unpack = os.path.join(work, "unpack")
        os.makedirs(unpack)
        _safe_extract(archive, unpack)
        source = os.path.join(unpack, name)
        if not _node_ok(_node_exe(source)):
            raise RuntimeError("下载的 Node 无法运行")
        # 不删除既有目录；替换失败时将其恢复。备份只在本次替换成功后随 work 清理。
        previous = os.path.join(work, 'previous')
        if os.path.lexists(final):
            os.replace(final, previous)
        try:
            os.replace(source, final)
        except OSError:
            if os.path.lexists(previous):
                try:
                    os.replace(previous, final)
                except OSError as error:
                    keep_recovery = True
                    raise RuntimeError('Node 替换与恢复均失败；原目录保留于 ' + previous) from error
            raise
    finally:
        if not keep_recovery:
            shutil.rmtree(work, ignore_errors=True)
    return _node_exe(final)


def prepare_sync():
    node = install_node()
    node_bin = os.path.dirname(node)
    if node_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")
    return {"node": node}
