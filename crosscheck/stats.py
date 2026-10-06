"""Local, counts-only tally of what crosscheck has caught.

Powers ``crosscheck stats``: a terminal card, a README badge, and an SVG card.
Only NUMBERS are stored — no code, no file paths, no finding text, no repo names
— so the stats file is safe to look at, share, or delete. Nothing is ever sent
anywhere. Disable with ``CROSSCHECK_STATS=0``.

Storage: ``$XDG_STATE_HOME/crosscheck/stats.json`` (default
``~/.local/state/crosscheck``; ``%LOCALAPPDATA%\\crosscheck`` on Windows), in a
private 0700 directory, written atomically. Every function here NEVER raises:
stats are a nicety and must never break a review.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from typing import Any, Dict, Iterable, List, Optional

from .review import Finding
from .state import _ensure_private_dir

_MAX_BYTES = 256_000
_KEEP_DAYS = 120
_VERSION = 1


def _base_dir() -> str:
    override = os.environ.get("CROSSCHECK_STATE_DIR")
    if override:
        return override
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "crosscheck")
    root = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(root, "crosscheck")


def path() -> str:
    return os.path.join(_base_dir(), "stats.json")


def empty() -> Dict[str, Any]:
    return {
        "version": _VERSION,
        "since": None,
        "reviews": 0,
        "blocked": 0,
        "caught": 0,
        "findings": 0,
        "by_severity": {},
        "by_category": {},
        "by_reviewer": {},
        "agreed": 0,
        "daily": {},
    }


def _count_map(value: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[k[:40]] = v
    return out


def load() -> Dict[str, Any]:
    """Return the stats dict (a fresh empty one if absent or unreadable)."""
    data = empty()
    try:
        p = path()
        if os.path.islink(p) or os.path.getsize(p) > _MAX_BYTES:
            return data
        with open(p, "rb") as fh:
            raw = json.loads(fh.read(_MAX_BYTES).decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return data
    if not isinstance(raw, dict):
        return data
    for key in ("reviews", "blocked", "caught", "findings", "agreed"):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            data[key] = v
    if isinstance(raw.get("since"), str):
        data["since"] = raw["since"][:10]
    for key in ("by_severity", "by_category", "by_reviewer", "daily"):
        data[key] = _count_map(raw.get(key))
    return data


def _save(data: Dict[str, Any]) -> None:
    base = _base_dir()
    if not _ensure_private_dir(base):
        return
    fd, tmp = tempfile.mkstemp(prefix=".stats-", dir=base)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path())
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _today() -> str:
    return datetime.date.today().isoformat()


def record(
    findings: Iterable[Finding],
    caught: Iterable[Finding],
    reviewers: Iterable[str],
    blocked: bool,
) -> None:
    """Add one review's counts to the tally. Never raises."""
    try:
        findings, caught = list(findings), list(caught)
        data = load()
        today = _today()
        data["since"] = data["since"] or today
        data["reviews"] += 1
        data["blocked"] += 1 if blocked else 0
        data["findings"] += len(findings)
        data["caught"] += len(caught)
        data["agreed"] += sum(1 for f in caught if len(f.reviewers) > 1)
        for f in caught:
            for key, name in (("by_severity", f.severity), ("by_category", f.category)):
                data[key][name] = data[key].get(name, 0) + 1
            for r in f.reviewers:
                data["by_reviewer"][r] = data["by_reviewer"].get(r, 0) + 1
        data["daily"][today] = data["daily"].get(today, 0) + len(caught)
        cutoff = (datetime.date.today() - datetime.timedelta(days=_KEEP_DAYS)).isoformat()
        data["daily"] = {d: n for d, n in data["daily"].items() if d >= cutoff}
        _save(data)
    except Exception:  # noqa: BLE001 - stats must never break a review
        pass


def reset() -> bool:
    try:
        os.unlink(path())
        return True
    except OSError:
        return False


def last_days(data: Dict[str, Any], days: int = 14) -> List[int]:
    """Caught-per-day for the last ``days`` days, oldest first."""
    today = datetime.date.today()
    out = []
    for back in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=back)).isoformat()
        out.append(data["daily"].get(d, 0))
    return out


def badge_url(data: Dict[str, Any]) -> str:
    n = data["caught"]
    label = "%d issue%s caught" % (n, "" if n == 1 else "s")
    return "https://img.shields.io/badge/crosscheck-%s-D2662F" % label.replace(" ", "%20")


def badge_markdown(data: Dict[str, Any], link: str = "https://github.com/karhuzin-lgtm/crosscheck") -> str:
    return "[![crosscheck](%s)](%s)" % (badge_url(data), link)


def _esc(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def svg_card(data: Dict[str, Any], title: Optional[str] = None) -> str:
    """A self-contained 480x200 SVG card, light/dark aware, for READMEs and posts."""
    days = last_days(data, 30)
    peak = max(days) or 1
    bars = []
    for i, n in enumerate(days):
        h = max(3, int(round(36 * n / peak))) if n else 2
        bars.append(
            '<rect x="%d" y="%d" width="10" height="%d" rx="2" class="%s"/>'
            % (24 + i * 14, 176 - h, h, "bar" if n else "bar0")
        )
    cats = sorted(data["by_category"].items(), key=lambda kv: -kv[1])[:3]
    cat_text = "  ·  ".join("%d %s" % (n, c) for c, n in cats) or "no issues yet"
    title = title or "crosscheck"
    return """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="200" viewBox="0 0 480 200" role="img" aria-label="{aria}">
<style>
  .bg{{fill:#fbfaf8;stroke:#e6e1da}} .t{{fill:#1a1a1a}} .m{{fill:#6b6560}} .a{{fill:#D2662F}}
  .bar{{fill:#D2662F}} .bar0{{fill:#e6e1da}}
  text{{font-family:ui-sans-serif,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}}
  @media (prefers-color-scheme: dark){{
    .bg{{fill:#1c1b1a;stroke:#33302d}} .t{{fill:#f3efe9}} .m{{fill:#a39d96}} .bar0{{fill:#33302d}}
  }}
</style>
<rect class="bg" x="0.5" y="0.5" width="479" height="199" rx="12"/>
<text class="m" x="24" y="34" font-size="13">{title} · independent AI code review</text>
<text class="a" x="24" y="80" font-size="40" font-weight="700">{caught}</text>
<text class="t" x="{tx}" y="80" font-size="16">issues caught before they shipped</text>
<text class="m" x="24" y="104" font-size="12">{reviews} reviews · {blocked} blocked · {agreed} confirmed by 2+ models</text>
<text class="m" x="24" y="124" font-size="12">{cats}</text>
{bars}
<text class="m" x="456" y="192" font-size="10" text-anchor="end">last 30 days</text>
</svg>
""".format(
        aria=_esc("crosscheck: %d issues caught" % data["caught"]),
        title=_esc(title),
        caught=data["caught"],
        tx=24 + 26 * len(str(data["caught"])) + 10,
        reviews=data["reviews"],
        blocked=data["blocked"],
        agreed=data["agreed"],
        cats=_esc(cat_text),
        bars="\n".join(bars),
    )
