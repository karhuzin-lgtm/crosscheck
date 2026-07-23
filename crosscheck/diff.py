"""Compute the review diff from the working tree.

The review target is everything that differs from ``HEAD`` (staged + unstaged),
so the reviewer sees exactly what the turn produced. Files are filtered by the
configured include/exclude globs, and the whole diff is truncated to a byte
budget so a huge change set can't blow up the reviewer prompt.

Untracked new files are included too: a brand-new file (possibly holding secrets)
never appears in ``git diff HEAD``, so it is diffed against ``/dev/null`` and
appended before filtering. The index is never mutated.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from typing import List, Optional, Tuple

from .config import Config

TRUNCATION_NOTICE = "\n\n[diff truncated: exceeded max_diff_bytes]\n"


def _run_git(cwd: str, args: List[str], ok_codes: Tuple[int, ...] = (0,)) -> Optional[str]:
    """Run a git command, returning stdout or None on failure.

    ``ok_codes`` lists the return codes treated as success. ``git diff --no-index``
    exits 1 when the files differ (the normal case for a new file), so callers pass
    ``ok_codes=(0, 1)`` to accept that stdout.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in ok_codes:
        return None
    return proc.stdout


def is_git_repo(cwd: str) -> bool:
    out = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    return bool(out and out.strip() == "true")


def _raw_diff(cwd: str) -> str:
    """Return the raw unified diff vs HEAD, falling back to the index diff."""
    # Primary: staged + unstaged changes relative to the last commit.
    out = _run_git(cwd, ["diff", "HEAD"])
    if out is None:
        # Fresh repo (no HEAD yet) or other failure: fall back to unstaged diff.
        out = _run_git(cwd, ["diff"])
    return out or ""


def _untracked_paths(cwd: str) -> List[str]:
    """Return repo-relative paths of untracked, non-ignored files."""
    out = _run_git(cwd, ["ls-files", "--others", "--exclude-standard"])
    if not out:
        return []
    return [line for line in out.splitlines() if line]


def _looks_binary(abs_path: str) -> bool:
    """Cheap binary sniff: a NUL byte in the first chunk means don't diff it."""
    try:
        with open(abs_path, "rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return True
    return b"\x00" in chunk


def _normalize_new_file_header(diff_text: str, path: str) -> str:
    """Force a ``diff --git a/<path> b/<path>`` header so the glob filter works.

    ``git diff --no-index`` may emit ``a/dev/null`` (or an absolute path) in the
    header line; the rest of the pipeline keys off the ``b/`` path, so rewrite the
    first header line to the canonical form.
    """
    lines = diff_text.splitlines(keepends=True)
    if lines and lines[0].startswith("diff --git "):
        eol = "\n" if lines[0].endswith("\n") else ""
        lines[0] = "diff --git a/%s b/%s%s" % (path, path, eol)
    return "".join(lines)


def _untracked_diff(cwd: str, path: str, max_bytes: int) -> str:
    """Produce a new-file unified diff for one untracked path, or "" to skip.

    Skips files that look binary or exceed ``max_bytes``. Uses ``git diff
    --no-index -- /dev/null <path>`` (exits 1 on difference, which is expected).
    """
    abs_path = os.path.join(cwd, path)
    try:
        if os.path.getsize(abs_path) > max_bytes:
            return ""
    except OSError:
        return ""
    if _looks_binary(abs_path):
        return ""
    out = _run_git(cwd, ["diff", "--no-index", "--", "/dev/null", path], ok_codes=(0, 1))
    if not out:
        return ""
    if "Binary files" in out:
        return ""
    return _normalize_new_file_header(out, path)


def _untracked_section(cwd: str, cfg: Config) -> str:
    """Concatenate new-file diffs for untracked, included paths within budget."""
    parts: List[str] = []
    used = 0
    for path in _untracked_paths(cwd):
        if used >= cfg.max_diff_bytes:
            break
        if not _included(path, cfg):
            continue
        section = _untracked_diff(cwd, path, cfg.max_diff_bytes)
        if not section:
            continue
        parts.append(section)
        used += len(section.encode("utf-8", errors="replace"))
    return "".join(parts)


def _path_from_header(header_line: str) -> str:
    """Extract the changed path from a ``diff --git a/x b/y`` header line."""
    # Format: "diff --git a/<path> b/<path>". Prefer the b/ side (destination).
    parts = header_line.split(" b/", 1)
    if len(parts) == 2:
        return parts[1].strip()
    # Fallback: strip the a/ prefix from the remainder.
    tail = header_line[len("diff --git ") :].strip()
    if tail.startswith("a/"):
        tail = tail[2:]
    return tail


def _included(path: str, cfg: Config) -> bool:
    """A path is reviewed if it matches an include glob and no exclude glob."""
    if not any(fnmatch.fnmatch(path, pat) for pat in cfg.include):
        return False
    if any(fnmatch.fnmatch(path, pat) for pat in cfg.exclude):
        return False
    return True


def _filter_by_globs(raw: str, cfg: Config) -> str:
    """Keep only per-file sections whose path passes the include/exclude filter."""
    if not raw:
        return ""
    lines = raw.splitlines(keepends=True)
    kept: List[str] = []
    keep_current = False
    seen_header = False
    for line in lines:
        if line.startswith("diff --git "):
            seen_header = True
            path = _path_from_header(line.rstrip("\n"))
            keep_current = _included(path, cfg)
            if keep_current:
                kept.append(line)
            continue
        if not seen_header:
            # Content before any file header (unusual) — pass through.
            kept.append(line)
        elif keep_current:
            kept.append(line)
    return "".join(kept)


def compute(cwd: str, cfg: Config) -> str:
    """Return the filtered, size-bounded review diff, or "" when there's nothing.

    Returns "" when the directory is not a git repo or there are no reviewable
    changes after filtering.
    """
    if not cwd or not is_git_repo(cwd):
        return ""
    raw = _raw_diff(cwd)
    # Brand-new (untracked) files are invisible to ``git diff HEAD``; append them
    # so new secrets/files still get reviewed. Filtering + truncation follow.
    raw += _untracked_section(cwd, cfg)
    if not raw.strip():
        return ""
    filtered = _filter_by_globs(raw, cfg)
    if not filtered.strip():
        return ""
    if len(filtered.encode("utf-8", errors="replace")) > cfg.max_diff_bytes:
        # Truncate on a byte budget while keeping valid UTF-8.
        clipped = filtered.encode("utf-8", errors="replace")[: cfg.max_diff_bytes]
        filtered = clipped.decode("utf-8", errors="ignore") + TRUNCATION_NOTICE
    return filtered
