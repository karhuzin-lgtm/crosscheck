"""Configuration loading and resolution.

Resolution order (highest priority first):

    1. Project file  ./.crosscheck.json  (relative to the hook's ``cwd``)
    2. Environment variables (CROSSCHECK_*)
    3. Built-in defaults

Only standard library is used. Unknown keys in the project file are ignored so
that forward-compatible configs don't crash older installs.

Trust boundary (UNTRUSTED project file, TRUSTED env/defaults)
-------------------------------------------------------------
``.crosscheck.json`` can arrive checked into a hostile cloned repo, so the
project file may only ever move the gate in the STRENGTHENING direction relative
to the trusted (env/default) baseline — it can never weaken it:

- ``enabled``: the project file may set it TRUE (turn the gate on); a false value
  is IGNORED. Disabling is possible only via env/defaults.
- ``threshold``: the project file may LOWER it (a lower floor catches more:
  nit < warn < blocker); it may NOT raise it above the trusted baseline.
- ``max_rounds``: the project file may RAISE it; it may NOT lower it below the
  trusted baseline.
- ``timeout_sec``: the project file may RAISE it (more time = the reviewer is
  less likely to spuriously time out and fail open); it may NOT lower it below
  the trusted baseline (a hostile ``timeout_sec=1`` that forces a fail-open
  bypass is IGNORED). Still hard-clamped to the upper maximum.
- ``max_diff_bytes``: the project file may RAISE it (review MORE of the diff);
  it may NOT lower it below the trusted baseline (a hostile
  ``max_diff_bytes=1000`` that truncates most of the diff away is IGNORED).
  Still hard-clamped to the upper maximum.
- ``fail_open``: the project file may only (re)affirm ``true``; a false value is
  IGNORED (strict mode can only come from env/defaults).
- ``provider``: the project file may only select ``auto`` or ``codex`` (the
  OS-sandboxed path). Any other value (gemini/claude/command/unknown) is IGNORED
  and the trusted baseline provider stands. ``gemini``/``claude`` additionally
  require the env opt-in ``CROSSCHECK_ALLOW_UNSANDBOXED=1`` at RESOLVE time
  regardless of source (see providers.resolve).
- ``model``: TRUSTED, env-only. A ``model`` key in the project file is IGNORED
  ENTIRELY — an untrusted repo must not be able to point the reviewer at a
  nonexistent/wrong model (which would make the reviewer CLI error and fail
  open). The model comes only from ``CROSSCHECK_MODEL`` / the CLI default.
- ``command``: TRUSTED, env-only. A ``command`` key in the project file is
  IGNORED ENTIRELY — an untrusted repo must never supply the reviewer command
  string (nor clear the env-sourced-command flag). The command comes only from
  ``CROSSCHECK_COMMAND``. (The ``command`` provider is already un-selectable
  from a project file; the command string is refused here too.)
- ``include``/``exclude``: IGNORED from the project file entirely, so a hostile
  repo cannot shrink review coverage (e.g. ``exclude=["*"]`` or an empty
  include). They take effect only from env/defaults — put vendored-dir excludes
  in ``CROSSCHECK_EXCLUDE`` / global config, not the repo file.

The file is also size-capped before it is read (DoS guard). The hard upper
clamps on ``timeout_sec``/``max_diff_bytes`` still apply after resolution.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

# Severity threshold values, lowest to highest.
VALID_THRESHOLDS = ("nit", "warn", "blocker")

# Rank map: lower rank == stricter (catches more). nit=0 < warn=1 < blocker=2.
_THRESHOLD_RANK = {name: idx for idx, name in enumerate(VALID_THRESHOLDS)}

# Hard cap on the size of the untrusted .crosscheck.json before we read/parse it.
# A larger file is treated as invalid config (skipped) rather than loaded into
# memory — a hostile repo must not be able to feed us an arbitrarily huge file.
_MAX_CONFIG_BYTES = 64_000

# Providers the UNTRUSTED project file is allowed to request. Both resolve to an
# OS-sandboxed reviewer (auto -> codex; codex directly). gemini/claude/command
# from a project file are ignored (they require a trusted env opt-in).
_PROJECT_ALLOWED_PROVIDERS = ("auto", "codex")

# Default file/dir patterns to exclude from review. These are matched against the
# changed file path with ``fnmatch`` (see diff.py). Wildcards are shell-style.
DEFAULT_EXCLUDE: List[str] = [
    # Deep forms (dir nested anywhere) plus root-level forms, because fnmatch
    # patterns starting with "*/" do NOT match repo-root paths like "node_modules/x".
    "*/node_modules/*",
    "node_modules/*",
    "*/dist/*",
    "dist/*",
    "*/build/*",
    "build/*",
    "*/.venv/*",
    ".venv/*",
    "*/vendor/*",
    "vendor/*",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "*-lock.json",
    "*.lockb",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.svg",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.gz",
    "*.env",
    "*.env.*",
    "*.snap",
]

DEFAULT_INCLUDE: List[str] = ["*"]


@dataclass
class Config:
    """Resolved crosscheck configuration."""

    enabled: bool = True
    provider: str = "auto"
    # TRUSTED, env-only (CROSSCHECK_MODEL) — never accepted from a project file.
    model: Optional[str] = None
    threshold: str = "warn"
    fail_open: bool = True
    max_rounds: int = 2
    timeout_sec: int = 120
    max_diff_bytes: int = 200_000
    # Command template for the "command" provider (prompt piped to stdin). Parsed
    # with shlex and run without a shell. TRUSTED, env-only: it may arrive ONLY
    # via CROSSCHECK_COMMAND (which sets ``command_from_env``); a ``command`` key
    # in a checked-in project file is ignored entirely — see _apply_file().
    command: Optional[str] = None
    command_from_env: bool = False
    # True when ``provider`` was pinned via CROSSCHECK_PROVIDER. When set, the
    # untrusted project file may NOT override provider (see _apply_file).
    provider_from_env: bool = False
    include: List[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))


def _to_bool(value: Any, default: bool) -> bool:
    """Parse a permissive boolean from config/env values."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_threshold(value: Any, default: str) -> str:
    text = str(value).strip().lower() if value is not None else default
    return text if text in VALID_THRESHOLDS else default


def _threshold_rank(name: str) -> int:
    return _THRESHOLD_RANK.get(name, _THRESHOLD_RANK["warn"])


def _split_globs(raw: str) -> List[str]:
    """Split a comma-separated glob list from an env var into a clean list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _apply_env(cfg: Config) -> None:
    """Overlay environment variables onto ``cfg`` in place (TRUSTED source)."""
    env = os.environ
    if "CROSSCHECK_ENABLED" in env:
        cfg.enabled = _to_bool(env["CROSSCHECK_ENABLED"], cfg.enabled)
    if env.get("CROSSCHECK_PROVIDER"):
        cfg.provider = env["CROSSCHECK_PROVIDER"].strip()
        # Pinned by the operator's env → the untrusted project file can't change it.
        cfg.provider_from_env = True
    if env.get("CROSSCHECK_MODEL"):
        cfg.model = env["CROSSCHECK_MODEL"].strip()
    if env.get("CROSSCHECK_THRESHOLD"):
        cfg.threshold = _normalize_threshold(env["CROSSCHECK_THRESHOLD"], cfg.threshold)
    if "CROSSCHECK_FAIL_OPEN" in env:
        cfg.fail_open = _to_bool(env["CROSSCHECK_FAIL_OPEN"], cfg.fail_open)
    if env.get("CROSSCHECK_MAX_ROUNDS"):
        cfg.max_rounds = _to_int(env["CROSSCHECK_MAX_ROUNDS"], cfg.max_rounds)
    if env.get("CROSSCHECK_TIMEOUT"):
        cfg.timeout_sec = _to_int(env["CROSSCHECK_TIMEOUT"], cfg.timeout_sec)
    if env.get("CROSSCHECK_MAX_DIFF_BYTES"):
        cfg.max_diff_bytes = _to_int(env["CROSSCHECK_MAX_DIFF_BYTES"], cfg.max_diff_bytes)
    if env.get("CROSSCHECK_COMMAND"):
        cfg.command = env["CROSSCHECK_COMMAND"]
        cfg.command_from_env = True
    # include/exclude are TRUSTED-only (never accepted from the project file).
    # CROSSCHECK_INCLUDE replaces the include list; CROSSCHECK_EXCLUDE extends the
    # sane defaults (so vendored-dir excludes add to, not replace, the baseline).
    if env.get("CROSSCHECK_INCLUDE"):
        globs = _split_globs(env["CROSSCHECK_INCLUDE"])
        if globs:
            cfg.include = globs
    if env.get("CROSSCHECK_EXCLUDE"):
        globs = _split_globs(env["CROSSCHECK_EXCLUDE"])
        if globs:
            cfg.exclude = list(DEFAULT_EXCLUDE) + globs


def _apply_file(cfg: Config, data: Dict[str, Any]) -> None:
    """Overlay a parsed ``.crosscheck.json`` (UNTRUSTED) onto ``cfg`` in place.

    The project file may only STRENGTHEN the gate relative to the trusted
    (env/default) baseline already in ``cfg`` — see the module docstring.
    """
    if not isinstance(data, dict):
        return
    # Capture the trusted baseline for the strengthen-only fields before we mutate.
    base_threshold = cfg.threshold
    base_max_rounds = cfg.max_rounds
    base_timeout = cfg.timeout_sec
    base_max_diff = cfg.max_diff_bytes

    known = {f.name for f in fields(Config)}
    for key, value in data.items():
        if key not in known or value is None:
            continue
        if key == "enabled":
            # UNTRUSTED: the project file may only ENABLE the gate, never disable
            # it. A false/ambiguous value is IGNORED (baseline stands).
            if _to_bool(value, False):
                cfg.enabled = True
        elif key == "fail_open":
            # UNTRUSTED: the project file may NEVER change fail_open, in EITHER
            # direction. A hostile repo must not weaken strict mode, and must not
            # flip fail_open back to true to defeat an operator's
            # CROSSCHECK_FAIL_OPEN=false (that would turn reviewer errors/timeouts
            # into a free pass). fail_open comes only from env/defaults.
            continue
        elif key == "threshold":
            # UNTRUSTED: may only LOWER the threshold (stricter), never raise it
            # above the trusted baseline.
            candidate = _normalize_threshold(value, base_threshold)
            if _threshold_rank(candidate) <= _threshold_rank(base_threshold):
                cfg.threshold = candidate
        elif key == "max_rounds":
            # UNTRUSTED: may only RAISE max_rounds, never lower it below baseline.
            candidate = _to_int(value, base_max_rounds)
            if candidate >= base_max_rounds:
                cfg.max_rounds = candidate
        elif key == "timeout_sec":
            # UNTRUSTED: may only RAISE the reviewer time budget, never lower it
            # below the trusted baseline. A hostile timeout_sec=1 forces the
            # reviewer to time out -> fail-open bypass, so it is IGNORED.
            candidate = _to_int(value, base_timeout)
            if candidate >= base_timeout:
                cfg.timeout_sec = candidate
        elif key == "max_diff_bytes":
            # UNTRUSTED: may only RAISE the diff budget (review MORE), never lower
            # it below baseline. A hostile max_diff_bytes=1000 truncates most of
            # the diff away (hiding issues), so it is IGNORED.
            candidate = _to_int(value, base_max_diff)
            if candidate >= base_max_diff:
                cfg.max_diff_bytes = candidate
        elif key == "provider":
            # UNTRUSTED: never override an env-pinned provider; and may only
            # select the OS-sandboxed auto/codex path. Any other value (gemini/
            # claude/command/unknown) is IGNORED (not an error, not fail-open).
            if not cfg.provider_from_env:
                candidate = str(value).strip().lower()
                if candidate in _PROJECT_ALLOWED_PROVIDERS:
                    cfg.provider = candidate
        # NOTE: model, command and include/exclude from the project file are
        # intentionally IGNORED. model/command are TRUSTED env-only fields (a
        # hostile repo must not steer the reviewer model or inject a command);
        # include/exclude must not be shrinkable by a hostile repo.


def _read_config_file(path: str) -> Optional[Dict[str, Any]]:
    """Read+parse ``.crosscheck.json`` under a hard size cap.

    Returns the parsed dict, or None when the file is absent, too large,
    unreadable, malformed, or not a JSON object. Never raises; never loads more
    than ``_MAX_CONFIG_BYTES`` into memory.
    """
    try:
        if os.path.getsize(path) > _MAX_CONFIG_BYTES:
            return None
    except OSError:
        return None
    try:
        with open(path, "rb") as fh:
            # Bound the read even if the file grew after getsize (TOCTOU race):
            # read at most one byte past the cap to detect oversize.
            raw = fh.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            return None
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def load(cwd: str) -> Config:
    """Load configuration for a given working directory.

    Never raises: a malformed/oversized/unreadable project file is ignored
    (defaults/env still apply). ``cwd`` is coerced to a string defensively so a
    non-str payload value can never blow up path joining.
    """
    cfg = Config()
    _apply_env(cfg)

    safe_cwd = cwd if isinstance(cwd, str) and cwd else "."
    path = os.path.join(safe_cwd, ".crosscheck.json")
    data = _read_config_file(path)
    if data is not None:
        _apply_file(cfg, data)

    # Clamp to sane bounds. Upper maxima are HARD safety limits (DoS/cost guard
    # against a hostile checked-in .crosscheck.json) and must not be raisable by
    # the project file.
    cfg.max_rounds = min(max(cfg.max_rounds, 1), 10)
    cfg.timeout_sec = min(max(cfg.timeout_sec, 1), 600)
    cfg.max_diff_bytes = min(max(cfg.max_diff_bytes, 1000), 2_000_000)
    return cfg
