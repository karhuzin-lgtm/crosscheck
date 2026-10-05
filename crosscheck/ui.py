"""Terminal rendering for the standalone CLI: colors, findings, the stats card.

Color is used only when writing to a TTY, and never when ``NO_COLOR`` is set
(https://no-color.org). ``FORCE_COLOR`` forces it on (handy for recording demos).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, TextIO

from .review import Finding

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "accent": "38;5;173",  # crosscheck orange (#D2662F-ish)
    "on_red": "1;97;41",
    "on_yellow": "1;30;43",
    "on_blue": "1;97;44",
}

_SPARK = "▁▂▃▄▅▆▇█"


def use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class Painter:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        codes = ";".join(_CODES[s] for s in styles)
        return "\033[%sm%s\033[0m" % (codes, text)


_SEV_STYLE = {"blocker": "on_red", "warn": "on_yellow", "nit": "on_blue"}


def severity_tag(p: Painter, severity: str) -> str:
    label = " %s " % severity.upper().ljust(7)
    return p(label, _SEV_STYLE.get(severity, "on_blue"))


def finding_block(p: Painter, f: Finding, jury_size: int, blocking: bool) -> List[str]:
    loc = f.file + (":%d" % f.line if f.line else "")
    meta = [f.category]
    if jury_size > 1:
        n = len(f.reviewers)
        agree = "%d/%d agree" % (n, jury_size) if n > 1 else "only " + f.reviewers[0]
        meta.append(p(agree, "bold", "accent") if n > 1 else agree)
    elif f.reviewers:
        meta.append(f.reviewers[0])
    head = "%s %s  %s" % (severity_tag(p, f.severity), p(loc, "bold"), p(" · ".join(meta), "dim"))
    if not blocking:
        head += p("  (below threshold)", "dim")
    lines = [head, "          " + f.summary]
    if f.detail:
        for chunk in _wrap(f.detail, 76):
            lines.append("          " + p(chunk, "dim"))
    return lines


def _wrap(text: str, width: int) -> List[str]:
    out: List[str] = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split():
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = word if not line else line + " " + word
        out.append(line)
    return [l for l in out if l]


def sparkline(values: List[int]) -> str:
    peak = max(values) if values else 0
    if not peak:
        return _SPARK[0] * len(values)
    return "".join(_SPARK[min(len(_SPARK) - 1, int(v * (len(_SPARK) - 1) / peak))] if v else _SPARK[0] for v in values)


def _bar(n: int, total: int, width: int = 18) -> str:
    if not total:
        return ""
    filled = max(1, int(round(width * n / total))) if n else 0
    return "█" * filled


def stats_card(p: Painter, data: Dict[str, Any], spark: List[int]) -> str:
    """A boxed, screenshot-friendly summary of everything crosscheck caught."""
    w = 52
    rows: List[str] = []

    def row(text: str = "", visible: Optional[int] = None) -> None:
        pad = w - (visible if visible is not None else len(text))
        rows.append(p("│", "dim") + " " + text + " " * max(0, pad) + " " + p("│", "dim"))

    caught = data["caught"]
    since = data.get("since") or "today"
    rows.append(p("╭" + "─" * (w + 2) + "╮", "dim"))
    title = "crosscheck · second-opinion AI code review"
    row(p(title, "bold"), len(title))
    row()
    big = "%d issue%s caught" % (caught, "" if caught == 1 else "s")
    row(p(big, "bold", "accent"), len(big))
    sub = "before they shipped · since %s" % since
    row(p(sub, "dim"), len(sub))
    row()
    line = "%d reviews   %d blocked   %d confirmed by 2+ models" % (
        data["reviews"], data["blocked"], data["agreed"])
    row(line)
    row()
    sev = data["by_severity"]
    for name in ("blocker", "warn", "nit"):
        n = sev.get(name, 0)
        if not n:
            continue
        label = "%-8s %4d  " % (name, n)
        bar = _bar(n, caught)
        row(label + p(bar, "accent"), len(label) + len(bar))
    cats = sorted(data["by_category"].items(), key=lambda kv: -kv[1])[:4]
    if cats:
        row()
        text = "  ".join("%s %d" % (c, n) for c, n in cats)
        row(text)
    revs = sorted(data["by_reviewer"].items(), key=lambda kv: -kv[1])[:4]
    if revs:
        text = "by " + ", ".join("%s (%d)" % (r, n) for r, n in revs)
        row(p(text[:w], "dim"), min(len(text), w))
    row()
    s = "last 14 days  " + sparkline(spark)
    row(s)
    rows.append(p("╰" + "─" * (w + 2) + "╯", "dim"))
    return "\n".join(rows)


def out(text: str = "", stream: Optional[TextIO] = None) -> None:
    (stream or sys.stdout).write(text + "\n")
