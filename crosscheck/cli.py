"""The standalone ``crosscheck`` command.

Works with any AI coding tool (Cursor, Copilot, Aider, Codex, Claude Code...) or
plain hand-written code — anything that ends up as a git diff:

    crosscheck                      review uncommitted changes (vs HEAD)
    crosscheck --staged             review what's staged (pre-commit view)
    crosscheck --base main          review this branch vs main (PR view)
    git diff | crosscheck -         review any diff from stdin
    crosscheck --jury codex,gemini  several independent models; show agreement
    crosscheck stats                what crosscheck has caught for you
    crosscheck doctor               which reviewers are installed and how they run
    crosscheck install-hook         run crosscheck on every `git commit`
    crosscheck demo                 offline walkthrough, no models or keys needed

Exit codes: 0 = pass (or a reviewer problem while fail-open), 1 = blocking
findings, 2 = usage error or a reviewer problem with --strict.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat as stat_mod
import subprocess
import sys
import threading
import time
from typing import List, Optional

from . import __version__, config, diff, engine, providers, stats, ui
from .providers import ProviderError
from .review import Finding

COMMANDS = ("review", "stats", "doctor", "install-hook", "demo")
_HOOK_MARKER = "# crosscheck pre-commit hook"
_MAX_STDIN_BYTES = 20_000_000


# --------------------------------------------------------------------------- review


class _Spinner:
    """A small 'reviewing…' indicator on stderr, only when stderr is a TTY."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, p: ui.Painter, text: str, enabled: bool) -> None:
        self.p, self.text, self.enabled = p, text, enabled
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start = time.time()

    def __enter__(self) -> "_Spinner":
        if self.enabled:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.08):
            frame = self.FRAMES[i % len(self.FRAMES)]
            secs = int(time.time() - self._start)
            sys.stderr.write("\r%s %s %s" % (self.p(frame, "accent"), self.text, self.p("%ds" % secs, "dim")))
            sys.stderr.flush()
            i += 1

    def __exit__(self, *exc: object) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join()
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def _git_toplevel(cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else cwd


def _collect_diff(args: argparse.Namespace, cwd: str, cfg: config.Config) -> str:
    if args.source == "-":
        raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES).decode("utf-8", errors="replace")
        return diff.prepare(raw, cfg)
    if args.source is not None:
        raise ValueError("unexpected argument %r (use '-' to read a diff from stdin)" % args.source)
    if args.staged:
        return diff.compute_staged(cwd, cfg)
    if args.base:
        return diff.compute_range(cwd, args.base, cfg)
    return diff.compute(cwd, cfg)


def _apply_flags(cfg: config.Config, args: argparse.Namespace) -> config.Config:
    """CLI flags are typed by the user, so they are TRUSTED (like env vars)."""
    changes = {}
    if args.provider:
        changes["provider"] = args.provider
    if args.jury:
        changes["jury"] = config.parse_jury(args.jury)
    if args.quorum:
        changes["quorum"] = min(max(args.quorum, 1), config.MAX_JURY)
    if args.threshold:
        changes["threshold"] = args.threshold
    if args.timeout:
        changes["timeout_sec"] = min(max(args.timeout, 1), 600)
    if args.strict:
        changes["fail_open"] = False
    if args.no_stats:
        changes["stats"] = False
    return dataclasses.replace(cfg, **changes)


def _render(
    p: ui.Painter,
    verdict: engine.Verdict,
    blocking: List[Finding],
    cfg: config.Config,
    summary: str,
) -> None:
    jury = verdict.jury_size
    who = ", ".join(verdict.reviewers)
    ui.out()
    ui.out("%s %s %s" % (
        p("crosscheck", "bold", "accent"),
        p("·", "dim"),
        p("%s · %s %s" % (summary, "jury:" if jury > 1 else "reviewer:", who), "dim"),
    ))
    ui.out()
    blocking_ids = {id(f) for f in blocking}
    for f in verdict.findings:
        for line in ui.finding_block(p, f, jury, id(f) in blocking_ids):
            ui.out(line)
        ui.out()
    if verdict.failures:
        for fail in verdict.failures:
            ui.out(p("  ! juror unavailable: %s" % fail, "yellow"))
        ui.out()
    if blocking:
        n = len(blocking)
        msg = "✗ %d blocking issue%s" % (n, "" if n == 1 else "s")
        agreed = sum(1 for f in blocking if len(f.reviewers) > 1)
        extra = " · %d confirmed by %d+ models" % (agreed, 2) if jury > 1 and agreed else ""
        ui.out("%s%s  %s" % (
            p(msg, "bold", "red"), extra,
            p("(threshold: %s%s)" % (cfg.threshold, ", quorum: %d" % cfg.quorum if jury > 1 else ""), "dim"),
        ))
    elif verdict.findings:
        ui.out(p("✓ passed", "bold", "green") + p(" — %d minor finding(s) below the '%s' threshold"
                                                   % (len(verdict.findings), cfg.threshold), "dim"))
    else:
        ui.out(p("✓ passed", "bold", "green") + p(" — no issues found by %s" % who, "dim"))
    if verdict.same_vendor:
        ui.out(p("note: Claude reviewing Claude isn't a second opinion — add codex or gemini.", "dim"))


def cmd_review(args: argparse.Namespace) -> int:
    p = ui.Painter(ui.use_color(sys.stdout) and not args.json)
    perr = ui.Painter(ui.use_color(sys.stderr))
    cwd = _git_toplevel(os.getcwd())
    cfg = _apply_flags(config.load(cwd), args)

    def note(text: str) -> None:
        sys.stderr.write(perr("crosscheck: %s" % text, "dim") + "\n")

    if not cfg.enabled:
        note("disabled (CROSSCHECK_ENABLED=0); nothing to do.")
        return 0
    try:
        review_diff = _collect_diff(args, cwd, cfg)
    except ValueError as exc:
        sys.stderr.write("crosscheck: %s\n" % exc)
        return 2
    if not review_diff:
        if args.json:
            print(json.dumps({"verdict": "pass", "reviewed": False, "findings": []}))
        else:
            note("nothing to review.")
        return 0

    files, added, removed = diff.summarize(review_diff)
    summary = "%d file%s (+%d −%d)" % (files, "" if files == 1 else "s", added, removed)
    names = ", ".join(engine.juror_names(cfg))
    spin = not args.json and sys.stderr.isatty()
    try:
        with _Spinner(perr, "reviewing %s with %s…" % (summary, names), spin):
            verdict = engine.run_review(review_diff, cfg, note)
    except ProviderError as exc:
        msg = str(exc)
        if cfg.fail_open:
            note("%s — skipped (fail-open). Run `crosscheck doctor`." % msg)
            if args.json:
                print(json.dumps({"verdict": "error", "error": msg, "findings": []}))
            return 0
        sys.stderr.write("crosscheck: %s\n" % msg)
        if args.json:
            print(json.dumps({"verdict": "error", "error": msg, "findings": []}))
        return 2

    blocking = verdict.blocking(cfg.threshold, cfg.quorum)
    if cfg.stats:
        stats.record(verdict.findings, blocking, verdict.reviewers, bool(blocking))

    if args.json:
        blocking_ids = {id(f) for f in blocking}
        print(json.dumps({
            "verdict": "changes-requested" if blocking else "pass",
            "reviewed": True,
            "reviewers": verdict.reviewers,
            "failures": verdict.failures,
            "threshold": cfg.threshold,
            "quorum": cfg.quorum,
            "blocking": len(blocking),
            "findings": [dict(f.to_dict(), blocking=id(f) in blocking_ids) for f in verdict.findings],
        }, indent=2))
    else:
        _render(p, verdict, blocking, cfg, summary)
    return 1 if blocking else 0


# ---------------------------------------------------------------------------- stats


def cmd_stats(args: argparse.Namespace) -> int:
    if args.reset:
        stats.reset()
        print("crosscheck: stats reset.")
        return 0
    data = stats.load()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if args.badge:
        print(stats.badge_markdown(data))
        return 0
    if args.svg:
        try:
            with open(args.svg, "w", encoding="utf-8") as fh:
                fh.write(stats.svg_card(data))
        except OSError as exc:
            sys.stderr.write("crosscheck: could not write %s: %s\n" % (args.svg, exc))
            return 2
        print("crosscheck: wrote %s — drop it in your README or post it." % args.svg)
        return 0
    p = ui.Painter(ui.use_color(sys.stdout))
    print(ui.stats_card(p, data, stats.last_days(data, 14)))
    if data["reviews"]:
        print(p("  share it:  crosscheck stats --badge   ·   crosscheck stats --svg crosscheck.svg", "dim"))
    else:
        print(p("  nothing yet — run `crosscheck` on a change, or `crosscheck demo` to see it in action.", "dim"))
    return 0


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    p = ui.Painter(ui.use_color(sys.stdout))
    cfg = config.load(_git_toplevel(os.getcwd()))
    opt_in = providers._unsandboxed_opt_in()
    print(p("crosscheck %s" % __version__, "bold", "accent"))
    print()
    print(p("reviewers", "bold"))
    found = 0
    for name, note in (
        ("codex", "OpenAI · write-sandboxed (--sandbox read-only) · used by `auto`"),
        ("gemini", "Google · no OS sandbox · needs CROSSCHECK_ALLOW_UNSANDBOXED=1"),
        ("claude", "Anthropic · no OS sandbox · needs CROSSCHECK_ALLOW_UNSANDBOXED=1"),
    ):
        try:
            path = providers._resolve_binary(name)
        except ProviderError:
            path = None
        usable = path is not None and (name == "codex" or opt_in)
        found += 1 if usable else 0
        mark = p("✓", "green") if usable else (p("~", "yellow") if path else p("✗", "red"))
        print("  %s %-7s %s" % (mark, name, p(path or "not installed", "dim")))
        print("            %s" % p(note, "dim"))
    print()
    print(p("config", "bold"))
    print("  provider   %s" % cfg.provider)
    print("  jury       %s" % (", ".join(cfg.jury) or "(off — set CROSSCHECK_JURY=codex,gemini)"))
    print("  quorum     %d" % cfg.quorum)
    print("  threshold  %s" % cfg.threshold)
    print("  fail_open  %s" % cfg.fail_open)
    print("  stats      %s" % (stats.path() if cfg.stats else "off"))
    print()
    if not found:
        print(p("No usable reviewer yet. Fastest path: install codex (npm i -g @openai/codex), "
                "then run `codex login`.", "yellow"))
        return 1
    if found == 1 and not cfg.jury:
        print(p("Tip: with two reviewers installed, try `crosscheck --jury codex,gemini` — "
                "issues both models flag are rarely false alarms.", "dim"))
    return 0


# ---------------------------------------------------------------------- install-hook


def _hook_script() -> str:
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return """#!/bin/sh
{marker}
# Reviews staged changes with an independent model before each commit.
# Skip once with: git commit --no-verify   ·   Remove: delete this file.
if command -v crosscheck >/dev/null 2>&1; then
  exec crosscheck review --staged
fi
PYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{py}" -m crosscheck review --staged
""".format(marker=_HOOK_MARKER, root=pkg_root, py=sys.executable)


def cmd_install_hook(args: argparse.Namespace) -> int:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("crosscheck: git not available: %s\n" % exc)
        return 2
    if out.returncode != 0:
        sys.stderr.write("crosscheck: not inside a git repository.\n")
        return 2
    hooks_dir = os.path.abspath(out.stdout.strip())
    target = os.path.join(hooks_dir, "pre-commit")
    if os.path.lexists(target) and not args.force:
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                ours = _HOOK_MARKER in fh.read(4096)
        except OSError:
            ours = False
        if not ours:
            sys.stderr.write(
                "crosscheck: %s already exists and isn't ours. Re-run with --force to "
                "replace it, or add `crosscheck review --staged` to it yourself.\n" % target
            )
            return 2
    os.makedirs(hooks_dir, exist_ok=True)
    if os.path.islink(target):
        os.unlink(target)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(_hook_script())
    mode = os.stat(target).st_mode
    os.chmod(target, mode | stat_mod.S_IXUSR | stat_mod.S_IXGRP | stat_mod.S_IXOTH)
    print("crosscheck: installed %s" % target)
    print("Every `git commit` now gets an independent review. Skip once with --no-verify.")
    return 0


# ----------------------------------------------------------------------------- demo

_DEMO_DIFF = """diff --git a/app/auth/session.py b/app/auth/session.py
--- a/app/auth/session.py
+++ b/app/auth/session.py
@@ -38,7 +38,12 @@ def is_valid(token):
-    return token.expires_at > now()
+    # allow tokens right up to expiry
+    return token.expires_at >= now()
diff --git a/app/api/users.py b/app/api/users.py
--- a/app/api/users.py
+++ b/app/api/users.py
@@ -85,6 +85,9 @@ def search_users(request):
+    q = request.args.get("q", "")
+    rows = db.execute(f"SELECT * FROM users WHERE name LIKE '%{q}%'")
+    return jsonify([dict(r) for r in rows])
diff --git a/app/jobs/export.py b/app/jobs/export.py
--- a/app/jobs/export.py
+++ b/app/jobs/export.py
@@ -12,4 +12,6 @@ def export_all(users):
+    for u in users:
+        fh = open(f"/tmp/export-{u.id}.csv", "w")
+        fh.write(to_csv(u))
"""


def _demo_verdict() -> engine.Verdict:
    def f(sev, cat, file, line, summary, detail, who):
        return Finding(sev, cat, file, line, summary, detail, list(who))

    findings = [
        f("blocker", "security", "app/api/users.py", 87,
          "SQL injection: user input interpolated into the query",
          "`q` goes straight into an f-string SQL statement. Use a parameterized "
          "query: db.execute(\"... LIKE ?\", (f\"%{q}%\",)).", ("codex", "gemini")),
        f("blocker", "bug", "app/auth/session.py", 40,
          "Expired token accepted at the exact expiry instant",
          "`>=` lets a token whose expires_at == now() through one more request. "
          "Keep the strict `>` comparison.", ("codex", "gemini")),
        f("warn", "bug", "app/jobs/export.py", 14,
          "File handle leaked on every loop iteration",
          "open() without close()/with — on large exports this exhausts file "
          "descriptors. Use `with open(...) as fh:`.", ("gemini",)),
        f("nit", "style", "app/jobs/export.py", 13,
          "Predictable path in shared /tmp",
          "Consider tempfile.mkstemp() so exports can't collide or be pre-created.",
          ("codex",)),
    ]
    return engine.Verdict(reviewers=["codex", "gemini"], findings=findings)


def cmd_demo(args: argparse.Namespace) -> int:
    p = ui.Painter(ui.use_color(sys.stdout))
    perr = ui.Painter(ui.use_color(sys.stderr))
    print(p("crosscheck demo", "bold", "accent") + p(" — simulated output on a sample diff; "
                                                     "no models are called, nothing is recorded.", "dim"))
    print()
    print(p("Your AI just wrote this change and says it looks good:", "dim"))
    for line in _DEMO_DIFF.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            print("  " + p(line, "green"))
        elif line.startswith("-") and not line.startswith("---"):
            print("  " + p(line, "red"))
        elif line.startswith("diff --git"):
            print("  " + p(line.split(" b/", 1)[-1], "bold"))
    cfg = config.Config(jury=["codex", "gemini"])
    verdict = _demo_verdict()
    files, added, removed = diff.summarize(_DEMO_DIFF)
    summary = "%d files (+%d −%d)" % (files, added, removed)
    pause = 0 if args.fast or not sys.stderr.isatty() else 1.6
    with _Spinner(perr, "reviewing %s with codex, gemini…" % summary, pause > 0):
        time.sleep(pause)
    _render(p, verdict, verdict.blocking(cfg.threshold, cfg.quorum), cfg, summary)
    print()
    print(p("Two models from different vendors independently flagged the same two bugs.", "dim"))
    print(p("Try it for real:  crosscheck doctor   ·   crosscheck --jury codex,gemini", "dim"))
    return 0


# ----------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crosscheck",
        description="One model shouldn't grade its own homework. An independent "
                    "AI model reviews your diff before you accept it.",
    )
    parser.add_argument("--version", action="version", version="crosscheck %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    r = sub.add_parser("review", help="review a diff (default command)")
    r.add_argument("source", nargs="?", help="'-' to read a unified diff from stdin")
    src = r.add_mutually_exclusive_group()
    src.add_argument("--staged", action="store_true", help="review staged changes only")
    src.add_argument("--base", metavar="REF", help="review this branch vs REF (merge-base), like a PR")
    r.add_argument("--jury", metavar="A,B", help="run several reviewers, e.g. codex,gemini")
    r.add_argument("--quorum", type=int, metavar="N", help="jurors that must agree for a finding to block")
    r.add_argument("--provider", help="single reviewer: auto|codex|gemini|claude|command")
    r.add_argument("--threshold", choices=config.VALID_THRESHOLDS, help="minimum severity that fails")
    r.add_argument("--timeout", type=int, metavar="SEC", help="reviewer time budget")
    r.add_argument("--strict", action="store_true", help="exit 2 if the review can't run (no fail-open)")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    r.add_argument("--no-stats", action="store_true", help="don't count this run in `crosscheck stats`")
    r.set_defaults(func=cmd_review)

    s = sub.add_parser("stats", help="what crosscheck has caught for you")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--badge", action="store_true", help="print a README badge (markdown)")
    g.add_argument("--svg", metavar="PATH", help="write a shareable SVG card")
    g.add_argument("--json", action="store_true", help="raw counts as JSON")
    g.add_argument("--reset", action="store_true", help="delete the local tally")
    s.set_defaults(func=cmd_stats)

    d = sub.add_parser("doctor", help="check which reviewers are installed")
    d.set_defaults(func=cmd_doctor)

    h = sub.add_parser("install-hook", help="run crosscheck on every git commit")
    h.add_argument("--force", action="store_true", help="replace an existing pre-commit hook")
    h.set_defaults(func=cmd_install_hook)

    m = sub.add_parser("demo", help="offline walkthrough (no models, no keys)")
    m.add_argument("--fast", action="store_true", help="skip the pause")
    m.set_defaults(func=cmd_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "review")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        sys.stderr.write("\ncrosscheck: interrupted.\n")
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
