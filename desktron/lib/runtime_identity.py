"""Immutable identity of the running Desk backend process."""
import os
import re
import secrets

BOOT_ID = secrets.token_hex(12)
HOST_INSTANCE_ID = str(os.environ.get("DESKTRON_HOST_INSTANCE_ID") or "")
LAUNCH_GENERATION = str(os.environ.get("DESKTRON_LAUNCH_GENERATION") or "")
try:
    HOST_PID = int(os.environ.get("DESKTRON_NATIVE_HOST_PID") or 0)
except ValueError:
    HOST_PID = 0
# Read once and remove before any adapter copies os.environ into model processes.
HOST_CONTROL_TOKEN = os.environ.pop("DESKTRON_HOST_CONTROL_TOKEN", "")
SUPERVISOR = "native" if HOST_INSTANCE_ID and LAUNCH_GENERATION else (
    "systemd" if os.environ.get("INVOCATION_ID") else "")


def facts():
    """Facts used by both the native supervisor and browser runtime handshake."""
    return {
        "boot_id": BOOT_ID,
        "host_instance_id": HOST_INSTANCE_ID,
        "launch_generation": LAUNCH_GENERATION,
        "pid": os.getpid(),
        "host_pid": HOST_PID,
        "supervisor": SUPERVISOR,
        "supervised": bool(SUPERVISOR == "systemd" or (
            re.fullmatch(r"[0-9a-f-]{16,64}", HOST_INSTANCE_ID)
            and re.fullmatch(r"[0-9a-f-]{16,64}", LAUNCH_GENERATION)
        )),
    }
