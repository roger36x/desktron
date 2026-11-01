"""Frozen reader from 35a5efd4:self_update.py, for rollback wire compatibility.

Only _safe_int/_save_state dependencies are supplied by the test. Do not modernize
this function: new helpers must not trigger its version-only commit shortcut.
"""

def _reconcile_pending_commit(value, current_rev, current_build, host_rev, host_build):
    if value.get("state") != "accepted_pending_commit":
        return value
    if not (str(value.get("pending_rev") or "") == current_rev
            and _safe_int(value.get("pending_build")) == current_build
            and str(value.get("pending_host_rev") or "") == host_rev
            and _safe_int(value.get("pending_host_build")) == host_build):
        return value
    outcome = str(value.get("pending_outcome") or "")
    if outcome not in ("idle", "rolled_back"):
        return value
    value.update(state=outcome, current_rev=current_rev, current_build=current_build,
                 current_host_rev=host_rev, current_host_build=host_build,
                 apply_pid=0, operation_id="", acceptance_unknown=False,
                 pending_outcome="", pending_rev="", pending_build=0,
                 pending_host_rev="", pending_host_build=0)
    if outcome == "idle":
        value.update(hasUpdate=False, staged_rev="", staged_build=0,
                     staged_host_rev="", staged_host_build=0, error="", fix="")
    else:
        value["fix"] = "旧版已恢复"
    return _save_state(value)
