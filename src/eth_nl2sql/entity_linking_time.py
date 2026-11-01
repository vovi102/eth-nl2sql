"""Time expressions of a question (VI/EN) -> a [start, end) UTC interval.

Patterns run on a folded copy of the question (casefolded, without diacritics, one character
for each character of the question), so spans are positions in the question itself and
Vietnamese typed without diacritics matches the same patterns. A question without a time
expression has time = null, i.e. the whole month (docs/linking-labels.md §4).

Span convention: Vietnamese spans leave out "trong"/"vào" and keep "ngày"/"từ"; English spans
keep a leading "on"/"in"/"during" (example 3 of linking-labels.md: "on September 5").
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from eth_nl2sql.entity_knowledge_base import normalize_alias

DEFAULT_YEAR = 2026

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12,
}  # fmt: skip
ORDINALS = {
    # Vietnamese
    "dau": 1, "dau tien": 1, "thu nhat": 1, "thu 1": 1, "thu hai": 2, "thu 2": 2,
    "thu ba": 3, "thu 3": 3, "thu tu": 4, "thu 4": 4, "cuoi": -1, "cuoi cung": -1,
    # English
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
    "last": -1, "final": -1,
}  # fmt: skip

_MONTH = r"(?P<{}>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
# A bare month name needs "in"/"during" or a year when it is also an English word ("may").
_BARE_MONTH = (
    r"(?:(?:in|during)\s+"
    + _MONTH.format("mn")
    + r"|(?P<mn_alone>"
    + "|".join(sorted(set(MONTHS) - {"may", "mar"}, key=len, reverse=True))
    + r")\.?)"
)
_DAY = r"(?P<{}>\d{{1,2}})(?:st|nd|rd|th)?"
_YEAR_EN = r"(?:,?\s+(?P<year>\d{4}))?"
_YEAR_VI = r"(?:(?:/|\s+nam\s+)(?P<year>\d{4}))?"
_EN_PREP = r"(?:(?:on|in|during)\s+)?(?:the\s+)?"
_VI_ORD = "dau tien|thu nhat|thu hai|thu ba|thu tu|thu 1|thu 2|thu 3|thu 4|cuoi cung|dau|cuoi"
_EN_ORD = "first|second|third|fourth|last|final|1st|2nd|3rd|4th"


def _p(pattern: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + pattern + r"(?!\w)")


# (kind, pattern); kinds are turned into intervals by _interval.
PATTERNS = [
    # ranges
    ("range", _p(r"(?:tu\s+)?(?:ngay\s+)?" + _DAY.format("d1") + r"(?:/(?P<m1>\d{1,2}))?"
                 r"\s*(?:den|toi|-)\s*(?:het\s+)?(?:ngay\s+)?" + _DAY.format("d2")
                 + r"(?:/|\s+thang\s+)(?P<m2>\d{1,2})" + _YEAR_VI)),
    ("range", _p(r"(?:(?:from|between)\s+)?" + _MONTH.format("mn1") + r"\s+" + _DAY.format("d1")
                 + r"\s*(?:to|until|through|and|-)\s*(?:" + _MONTH.format("mn2") + r"\s+)?"
                 + _DAY.format("d2") + _YEAR_EN)),
    ("range", _p(r"(?:(?:from|between)\s+)?(?:the\s+)?" + _DAY.format("d1")
                 + r"\s*(?:to|until|through|and|-)\s*(?:the\s+)?" + _DAY.format("d2")
                 + r"\s+(?:of\s+)?" + _MONTH.format("mn2") + _YEAR_EN)),
    # weeks, halves, first/last N days
    ("week", _p(r"tuan\s+(?P<ord>" + _VI_ORD + r")\s+(?:cua\s+)?thang\s+(?P<m>\d{1,2})"
                + _YEAR_VI)),
    ("week", _p(_EN_PREP + r"(?P<ord>" + _EN_ORD + r")\s+week\s+of\s+" + _MONTH.format("mn")
                + _YEAR_EN)),
    ("half", _p(r"nua\s+(?P<ord>dau|cuoi|sau)\s+(?:cua\s+)?thang\s+(?P<m>\d{1,2})" + _YEAR_VI)),
    ("half", _p(_EN_PREP + r"(?P<ord>first|second|last)\s+half\s+of\s+" + _MONTH.format("mn")
                + _YEAR_EN)),
    ("days", _p(r"(?P<n>\d{1,2})\s+ngay\s+(?P<ord>dau|cuoi)\s+(?:cua\s+)?thang\s+"
                r"(?P<m>\d{1,2})" + _YEAR_VI)),
    ("days", _p(_EN_PREP + r"(?P<ord>first|last)\s+(?P<n>\d{1,2})\s+days\s+of\s+"
                + _MONTH.format("mn") + _YEAR_EN)),
    # single days
    ("day", _p(r"(?:(?:ngay|mung|hom)\s+)?" + _DAY.format("d") + r"(?:/|\s+thang\s+)"
               r"(?P<m>\d{1,2})" + _YEAR_VI)),
    ("day", _p(_EN_PREP + _MONTH.format("mn") + r"\s+" + _DAY.format("d") + _YEAR_EN)),
    ("day", _p(_EN_PREP + _DAY.format("d") + r"\s+(?:of\s+)?" + _MONTH.format("mn") + _YEAR_EN)),
    # whole months
    ("month", _p(r"thang\s+(?P<m>\d{1,2})" + _YEAR_VI)),
    ("month", _p(_BARE_MONTH + r"(?:,?\s+(?P<year>\d{4}))?")),
]  # fmt: skip


def fold(text: str) -> str:
    """normalize_alias character by character, keeping one character per character."""
    out = []
    for c in text:
        n = normalize_alias(c)
        out.append(n if len(n) == 1 else c.lower() if len(c.lower()) == 1 else c)
    return "".join(out)


def _month_end(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1)


def _interval(kind: str, g: dict) -> tuple[date, date] | None:
    year = int(g.get("year") or DEFAULT_YEAR)
    month = int(g["m"]) if g.get("m") else MONTHS.get(g.get("mn") or g.get("mn_alone") or "", 0)
    try:
        if kind == "range":
            m1 = int(g["m1"]) if g.get("m1") else MONTHS.get(g.get("mn1") or "")
            m2 = int(g["m2"]) if g.get("m2") else MONTHS.get(g.get("mn2") or "") or m1
            start, last = date(year, m1 or m2, int(g["d1"])), date(year, m2, int(g["d2"]))
            return (start, last + timedelta(days=1)) if start <= last else None
        first, end = date(year, month, 1), _month_end(year, month)
        if kind == "month":
            return first, end
        if kind == "day":
            day = date(year, month, int(g["d"]))
            return day, day + timedelta(days=1)
        if kind == "week":
            k = ORDINALS[g["ord"]]
            if k == -1:
                return end - timedelta(days=7), end
            start = first + timedelta(days=7 * (k - 1))
            return start, min(start + timedelta(days=7), end)
        if kind == "half":
            middle = first + timedelta(days=15)
            return (first, middle) if g["ord"] in ("dau", "first") else (middle, end)
        if kind == "days":
            n = timedelta(days=int(g["n"]))
            return (first, first + n) if g["ord"] in ("dau", "first") else (end - n, end)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def parse_time(question: str) -> dict | None:
    """The question's time expression as {start, end, span, text}, or None.

    The longest expression wins (the earliest on a tie); a question has one interval.
    """
    folded = fold(question)
    best = None
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(folded):
            interval = _interval(kind, m.groupdict())
            if interval is None:
                continue
            key = (m.end() - m.start(), -m.start())
            if best is None or key > best[0]:
                best = (key, m.start(), m.end(), interval)
    if best is None:
        return None
    _, start, end, (first, stop) = best
    return {
        "start": datetime.combine(first, datetime.min.time()).isoformat(),
        "end": datetime.combine(stop, datetime.min.time()).isoformat(),
        "span": [start, end],
        "text": question[start:end],
    }
