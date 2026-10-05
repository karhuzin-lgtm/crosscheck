"""Reviewer provider adapters.

A provider takes a prompt string and returns the reviewer model's raw text output.
Everything is done through local CLIs the user already has installed — crosscheck
ships no API keys and makes no direct network calls of its own.

Trust model (this file processes an UNTRUSTED git diff and UNTRUSTED config from
potentially-hostile cloned repos, embedded in the reviewer prompt — a malicious
diff can attempt to prompt-inject the reviewer CLI into reading env secrets or
local files):

- ``auto`` selects ONLY ``codex``, and only when it is available. ``codex`` runs
  with ``--sandbox read-only`` (an OS-level sandbox), so even if the diff hijacks
  the reviewer, the OS confines what it can touch. ``auto`` NEVER silently falls
  back to an unsandboxed CLI — if codex is absent, ``resolve()`` fails open with a
  message telling the user to install codex or explicitly opt into gemini/claude.
- ``gemini`` and ``claude`` have no OS sandbox in this harness, so they are usable
  ONLY when set EXPLICITLY as ``provider``. When a non-sandboxed reviewer is
  selected, ``Reviewer.sandboxed`` is False and gate.py prints a one-line warning
  that the reviewer runs without OS isolation.
- We still harden the SUBPROCESS ENVIRONMENT for every built-in CLI: they run with
  a SANITIZED env (only a fixed-safe PATH, HOME, and locale vars — every other env
  var, including credentials, is dropped) and with cwd set to a neutral temp dir,
  so the reviewer can't trivially read repo-relative files or inherit secrets.
- Binaries are resolved to an ABSOLUTE path against a fixed-safe PATH to prevent a
  malicious repo from shadowing ``codex`` via a writable/cwd PATH entry.

The exact CLI invocations are centralized in ``_ARGV`` below. They are the standard
non-interactive forms as of 2026 but may need adjustment for future CLI versions;
any provider can be overridden entirely via the ``command`` provider + config.
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import redact
from .config import Config

# Fixed, safe PATH used both to RESOLVE built-in reviewer binaries and as the
# PATH inside their sanitized environment. Deliberately excludes "." and any
# cwd-relative or user-writable entry so a cloned repo cannot shadow a binary.
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Hard per-stream byte cap on captured reviewer output. Enforced DURING capture
# (not just after) so a runaway/compromised CLI cannot balloon our memory even if
# it never exits before the timeout. Applied independently to stdout and stderr.
_MAX_CAPTURE_BYTES = 1_000_000

# Chunk size for draining reviewer stdout/stderr pipes.
_READ_CHUNK = 65536


class ProviderError(Exception):
    """Raised on any reviewer failure (missing CLI, timeout, nonzero exit).

    The orchestrator converts this into a fail-open (or, in strict mode, a block
    citing the error) — a reviewer problem must never hard-wedge the session.
    """


# Centralized argv templates. ``{model}`` slots are filled only when a model is
# configured. Prompt is always delivered on stdin. Adjust here if a CLI changes.
# argv[0] here is only a NAME; it is replaced with the resolved absolute path
# before the reviewer is built (see _build_cli / _resolve_binary).
_ARGV = {
    # OpenAI Codex CLI, non-interactive. --sandbox read-only blocks WRITES only
    # (the reviewer cannot mutate the repo); it does NOT restrict reads, so it is
    # not a secrets boundary. skip-git-repo-check lets it run in any dir.
    "codex": ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-"],
    # Google Gemini CLI, non-interactive. Prompt is read from stdin. Some versions
    # expect `-p -`; if yours does, override via the "command" provider.
    "gemini": ["gemini"],
    # Anthropic Claude CLI, print mode. Prompt on stdin.
    "claude": ["claude", "-p"],
    # Ollama, a LOCAL model server. `ollama run MODEL` reads the prompt on stdin
    # and prints the completion. It is a plain text model with no tools: it
    # cannot read or write files or run commands, so it needs no sandbox opt-in.
    # The model name is appended in _build_cli.
    "ollama": ["ollama", "run"],
}

# Model used for ollama when none is given (``--jury codex,ollama`` or
# ``ollama:<model>`` to pick another). A small, strong code model.
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"

# Reviewers that are plain models with NO tool access (no file reads, no shell).
_TOOLLESS = ("ollama",)

# Per-provider flag used to pin a specific model, when config.model is set.
_MODEL_FLAG = {
    "codex": "-m",
    "gemini": "-m",
    "claude": "--model",
}


@dataclass
class Reviewer:
    """A resolved reviewer: its display name and how to invoke it.

    ``sanitized`` controls subprocess isolation in ``run()``:
    - True (built-in CLIs): sanitized env + neutral temp cwd. These process the
      untrusted diff, so we deny them ambient secrets and repo-relative access.
    - False (the "command" provider): normal env + inherited (repo) cwd. That
      provider only runs after an explicit user opt-in
      (CROSSCHECK_ALLOW_COMMAND / CROSSCHECK_COMMAND), so it is user-trusted.

    ``sandboxed`` records whether the reviewer runs under an OS-level sandbox.
    ONLY ``codex`` (``--sandbox read-only``) is sandboxed. gemini/claude/command
    are not; the orchestrator warns when a non-sandboxed reviewer runs.
    """

    name: str
    argv: List[str]
    warn_same_vendor: bool = False
    sanitized: bool = True
    sandboxed: bool = False
    # False for plain text models (ollama) that cannot read files or run
    # commands at all — the read-access warning does not apply to them.
    tools: bool = True
    # Model pinned for this reviewer, if any (shown in its display name).
    model: Optional[str] = None
    # Extra env vars passed through the sanitized environment (e.g. OLLAMA_HOST).
    env_passthrough: Tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return "%s:%s" % (self.name, self.model) if self.model else self.name


def _resolve_binary(name: str) -> str:
    """Resolve ``name`` to an ABSOLUTE path via a fixed-safe PATH.

    Raises ProviderError (which fails open) if the binary is not found, does not
    resolve to an absolute path, or resolves inside the repo cwd (a repo trying
    to ship its own shadowing binary).
    """
    resolved = shutil.which(name, path=_SAFE_PATH)
    if not resolved:
        raise ProviderError("reviewer CLI '%s' not found on the safe PATH." % name)
    resolved = os.path.abspath(resolved)
    if not os.path.isabs(resolved):
        raise ProviderError("reviewer CLI '%s' did not resolve to an absolute path." % name)
    repo = os.path.abspath(os.getcwd())
    if _is_within(resolved, repo):
        raise ProviderError(
            "refusing reviewer CLI '%s': resolved path %s is inside the repo cwd "
            "(possible binary-shadowing attack)." % (name, resolved)
        )
    return resolved


def _is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        # Different drives / relative vs absolute — treat as "not within".
        return False


def _cli_available(name: str) -> bool:
    """True iff ``name`` resolves safely (same logic used to build the reviewer)."""
    try:
        _resolve_binary(name)
        return True
    except ProviderError:
        return False


def _unsandboxed_opt_in() -> bool:
    """True iff the operator explicitly allowed unsandboxed reviewers via env."""
    return os.environ.get("CROSSCHECK_ALLOW_UNSANDBOXED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def resolve(cfg: Config) -> Reviewer:
    """Select the reviewer according to config, raising ProviderError if none.

    ``auto`` selects ONLY the OS-sandboxed ``codex``. We do NOT auto-run an
    unsandboxed reviewer on an untrusted diff; if codex is unavailable, this
    fails open (via ProviderError) with guidance.

    ``gemini``/``claude`` have no OS sandbox in this harness, so they are gated
    behind the env opt-in ``CROSSCHECK_ALLOW_UNSANDBOXED=1`` — required to use
    them AT ALL, regardless of whether they were requested via env or a project
    file. Without the opt-in, a gemini/claude request from ANY source silently
    falls back to ``auto`` (the codex-only, OS-sandboxed path).
    """
    # A provider may carry its own model: "ollama:qwen2.5-coder:7b", "codex:gpt-5".
    # Split on the FIRST colon only (ollama model tags contain colons).
    spec = (cfg.provider or "auto").strip()
    name, sep, model = spec.partition(":")
    provider = name.strip().lower()
    if sep:
        model = model.strip()
        if not model or model.startswith("-") or any(c.isspace() for c in model):
            raise ProviderError("invalid model in reviewer spec %r." % spec)
        cfg = dataclasses.replace(cfg, model=model)

    # Unsandboxed reviewers require an explicit trusted (env) opt-in. Without it,
    # fall back to the sandboxed auto path rather than run them on an untrusted diff.
    if provider in ("gemini", "claude") and not _unsandboxed_opt_in():
        provider = "auto"

    if provider == "auto":
        if _cli_available("codex"):
            return _build_cli("codex", cfg, warn_same_vendor=False)
        raise ProviderError(
            "auto reviewer requires codex (the only OS-sandboxed reviewer). "
            "Install codex, or set provider=gemini|claude AND opt in with "
            "CROSSCHECK_ALLOW_UNSANDBOXED=1 (these run WITHOUT an OS sandbox — "
            "only use a reviewer you trust to ignore instructions embedded in "
            "the diff)."
        )

    if provider == "command":
        if not cfg.command:
            raise ProviderError(
                "provider 'command' requires a 'command' template in config "
                "(or CROSSCHECK_COMMAND)."
            )
        # Security: a command from a checked-in project file could execute
        # arbitrary shell in a cloned repo during the Stop hook. Only honor an
        # env-sourced command, or a project-file command with an explicit opt-in.
        allow_env = os.environ.get("CROSSCHECK_ALLOW_COMMAND", "").strip().lower()
        if not cfg.command_from_env and allow_env not in ("1", "true", "yes", "on"):
            raise ProviderError(
                "provider 'command' from a project file (.crosscheck.json) is "
                "disabled for security. Set CROSSCHECK_ALLOW_COMMAND=1 to honor it, "
                "or supply the command via the CROSSCHECK_COMMAND env var."
            )
        # Parse into an argv list and run without a shell (no shell interpretation).
        try:
            argv = shlex.split(cfg.command)
        except ValueError as exc:
            raise ProviderError("invalid 'command' template: %s" % exc)
        if not argv:
            raise ProviderError("'command' template is empty after parsing.")
        # User-trusted (explicit opt-in): keep normal env + repo cwd, so a custom
        # reviewer wrapper can read repo files / use the user's environment. Not
        # OS-sandboxed → sandboxed=False (gate.py warns).
        return Reviewer(name="command", argv=argv, sanitized=False, sandboxed=False)

    if provider in _ARGV:
        if not _cli_available(provider):
            raise ProviderError("configured provider '%s' CLI not found on PATH." % provider)
        return _build_cli(provider, cfg, warn_same_vendor=(provider == "claude"))

    raise ProviderError("unknown provider '%s'." % provider)


def _build_cli(name: str, cfg: Config, warn_same_vendor: bool) -> Reviewer:
    resolved = _resolve_binary(name)  # absolute path or ProviderError (fail-open)
    argv = list(_ARGV[name])
    argv[0] = resolved
    if name == "ollama":
        model = cfg.model or DEFAULT_OLLAMA_MODEL
        if model.startswith("-"):
            raise ProviderError("invalid ollama model %r." % model)
        return Reviewer(
            name=name,
            argv=argv + [model],
            sanitized=True,
            sandboxed=False,
            tools=False,
            model=model,
            env_passthrough=("OLLAMA_HOST",),
        )
    if cfg.model and name in _MODEL_FLAG:
        # Insert the model flag right after the base binary/subcommand tokens.
        flag = _MODEL_FLAG[name]
        argv = _inject_model(name, argv, flag, cfg.model)
    # Only codex runs under an OS sandbox (--sandbox read-only).
    return Reviewer(
        name=name,
        argv=argv,
        warn_same_vendor=warn_same_vendor,
        sanitized=True,
        sandboxed=(name == "codex"),
        model=cfg.model,
    )


def _inject_model(name: str, argv: List[str], flag: str, model: str) -> List[str]:
    """Insert ``flag model`` before the trailing stdin marker, if any."""
    # Keep a trailing "-" (stdin marker) last so the flag doesn't consume it.
    if argv and argv[-1] == "-":
        return argv[:-1] + [flag, model, "-"]
    return argv + [flag, model]


def _sanitized_env(extra: Tuple[str, ...] = ()) -> Dict[str, str]:
    """Minimal env for built-in reviewer subprocesses.

    Only a fixed-safe PATH plus HOME and locale vars survive; every other var
    (credentials, tokens, provider keys) is dropped so they are not handed to the
    reviewer through the environment. This is NOT a privacy boundary: HOME is kept
    so file-based CLI auth (e.g. codex's ~/.codex) works, which also means the
    reviewer can still read on-disk secrets by absolute path. Redaction (in and
    out) is the mitigation, not this env. See the README "Security model".
    """
    src = os.environ
    env: Dict[str, str] = {"PATH": _SAFE_PATH}
    for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE") + tuple(extra):
        val = src.get(key)
        if val:
            env[key] = val
    return env


def _feed_stdin(stream, data: bytes) -> None:
    """Write ``data`` to the child's stdin in its own thread, then close it.

    Runs concurrently so a large prompt can't deadlock against a child that
    fills its stdout/stderr pipes before reading stdin. All errors (e.g. the
    child died and the pipe broke) are swallowed — output/exit handling decides
    the outcome.
    """
    try:
        stream.write(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _drain_stream(stream, cap: int, sink: Dict[str, object]) -> None:
    """Read ``stream`` to EOF, accumulating at most ``cap`` bytes into ``sink``.

    Once the cap is reached we KEEP reading (so the child never blocks on a full
    pipe) but discard the overflow and set ``sink['truncated']``. This bounds our
    memory regardless of how much a runaway CLI emits.
    """
    buf = sink["buf"]  # type: ignore[assignment]
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            size = sink["size"]  # type: ignore[assignment]
            room = cap - size
            if room > 0:
                if len(chunk) <= room:
                    buf.append(chunk)  # type: ignore[union-attr]
                    sink["size"] = size + len(chunk)
                else:
                    buf.append(chunk[:room])  # type: ignore[union-attr]
                    sink["size"] = cap
                    sink["truncated"] = True
            else:
                sink["truncated"] = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _terminate_group(proc: "subprocess.Popen") -> None:
    """Best-effort kill of the child's whole process group so a timeout leaves
    no orphaned grandchildren. POSIX: SIGTERM the group, wait briefly, then
    SIGKILL. Windows (no killpg/getpgid): fall back to proc.kill()."""
    if os.name == "nt":
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def run(reviewer: Reviewer, prompt: str, timeout_sec: int) -> str:
    """Invoke the reviewer with ``prompt`` on stdin; return raw stdout text.

    Uses Popen with a new session/process group and concurrently drains stdout
    and stderr under a hard per-stream byte cap, so memory stays bounded even for
    a compromised/runaway CLI. A wall-clock deadline (proc.wait timeout) enforces
    ``timeout_sec``; on expiry the whole process group is killed (no orphans).

    Raises ProviderError on missing binary, timeout, nonzero exit, or any spawn
    error — must never propagate an exception that breaks the fail-open contract.
    """
    # A malicious .crosscheck.json (e.g. model=" \x00 ") could inject a NUL byte,
    # which subprocess rejects with ValueError. Validate up front.
    for elem in reviewer.argv:
        if not isinstance(elem, str) or "\x00" in elem:
            raise ProviderError("reviewer argv contains an invalid element.")

    # Working directory for the child. For sanitized (built-in) reviewers we use a
    # UNIQUE PRIVATE dir (mkdtemp → mode 0700, owned by us) instead of the shared,
    # world-writable temp root: a CLI launched in the shared /tmp could auto-pick
    # up attacker-planted config/instructions (AGENTS.md, etc.) sitting there — an
    # injection channel. The dir must exist for the whole child lifetime and be
    # removed on EVERY exit path, so it is created here and torn down in ``finally``.
    rev_cwd: Optional[str] = None
    if reviewer.sanitized:
        env: Optional[Dict[str, str]] = _sanitized_env(reviewer.env_passthrough)
        rev_cwd = tempfile.mkdtemp(prefix="crosscheck-rev-")  # 0700, unique, ours
        cwd: Optional[str] = rev_cwd
    else:
        # "command" provider: user-trusted, keep normal env + inherited (repo) cwd.
        env = None
        cwd = None

    try:
        try:
            proc = subprocess.Popen(
                reviewer.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                # New session/process group so we can kill the whole tree on timeout.
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            raise ProviderError("reviewer CLI not found: %s" % exc) from exc
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
            raise ProviderError("reviewer '%s' failed to run: %s" % (reviewer.name, exc)) from exc

        data = prompt.encode("utf-8", errors="replace")
        out_sink: Dict[str, object] = {"buf": [], "size": 0, "truncated": False}
        err_sink: Dict[str, object] = {"buf": [], "size": 0, "truncated": False}

        threads = [
            threading.Thread(target=_feed_stdin, args=(proc.stdin, data), daemon=True),
            threading.Thread(
                target=_drain_stream, args=(proc.stdout, _MAX_CAPTURE_BYTES, out_sink), daemon=True
            ),
            threading.Thread(
                target=_drain_stream, args=(proc.stderr, _MAX_CAPTURE_BYTES, err_sink), daemon=True
            ),
        ]
        for t in threads:
            t.start()

        timed_out = False
        try:
            proc.wait(timeout=max(int(timeout_sec), 0))
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(proc)
        except (OSError, ValueError) as exc:
            _terminate_group(proc)
            # ``exc`` text is process-derived; mask any secret shape before it becomes
            # a ProviderError message that gate.py may surface to the user.
            safe_exc, _ = redact.redact(str(exc))
            raise ProviderError(
                "reviewer '%s' failed while running: %s" % (reviewer.name, safe_exc)
            ) from exc

        # The child's exit (or kill) closes its pipes, so the drain/feed threads reach
        # EOF and finish. Join with a bounded timeout as a deadlock safety net.
        for t in threads:
            t.join(timeout=5)

        if timed_out:
            raise ProviderError(
                "reviewer '%s' timed out after %ss" % (reviewer.name, timeout_sec)
            )

        # A stdout that hit the capture cap is UNUSABLE: truncation can leave a
        # syntactically-valid JSON prefix (e.g. an early ``{"verdict":"pass"...}``)
        # while the real findings are lost, which the gate would wrongly accept as a
        # clean pass. Refuse it BEFORE decoding/returning so the configured
        # fail-open/strict policy decides the outcome instead of trusting a partial
        # response. (stderr truncation is cosmetic — it only feeds error snippets.)
        if out_sink.get("truncated"):
            raise ProviderError(
                "reviewer '%s' output exceeded the capture limit; truncated result "
                "is unusable" % reviewer.name
            )

        stdout = b"".join(out_sink["buf"]).decode("utf-8", errors="replace")  # type: ignore[arg-type]
        stderr = b"".join(err_sink["buf"]).decode("utf-8", errors="replace")  # type: ignore[arg-type]

        returncode = proc.returncode
        if returncode != 0:
            detail = (stderr or stdout or "").strip()
            # Keep the message short — full CLI noise isn't useful to the user.
            snippet = detail.splitlines()[-1] if detail else "no output"
            # A prompt-injected reviewer could read a secret then exit nonzero,
            # planting it in stderr/stdout. Redact the snippet BEFORE it enters the
            # ProviderError (which gate.py prints via _fail / a block reason).
            safe_snippet, _ = redact.redact(snippet)
            raise ProviderError(
                "reviewer '%s' exited %s: %s" % (reviewer.name, returncode, safe_snippet)
            )

        if not stdout.strip():
            raise ProviderError("reviewer '%s' produced empty output" % reviewer.name)
        return stdout
    finally:
        # Always remove the private reviewer cwd (if we created one), on every
        # return/raise/timeout path. ignore_errors so cleanup never masks the result.
        if rev_cwd is not None:
            shutil.rmtree(rev_cwd, ignore_errors=True)
