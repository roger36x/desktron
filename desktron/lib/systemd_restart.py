#!/usr/bin/env python3
"""One-shot systemd-owned backend stop with exact Linux process identity."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import argparse
import json
import os
import signal
import time
import urllib.request


def atomic_result(path, operation_id, state, error='', *, boot_id='', pid=0):
    if not path:
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary=path+'.%d.tmp'%os.getpid()
    with open(temporary,'w',encoding='utf-8') as handle:
        json.dump({'operation_id':operation_id,'state':state,'error':error,
                   'host_instance_id':'','request_launch_generation':'',
                   'request_boot_id':boot_id,'request_pid':pid},handle)
        handle.write('\n')
    os.chmod(temporary,0o600);os.replace(temporary,path)


def start_token(pid):
    try:
        # /proc/<pid>/stat field 22; command may contain spaces and parentheses.
        tail = open('/proc/%d/stat' % pid, encoding='utf-8').read().rsplit(')', 1)[1].split()
        return tail[19]
    except Exception:
        return ''


def probe(port):
    with urllib.request.urlopen('http://127.0.0.1:%d/desk-probe' % port, timeout=2) as response:
        return json.load(response)


def stop(expected_boot, expected_pid, expected_token, port=7765, timeout=4,
         result_path='', operation_id=''):
    try:
        current = probe(port)
    except Exception as exc:
        atomic_result(result_path,operation_id,'rejected','probe_failed:'+type(exc).__name__,
                      boot_id=expected_boot,pid=expected_pid)
        return 2
    if (current.get('boot_id') != expected_boot
            or int(current.get('pid') or 0) != expected_pid
            or start_token(expected_pid) != expected_token):
        atomic_result(result_path,operation_id,'rejected','identity_mismatch',
                      boot_id=expected_boot,pid=expected_pid)
        return 3
    atomic_result(result_path,operation_id,'stopping',boot_id=expected_boot,pid=expected_pid)
    try:
        os.kill(expected_pid, signal.SIGTERM)
    except ProcessLookupError:
        atomic_result(result_path,operation_id,'old_stopped',boot_id=expected_boot,pid=expected_pid)
        return 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if start_token(expected_pid) != expected_token:
            atomic_result(result_path,operation_id,'old_stopped',boot_id=expected_boot,pid=expected_pid)
            return 0
        time.sleep(.05)
    if start_token(expected_pid) == expected_token:
        try: os.kill(expected_pid, signal.SIGKILL)
        except ProcessLookupError: pass
    atomic_result(result_path,operation_id,'old_stopped',boot_id=expected_boot,pid=expected_pid)
    return 0


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--boot',required=True);parser.add_argument('--pid',required=True,type=int)
    parser.add_argument('--start-token',required=True);parser.add_argument('--port',type=int,default=7765)
    parser.add_argument('--result',default='');parser.add_argument('--operation-id',default='')
    args=parser.parse_args(argv)
    return stop(args.boot,args.pid,args.start_token,args.port,result_path=args.result,
                operation_id=args.operation_id)


if __name__=='__main__':raise SystemExit(main())
