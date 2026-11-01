"""Stable model ordering shared by engine adapters."""
import re


_NAMED_RELEASE_ORDER = {"sol": 0, "terra": 1, "luna": 2}


def openai_model_sort_key(model):
    """Put newer GPT releases first, then apply the product's named-release order."""
    route = str(model.get("model") or model.get("id") or "").lower().rsplit("/", 1)[-1]
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(.*)$", route)
    if not match:
        return (1, 0, 0, 0, 0)
    suffix = match.group(3).strip("-_.")
    tokens = suffix.split("-") if suffix else []
    if not tokens:
        variant, named = 0, 0
    elif tokens[0] == "mini":
        variant, named = 1, 0
    elif "codex" in tokens:
        variant, named = 2, 0
    else:
        variant, named = 3, _NAMED_RELEASE_ORDER.get(tokens[0], len(_NAMED_RELEASE_ORDER))
    return (0, -int(match.group(1)), -int(match.group(2) or 0), variant, named)


def order_gpt_model_slots(rows):
    """Sort GPT rows without moving unrelated providers from their visible positions."""
    ordered = list(rows)
    slots = [index for index, model in enumerate(ordered)
             if openai_model_sort_key(model)[0] == 0]
    models = sorted((ordered[index] for index in slots), key=openai_model_sort_key)
    for index, model in zip(slots, models):
        ordered[index] = model
    return ordered
