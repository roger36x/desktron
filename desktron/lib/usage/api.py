"""Read-only token usage endpoint for the settings page."""
import urllib.parse

from deskcfg import DATA_HOME
from usage_stats import aggregate
from workbench import _wb_domains_read


def owns(path, method):
    return path == "/workbench/usage" and method == "GET"


def handle(handler, path, query, method):
    params = urllib.parse.parse_qs(query)
    try:
        days = int((params.get("days") or ["365"])[0])
    except (TypeError, ValueError):
        days = 365
    domains = {str(row.get("id")): str(row.get("name") or row.get("id"))
               for row in _wb_domains_read() if isinstance(row, dict) and row.get("id")}
    return handler._send_json(aggregate(DATA_HOME, domains, days=days))
