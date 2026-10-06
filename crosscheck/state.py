"""Private on-disk state helpers (per-user, symlink-safe).

crosscheck keeps two kinds of local state: the per-session loop-guard counters
(gate.py) and the counts-only "issues caught" stats (stats.py). Both live in
directories created 0700 and verified to be real, owned, non-symlink dirs.
"""

from __future__ import annotations

import os
import stat


def _verify_private_dir(path: str) -> bool:
    """True iff ``path`` is a real directory, owned by us, and not a symlink.

    Uses ``lstat`` so a symlink is caught (its lstat is not a directory). The
    uid check is skipped on Windows, which lacks POSIX ownership semantics.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    if os.name != "nt" and st.st_uid != os.getuid():
        return False
    return True


def _ensure_private_dir(path: str) -> bool:
    """Create ``path`` (mode 0700) if needed and verify it is ours + a real dir.

    Returns False on any problem so the caller can degrade gracefully. Perms are
    tightened explicitly after creation (umask can otherwise loosen makedirs),
    and only after the symlink/ownership check has passed.
    """
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return False
    if not _verify_private_dir(path):
        return False
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            return False
    return True
