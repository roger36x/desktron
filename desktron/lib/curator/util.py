"""通用小工具:错误类型、id 校验、日期、原子写。"""

from __future__ import annotations

import datetime
import os
import re
import tempfile

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class CuratorError(Exception):
    """引擎层可预期错误。cli 捕获后以非零码退出,不打栈。"""


def valid_id(s: str) -> bool:
    return bool(ID_RE.match(s))


def slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def today() -> str:
    return datetime.date.today().isoformat()


def parse_date(s: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def months_between(earlier: datetime.date, later: datetime.date) -> float:
    return (later - earlier).days / 30.44


def add_months(d: datetime.date, n: int) -> datetime.date:
    import calendar

    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


_DUR_RE = re.compile(r"^(\d+)(d|w|mo|m)$")


def add_duration(d: datetime.date, spec: str) -> datetime.date:
    """把时长 spec(14d / 2w / 6mo)加到日期 d;非法格式抛 CuratorError。"""
    m = _DUR_RE.match(str(spec).strip())
    if not m:
        raise CuratorError(f"时长格式非法:{spec}(用 <N>d / <N>w / <N>mo)")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return d + datetime.timedelta(days=n)
    if unit == "w":
        return d + datetime.timedelta(weeks=n)
    return add_months(d, n)  # m / mo


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
