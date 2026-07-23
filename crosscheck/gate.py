"""Orchestrator: the Stop-hook entry logic.

Reads the hook payload from stdin, computes and redacts the working-tree diff,
asks an independent reviewer model to grade it, and either blocks the turn (so
Claude keeps fixing) or allows it to end.

Guarantees:
- Exit code is ALWAYS 0. Blocking is expressed via a ``{"decision":"block"}``
  JSON object on stdout, per the Claude Code Stop-hook contract.
- Fail-open by default: any unexpected error allows the turn to end with a
  visible stderr warning, so crosscheck can never wedge the user's session.
- ``main()`` NEVER raises. Payload parsing/validation, config loading and the
  cwd/session derivation all live inside the top-level fail-open guard.
- A per-session round counter caps the block/fix loop at ``max_rounds``. Its
  state lives in a per-user private directory with symlink-safe, atomic file
  I/O; if that state can't be established safely it degrades gracefully (the
  counter is skipped — Claude Code still caps consecutive blocks at 8).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import config, diff, providers, redact, review
from .providers import ProviderError, Reviewer
from .review import Finding


def _state_base() -> str:
    """Return the per-user private base dir that holds crosscheck session state.

    Prefers ``$XDG_RUNTIME_DIR`` (already a user-private, 0700 tmpfs on most
    Linux). Otherwise a user-owned dir under the system temp dir, namespaced by
    uid on POSIX so it can't collide with another user's predictable /tmp path.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "crosscheck")
    if os.name == "nt":
        return os.path.join(tempfile.gettempdir(), "crosscheck")
    return os.path.join(tempfile.gettempdir(), "crosscheck-%d" % os.getuid())


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


def _state_dir(session_id: str) -> Optional[str]:
    """Return a private per-session state dir, or None if it can't be made safe.

    The session subdir name is a full SHA-256 hexdigest of ``session_id`` (not a
    lossy character-sanitized form) so distinct sessions never collide.
    """
    base = _state_base()
    if not _ensure_private_dir(base):
        return None
    name = hashlib.sha256((session_id or "default").encode("utf-8")).hexdigest()
    path = os.path.join(base, name)
    if not _ensure_private_dir(path):
        return None
    return path


def _read_int(path: Optional[str], default: int = 0) -> int:
    """Symlink-safe read of a small int counter file. Degrades to ``default``.

    Opens with O_NOFOLLOW and verifies via fstat that the target is a regular
    file owned by us before reading. Any problem returns ``default`` (never
    raises), so a tampered/absent state file just resets the counter.
    """
    if not path:
        return default
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return default
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return default
        if os.name != "nt" and st.st_uid != os.getuid():
            return default
        raw = os.read(fd, 64)
    except OSError:
        return default
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        return int(raw.decode("utf-8", errors="replace").strip() or default)
    except ValueError:
        return default


def _write_int(path: Optional[str], value: int) -> None:
    """Symlink-safe atomic-ish write of an int counter file. Never raises.

    Opens the target with O_CREAT|O_TRUNC|O_NOFOLLOW at mode 0600, so a symlink
    planted at ``path`` is refused rather than followed. On any OSError the loop
    guard just degrades (Claude Code still caps consecutive blocks at 8).
    """
    if not path:
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return
    try:
        os.write(fd, str(int(value)).encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _emit_block(reason: str) -> None:
    """Print the block decision to stdout (the only thing Claude Code reads)."""
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _note(text: str) -> None:
    """Human-facing status line on stderr (visible in the transcript, not to Claude)."""
    sys.stderr.write("crosscheck: %s\n" % text)


def _format_block_reason(
    reviewer: Reviewer, blocking: List[Finding], round_no: int, cfg: config.Config
) -> str:
    header = (
        "crosscheck: an independent reviewer (%s) found %d issue(s) that should "
        "be addressed before finishing:" % (reviewer.display, len(blocking))
    )
    lines: List[str] = [header, ""]
    for f in blocking:
        loc = f.file + (":%d" % f.line if f.line else "")
        lines.append("[%s] %s — %s" % (f.severity.upper(), loc, f.summary))
        if f.detail:
            lines.append("  " + f.detail)
    lines.append("")
    lines.append(
        "Fix the issues above, then finish. (crosscheck round %d/%d)"
        % (round_no, cfg.max_rounds)
    )
    if reviewer.warn_same_vendor:
        lines.append(
            "Note: the reviewer shares a vendor with the author (Claude reviewing "
            "Claude). Install `codex` or `gemini` for a true cross-model second "
            "opinion."
        )
    return "\n".join(lines)


def _fail(cfg: config.Config, message: str) -> int:
    """Handle an unrecoverable review problem per fail-open/strict policy.

    ``message`` may be subprocess-derived (a ProviderError carries the reviewer's
    last stderr/stdout line on a nonzero exit). A prompt-injected reviewer could
    read a local secret and die nonzero, planting it there — so the message is run
    through ``redact.redact`` before it is written to stderr or embedded in a
    block reason. This is the single chokepoint every reviewer failure flows
    through, so every user-facing path for that text is covered here.
    """
    safe_message, _ = redact.redact(message)
    if cfg.fail_open:
        _note("%s — failing open, turn allowed." % safe_message)
        return 0
    reason = (
        "crosscheck (strict mode): the review could not be completed and "
        "fail_open is disabled, so this turn is blocked.\n"
        "Reason: %s\n"
        "Resolve the reviewer setup, or set fail_open=true / disable crosscheck." % safe_message
    )
    _emit_block(reason)
    return 0


def _run_gate(data: Dict[str, Any], cwd: str, session_id: str, cfg: config.Config) -> int:
    state = _state_dir(session_id)
    rounds_path = os.path.join(state, "rounds") if state else None
    tally_path = os.path.join(state, "tally") if state else None

    rounds = _read_int(rounds_path)

    # Loop guard: after max_rounds blocks, stop nagging and let the turn end.
    if rounds >= cfg.max_rounds:
        _write_int(rounds_path, 0)
        _note(
            "reached max_rounds (%d); passing with unresolved findings left to the user."
            % cfg.max_rounds
        )
        return 0

    # 1. What changed?
    review_diff = diff.compute(cwd, cfg)
    if not review_diff:
        _write_int(rounds_path, 0)  # nothing to review -> fresh slate
        return 0

    # 2. Never let secrets leave the machine.
    redacted, n_redacted = redact.redact(review_diff)
    if n_redacted:
        _note("redacted %d secret(s) from the diff before review." % n_redacted)

    # 3. Pick the independent reviewer (may fail-open on missing CLI).
    try:
        reviewer = providers.resolve(cfg)
    except ProviderError as exc:
        return _fail(cfg, str(exc))

    # Be honest about the reviewer's isolation. codex --sandbox read-only blocks
    # WRITES only; gemini/claude/command (reachable only behind an explicit env
    # opt-in) have not even that. NO reviewer is isolated from filesystem READS,
    # so a prompt-injecting diff could induce any of them to read local files;
    # redaction (in and out) is the mitigation, not the reviewer sandbox.
    if not reviewer.sandboxed:
        _note(
            "reviewer '%s' has no write-sandbox (codex uses --sandbox read-only, "
            "which blocks writes). No reviewer is isolated from filesystem reads: "
            "a prompt-injecting diff could induce it to read local files. "
            "Redaction of the diff and the output is the mitigation." % reviewer.display
        )

    # 4. Ask it to grade the diff.
    prompt = review.build_prompt(redacted, cfg)
    try:
        raw_output = providers.run(reviewer, prompt, cfg.timeout_sec)
    except ProviderError as exc:
        return _fail(cfg, str(exc))

    # Defense-in-depth: the reviewer is not filesystem-sandboxed, so a prompt
    # injection could induce it to surface a local secret in its findings. Redact
    # its OUTPUT with the same masks we applied to the input, BEFORE it reaches
    # the parser / block reason / stderr.
    raw_output, n_out_redacted = redact.redact(raw_output)
    if n_out_redacted:
        _note(
            "redacted %d secret(s) from the reviewer's OUTPUT before use."
            % n_out_redacted
        )

    result = review.parse(raw_output)
    if not result.parsed:
        return _fail(cfg, "reviewer output could not be parsed as findings JSON")

    blocking = result.at_or_above(cfg.threshold)

    if blocking:
        round_no = rounds + 1
        _write_int(rounds_path, round_no)
        _write_int(tally_path, _read_int(tally_path) + len(blocking))
        _emit_block(_format_block_reason(reviewer, blocking, round_no, cfg))
        _note(
            "blocked turn — %d issue(s) at/above '%s' (round %d/%d)."
            % (len(blocking), cfg.threshold, round_no, cfg.max_rounds)
        )
        return 0

    # Passed the threshold. Reset the loop and report the tally.
    _write_int(rounds_path, 0)
    total = len(result.findings)
    session_caught = _read_int(tally_path)
    if total:
        _note(
            "passed — %d finding(s) below the '%s' threshold, none blocking."
            % (total, cfg.threshold)
        )
    else:
        _note("passed — reviewer (%s) found no issues." % reviewer.display)
    if session_caught:
        _note("%d issue(s) caught so far this session." % session_caught)
    return 0


def _coerce_str(value: Any, fallback: str) -> str:
    """Return ``value`` if it's a non-empty str, else ``fallback``."""
    return value if isinstance(value, str) and value else fallback


def main() -> int:
    """Stop-hook entry. ALWAYS returns 0 and NEVER raises.

    ALL logic after reading stdin — payload parse/validation, config load, and
    cwd/session derivation — runs inside the fail-open guard, so a hostile or
    malformed payload (``[]``, ``"str"``, ``123``, ``{"cwd":1}``, bad JSON) can
    never crash the hook and break the "exit is always 0" contract.
    """
    raw = sys.stdin.read()
    cfg: Optional[config.Config] = None
    try:
        try:
            data: Any = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            # Valid JSON that isn't an object (list/str/number) carries no usable
            # fields — treat as an empty payload.
            data = {}

        cwd = _coerce_str(data.get("cwd"), os.getcwd())
        session_id = _coerce_str(data.get("session_id"), "default")

        cfg = config.load(cwd)  # coerces cwd defensively; never raises
        if not cfg.enabled:
            return 0
        return _run_gate(data, cwd, session_id, cfg)
    except Exception as exc:  # noqa: BLE001 - top-level safety net, never wedge the session
        if cfg is not None:
            return _fail(cfg, "unexpected error: %s" % exc)
        # Failure before config was loaded: fall back to fail-open (allow).
        # Redact defensively — the message is surfaced to the user.
        safe_exc, _ = redact.redact("unexpected error before config load (%s); allowing turn." % exc)
        _note(safe_exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
