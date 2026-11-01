"""Session-scoped compaction thresholds for Shepherd-owned Pi hosts.

Pi 0.85.1 compacts when context tokens exceed ``contextWindow - reserveTokens``.
Shepherd's launcher (pi_memory_rpc.mjs) owns a private in-memory settings
manager, so a per-session threshold T is applied by setting
``reserveTokens = contextWindow - T`` before each turn. Native Pi sessions do not
run through that launcher and keep Pi's own threshold.
"""

PI_AUTO_COMPACTS = (("200000", "20 万"), ("400000", "40 万"), ("600000", "60 万"))
_ALLOWED = {key for key, _label in PI_AUTO_COMPACTS}
# A manual threshold still needs room for the kept tail and generated summary.
# 64K is conservative enough for Pi's native 20K kept tail and 16K default
# reserve, while allowing a 272K OpenAI route to offer the useful 200K tier.
PI_COMPACTION_MIN_HEADROOM = 65536


def normalize_pi_autocompact(value):
    """Return a supported threshold id, or '' for Pi's native automatic threshold."""
    value = str(value or "").strip()
    return value if value in _ALLOWED else ""


def pi_compaction_threshold(options):
    """Token threshold sent to the launcher; 0 restores Pi's native reserve."""
    value = normalize_pi_autocompact((options or {}).get("autocompact"))
    return int(value) if value else 0


def pi_compaction_frame(options):
    """Control frame restated before every Pi turn.

    The launcher rechecks the live model window and the actual native reserve. A
    saved tier therefore stays inert when the selected route is too small and
    becomes active again after switching back to a compatible route.
    """
    return {"type": "desk_compaction", "threshold": pi_compaction_threshold(options),
            "minHeadroom": PI_COMPACTION_MIN_HEADROOM}


def pi_autocompact_choices(context_window=None):
    """Return only thresholds that fit the model, always retaining native auto."""
    automatic = [{"id": "", "label": "自动"}]
    try:
        window = int(context_window or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return automatic
    return automatic + [{"id": key, "label": label}
                        for key, label in PI_AUTO_COMPACTS
                        if window - int(key) >= PI_COMPACTION_MIN_HEADROOM]


def annotate_pi_catalog(catalog):
    """Attach tiers to each qualifying model or route; others get no menu at all."""
    if not isinstance(catalog, dict):
        return catalog
    def mark(row):
        if not isinstance(row, dict):
            return row
        out = dict(row)
        out.pop("autocompacts", None)
        if isinstance(row.get("variants"), list):
            out["variants"] = [mark(route) for route in row["variants"]]
        context_window = out.get("contextWindow")
        if context_window:
            out["autocompacts"] = pi_autocompact_choices(context_window)
        return out
    out = dict(catalog)
    out["models"] = [mark(row) for row in catalog.get("models") or []]
    ui = dict(catalog.get("ui") or {})
    ui.pop("autocompacts", None)
    out["ui"] = ui
    return out
