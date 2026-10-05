# crosscheck — design (internal, not shipped)

## One line
An independent second model reviews the code your AI agent wrote — before you accept it.
Tagline candidates: "One model shouldn't grade its own homework." / "A second pair of eyes for AI-written code."

## Problem (validated by research 2026)
- People fear accepting code only one AI has seen. "Almost right but not quite" = top complaint (~66%); debugging AI code takes longer (~45%).
- Existing solutions are either cloud/post-PR (GitHub Actions review bots) or same-model self-review (Claude grading Claude). No clean, LOCAL, pre-completion gate where a DIFFERENT model reviews the diff. That's the wedge.

## What it does
Claude Code Stop-hook plugin. When Claude finishes a turn that changed code:
1. Compute the change set (working-tree diff).
2. Send diff + minimal context to a configured INDEPENDENT reviewer model (different vendor by default).
3. Reviewer returns structured findings (severity: blocker/warn/nit; category; file:line; why).
4. If any finding >= threshold → BLOCK completion and feed findings back to Claude to fix. Else pass.
5. Log a running "issues caught" tally (the demo/social-proof number).

## Differentiators (why stars)
- LOCAL + pre-completion (before you leave the session), not cloud/post-PR.
- CROSS-MODEL by design (reviewer != author vendor). The whole point.
- Zero-config sane defaults; one-command install.
- Beautiful terminal output + caught-issues counter.

## Reviewer provider interface (pluggable)
A provider = a command that takes review input on stdin/args and returns findings JSON.
Built-in adapters:
- `codex`  -> OpenAI Codex/GPT CLI
- `gemini` -> Gemini CLI
- `claude` -> claude CLI (allowed but warns: same-vendor as common author; not ideal)
- `command`-> generic: user supplies a shell command template
Contract: adapter receives {diff, files[], instructions} and must emit findings JSON (schema below). Non-zero exit or unparseable => fail-open with a visible warning (never hard-block on tool failure; log it).

## Findings schema (reviewer output)
{
  "findings": [
    {"severity":"blocker|warn|nit","category":"bug|security|perf|logic|style",
     "file":"path","line":123,"summary":"one line","detail":"why + fix"}
  ],
  "verdict":"pass|changes-requested"
}

## Config surface
Resolution order: project `.crosscheck.json` > env vars > plugin defaults.
Keys: provider, model, threshold (blocker|warn|nit), include/exclude globs,
max_diff_bytes, timeout_sec, fail_open (bool, default true), enabled (bool).
Env mirror: CROSSCHECK_PROVIDER, CROSSCHECK_MODEL, CROSSCHECK_THRESHOLD, ...

## Safety / trust
- NEVER sends secrets: diff is scanned; lines matching secret patterns are redacted before leaving the machine, and files matching .gitignore/secret globs are skipped. Loud notice if redaction happened. The reviewer's OUTPUT is redacted with the same masks on the way back, AND so is any ERROR TEXT from a crashed reviewer (a reviewer that reads a secret then exits nonzero puts stderr into ProviderError → gate prints it; redacted at both the providers.run() construction point and the gate._fail() output chokepoint). Defense-in-depth against a prompt-injected reviewer surfacing a local secret it read.
- Untrusted `.crosscheck.json`: the project file is treated as hostile (checked into a cloned repo). It may only STRENGTHEN the gate vs the env/default baseline — enable it, lower the threshold, raise max_rounds/timeout_sec/max_diff_bytes. It may NOT disable it, raise the threshold, lower max_rounds, lower timeout_sec (hostile timeout=1 forces fail-open) or max_diff_bytes (hostile 1000 truncates the diff), turn off fail-open, pick the reviewer MODEL (env-only CROSSCHECK_MODEL) or COMMAND (env-only CROSSCHECK_COMMAND) — both ignored entirely from the file — select an un-sandboxed/command reviewer (only `auto`/`codex` accepted from the file), or shrink coverage (include/exclude from the file are ignored; use env). Trust is tracked per-field (defaults+env = trusted, file = untrusted); strengthen-only fields capture the trusted baseline before applying the file.
- fail-open by default (tool/model failure must never wedge the user's session), with a visible warning. Optional strict mode fail-closed. gate.main() never raises and always exits 0, even on a malformed/hostile hook payload (non-dict JSON, non-str cwd/session_id, bad JSON).
- Loop-guard state lives in a per-user private dir ($XDG_RUNTIME_DIR or a uid-namespaced temp dir), created 0700 and verified (real dir, owned by us, not a symlink); session subdir = sha256(session_id). Counter files are read/written with O_NOFOLLOW + fstat regular-file/owner checks; any failure degrades gracefully (counter resets; Claude Code still caps consecutive blocks).
- **Residual (v0.1, intentional):** the reviewer is a user-installed, authenticated CLI that crosscheck TRUSTS to execute. crosscheck does NOT OS-sandbox the reviewer's filesystem reads (HOME is passed through so its config/auth works; codex is `--sandbox read-only`, which stops writes, not reads). A determined prompt-injection in a diff could induce the reviewer to read a local file and echo it — the in+out redaction masks common secret shapes but is not containment. A real OS sandbox (bwrap/landlock/seccomp) is deliberately OUT OF SCOPE for a stdlib-only v0.1. Un-sandboxed gemini/claude are gated behind CROSSCHECK_ALLOW_UNSANDBOXED=1.
- No telemetry. Everything local. The reviewer call is the only outbound (to the model provider the user already uses).

## Loop safety
Use stop_hook_active (from hook stdin) to avoid infinite Stop->fix->Stop loops: after N gate rounds, pass with a warning summarizing unresolved findings.

## Naming (decide before publish)
Working: `crosscheck`. Alts: second-opinion, peerpass, crossmodel, diffjudge.
Repo: karhuzin-lgtm/crosscheck (PUBLIC).

## OPEN (pending official hook/packaging specs — do NOT invent)
- Exact Stop-hook block JSON ({"decision":"block","reason":...}?) + stop_hook_active field.
- transcript_path / cwd / session_id availability in Stop stdin; best diff source.
- Exit-code semantics (0 vs 2) for Stop.
- Canonical PLUGIN layout + install/marketplace flow + exact one-line install for README.
- Settings merge order (user/project/local).

## v0.2 — beyond the hook
- `engine.py`: one review path shared by the Stop hook and the CLI. Single reviewer, or a JURY (CROSSCHECK_JURY / --jury) run in parallel. Findings are merged across jurors (same file, lines within ±3, same category or similar wording), each carries `reviewers[]`, and `quorum` sets how many jurors must agree to block (capped at the jurors that actually answered). A juror that fails or returns garbage is reported, not trusted as a pass. If all jurors fail, the fail-open/strict policy applies.
- `cli.py`: standalone `crosscheck` (worktree / --staged / --base REF / stdin), `stats`, `doctor`, `install-hook`, `demo` (offline, simulated, labeled as such, never recorded).
- `stats.py`: counts-only local tally in a 0700 state dir, written atomically, never raises. It feeds the terminal card, the shields.io badge and the SVG card.
- Trust: jury/quorum/stats are env/flag-only. A project file can't multiply reviewer spend or raise the quorum to defang the gate.
