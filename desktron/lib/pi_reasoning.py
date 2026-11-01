"""Display policy for provider-specific Pi reasoning payloads."""


def is_openai_reasoning_provider(provider):
    """Whether Pi identifies the model as the OpenAI reasoning provider."""
    return str(provider or "").strip() == "openai"


def _is_markdown_summary_title(line):
    for marker in ("**", "__"):
        if line.startswith(marker) and line.endswith(marker):
            inner = line[len(marker):-len(marker)].strip()
            return bool(inner) and marker not in inner
    return False


def is_openai_summary_titles_only(provider, text):
    """Whether Pi exposed only OpenAI summary titles, without visible reasoning."""
    if not is_openai_reasoning_provider(provider):
        return False
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and all(_is_markdown_summary_title(line) for line in lines)
