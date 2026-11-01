#!/usr/bin/env python3
"""Create or select a stable macOS code-signing identity and sign Desktron.app."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile

BUNDLE_ID = "io.github.desktron"
LOCAL_IDENTITY_LABEL = "Desktron Local Code Signing"
_IDENTITY_RE = re.compile(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.*)"\s*$')


class CodeSignError(RuntimeError):
    pass


def _run(args, *, timeout=60, check=True):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                check=False, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodeSignError(f"签名命令无法完成：{Path(args[0]).name}") from error
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "未知错误").strip()[-800:]
        raise CodeSignError(f"签名命令失败：{Path(args[0]).name}：{detail}")
    return result


def code_signing_identities():
    result = _run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
                  timeout=15)
    identities = []
    for line in result.stdout.splitlines():
        match = _IDENTITY_RE.match(line)
        if match:
            identities.append({"hash": match.group(1).upper(), "name": match.group(2)})
    return identities


def select_identity(env=None):
    env = os.environ if env is None else env
    identities = code_signing_identities()
    explicit = str(env.get("DESKTRON_CODESIGN_IDENTITY") or "").strip()
    if explicit:
        matches = [row for row in identities
                   if explicit.lower() in (row["hash"].lower(), row["name"].lower())]
        if len(matches) != 1:
            raise CodeSignError("DESKTRON_CODESIGN_IDENTITY 未唯一匹配有效代码签名身份")
        return matches[0]["hash"]
    developer = [row for row in identities if row["name"].startswith("Developer ID Application:")]
    if len(developer) == 1:
        return developer[0]["hash"]
    if len(developer) > 1:
        raise CodeSignError("存在多个 Developer ID Application 身份，请设置 DESKTRON_CODESIGN_IDENTITY")
    local = [row for row in identities if row["name"] == LOCAL_IDENTITY_LABEL]
    if len(local) == 1:
        return local[0]["hash"]
    if len(local) > 1:
        raise CodeSignError("存在多个 Desktron 本地签名身份，请设置 DESKTRON_CODESIGN_IDENTITY")
    return ""


def _login_keychain():
    result = _run(["/usr/bin/security", "default-keychain", "-d", "user"], timeout=15)
    path = result.stdout.strip().strip('"')
    if not path:
        raise CodeSignError("找不到用户默认钥匙串")
    return path


def create_local_identity():
    """Create the dedicated identity once; macOS may show a trust confirmation."""
    if sys.platform != "darwin":
        raise CodeSignError("稳定代码签名身份只能在 macOS 上创建")
    if select_identity():
        raise CodeSignError("已有可用稳定签名身份，不应重复创建")
    keychain = _login_keychain()
    with tempfile.TemporaryDirectory(prefix="desktron-codesign-") as folder:
        root = Path(folder)
        config = root / "openssl.cnf"
        key = root / "identity.key"
        cert = root / "identity.pem"
        archive = root / "identity.p12"
        config.write_text("""[req]
prompt = no
distinguished_name = subject
x509_extensions = extensions
[subject]
CN = Desktron Local Code Signing
[extensions]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = codeSigning
subjectKeyIdentifier = hash
""", encoding="utf-8")
        _run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
              "-days", "3650", "-config", str(config), "-keyout", str(key),
              "-out", str(cert)])
        _run(["/usr/bin/openssl", "pkcs12", "-export", "-inkey", str(key),
              "-in", str(cert), "-out", str(archive), "-passout", "pass:"])
        _run(["/usr/bin/security", "import", str(archive), "-f", "pkcs12",
              "-k", keychain, "-P", "", "-T", "/usr/bin/codesign"])
        _run(["/usr/bin/security", "add-trusted-cert", "-r", "trustRoot",
              "-p", "codeSign", "-k", keychain, str(cert)])
    identity = select_identity()
    if not identity:
        raise CodeSignError("本地签名身份已创建但未被 macOS 识别为有效代码签名身份")
    return identity


def stable_requirement(requirement):
    text = str(requirement or "").strip()
    return (text.startswith("designated =>") and "cdhash" not in text
            and f'identifier "{BUNDLE_ID}"' in text
            and ("certificate" in text or "anchor" in text))


def _bundle_identifier(app):
    plist = Path(app) / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as stream:
            return str(plistlib.load(stream).get("CFBundleIdentifier") or "")
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise CodeSignError("Desktron.app 的 Info.plist 无法读取") from error


def signature_requirement(app):
    result = _run(["/usr/bin/codesign", "-d", "-r-", os.fspath(app)], timeout=20)
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        text = line.strip()
        if text.startswith("# "):
            text = text[2:].lstrip()
        if text.startswith("designated =>"):
            return text
    return ""


def _signature_metadata(app):
    result = _run(["/usr/bin/codesign", "-dv", "--verbose=4", app],
                  timeout=20, check=False)
    text = result.stdout + "\n" + result.stderr
    lines = [line.strip() for line in text.splitlines()]
    is_adhoc = any(line.lower() == "signature=adhoc" for line in lines)
    has_authority = any(line.lower().startswith("authority=") for line in lines)
    return result, text, is_adhoc, has_authority


def verify_stable_app(app):
    app = os.path.realpath(os.fspath(app))
    if _bundle_identifier(app) != BUNDLE_ID:
        raise CodeSignError(f"应用 bundle id 必须是 {BUNDLE_ID}")
    metadata, _, is_adhoc, has_authority = _signature_metadata(app)
    if metadata.returncode or is_adhoc or not has_authority:
        raise CodeSignError("Desktron.app 必须使用可验证的证书身份，拒绝 ad-hoc 签名")
    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
          "--all-architectures", app], timeout=30)
    requirement = signature_requirement(app)
    if not stable_requirement(requirement):
        raise CodeSignError("Desktron.app 指定要求不是基于稳定证书，拒绝 ad-hoc/cdhash 签名")
    return requirement


def installed_requirement_or_legacy(app):
    """Return a stable requirement or empty only for proven unsigned/ad-hoc apps."""
    app = os.path.realpath(os.fspath(app))
    if _bundle_identifier(app) != BUNDLE_ID:
        raise CodeSignError(f"当前应用 bundle id 必须是 {BUNDLE_ID}")
    metadata, metadata_text, is_adhoc, has_authority = _signature_metadata(app)
    if metadata.returncode:
        if "code object is not signed at all" in metadata_text.lower():
            return ""
        raise CodeSignError("无法读取当前应用签名元数据，拒绝更换稳定身份")
    if is_adhoc and not has_authority:
        # Linker signatures, ordinary ad-hoc bundles and locally modified ad-hoc
        # bundles are all legacy identities. Their strict resource verification
        # may fail, but replacing them once with a verified stable candidate is safe.
        return ""
    if not has_authority:
        raise CodeSignError("当前应用不是可迁移的未签名、ad-hoc 或稳定证书安装")
    verification = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                         "--all-architectures", app], timeout=30, check=False)
    if verification.returncode:
        raise CodeSignError("无法验证当前稳定应用签名，拒绝更换身份")
    requirement = signature_requirement(app)
    if stable_requirement(requirement):
        return requirement
    raise CodeSignError("当前应用指定要求不是稳定的证书身份")


def read_requirement_marker(path):
    path = os.fspath(path or "")
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as stream:
            value = stream.read(8193).strip()
    except OSError as error:
        raise CodeSignError("无法读取已记录的稳定签名身份") from error
    if len(value) > 8192 or not stable_requirement(value):
        raise CodeSignError("已记录的稳定签名身份无效")
    return value


def record_requirement(app, marker):
    requirement = verify_stable_app(app)
    previous = read_requirement_marker(marker)
    if previous and previous != requirement:
        raise CodeSignError("拒绝覆盖已记录的另一稳定签名身份")
    marker = os.path.realpath(os.fspath(marker))
    parent = os.path.dirname(marker)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = f"{marker}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(requirement + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, marker)
        if os.name != "nt":
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as error:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise CodeSignError("无法保存稳定签名身份") from error
    return requirement


def verify_compatible_app(candidate, current="", requirement_marker=""):
    """Verify candidate and preserve an existing or recorded stable identity."""
    candidate_requirement = verify_stable_app(candidate)
    expected_requirement = read_requirement_marker(requirement_marker)
    current = os.fspath(current or "")
    current_requirement = ""
    if current and os.path.exists(current):
        current_requirement = installed_requirement_or_legacy(current)
    if expected_requirement:
        if current and current_requirement != expected_requirement:
            raise CodeSignError("当前应用签名与已记录的稳定身份不一致")
        if candidate_requirement != expected_requirement:
            raise CodeSignError("候选应用签名身份与已记录的稳定身份不一致")
    elif current_requirement and current_requirement != candidate_requirement:
        raise CodeSignError("候选应用签名身份与当前稳定安装不一致")
    return candidate_requirement


def sign_app(app, env=None):
    if sys.platform != "darwin":
        raise CodeSignError("Desktron.app 只能在 macOS 上签名")
    app = os.path.realpath(os.fspath(app))
    identity = select_identity(env)
    if not identity:
        identity = create_local_identity()
    _run(["/usr/bin/codesign", "--force", "--deep", "--sign", identity,
          "--identifier", BUNDLE_ID, "--timestamp=none", app], timeout=120)
    return verify_stable_app(app)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("sign", "verify"):
        item = commands.add_parser(command)
        item.add_argument("app")
    compatible = commands.add_parser("verify-compatible")
    compatible.add_argument("candidate")
    compatible.add_argument("current", nargs="?", default="")
    compatible.add_argument("--requirement-marker", default="")
    record = commands.add_parser("record-requirement")
    record.add_argument("app")
    record.add_argument("marker")
    commands.add_parser("setup")
    args = parser.parse_args(argv)
    try:
        if args.command == "sign":
            sign_app(args.app)
            print("desktron: 原生壳已使用稳定证书签名")
        elif args.command == "verify":
            verify_stable_app(args.app)
            print("desktron: 原生壳稳定签名有效")
        elif args.command == "verify-compatible":
            verify_compatible_app(args.candidate, args.current, args.requirement_marker)
            print("desktron: 原生壳稳定签名身份连续")
        elif args.command == "record-requirement":
            record_requirement(args.app, args.marker)
            print("desktron: 原生壳稳定签名身份已记录")
        else:
            identity = select_identity()
            if not identity:
                create_local_identity()
            print("desktron: 稳定代码签名身份已就绪")
        return 0
    except CodeSignError as error:
        print(f"desktron: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
