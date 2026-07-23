<div align="center">

# crosscheck

### One model shouldn't grade its own homework.

**An independent second model reviews the code your AI wrote — *before* you accept it.**

[![License: MIT](https://img.shields.io/badge/License-MIT-D2662F.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-1a1a1a.svg)](https://www.python.org)
[![Zero dependencies](https://img.shields.io/badge/deps-0-1a1a1a.svg)](#)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-D2662F.svg)](https://code.claude.com)

</div>

---

You let Claude write the code. Then the same model "checks" its own work, says "looks good!", and you hit accept. That's not a review — that's one AI grading its own homework.

**crosscheck** puts an *independent* model in the loop. When your coding agent finishes a change, a **different** model (GPT via Codex, Gemini, or any CLI you point it at) reviews the diff. If it finds a real problem, the turn is **blocked** and the findings are handed back to the agent to fix — before the work ever reaches you.

Local. No cloud. No account. No API keys of its own. Zero dependencies.

## Why

The single most common complaint about AI coding is *"it's almost right — but not quite."* The subtle bug, the missed edge case, the security slip. A model reviewing its own output shares its own blind spots. A **second, independent** model doesn't.

crosscheck is the missing local gate: cross-model review, at the moment of completion, in your own terminal.

## How it works

```
  Claude finishes a change
            │
            ▼
   ┌──────────────────┐     git diff of the change
   │  crosscheck gate │ ──────────────────────────────┐
   │   (Stop hook)    │                                │
   └──────────────────┘        secrets redacted        ▼
            │                              ┌───────────────────────┐
            │                              │  independent reviewer  │
            │        findings (JSON)       │  (codex / gemini /...) │
            │ ◀────────────────────────────└───────────────────────┘
            ▼
   issues ≥ threshold?  ──yes──▶  BLOCK the turn, hand findings back to fix
            │
            no
            ▼
        turn completes ✓   (issues-caught tally kept per session)
```

When crosscheck blocks, your agent sees exactly what to fix:

```
crosscheck: an independent reviewer (codex) found 1 issue(s) that should be addressed before finishing:

[BLOCKER] auth/session.py:42 — off-by-one lets an expired token pass one extra request
  `<=` should be `<`; at exactly expiry the check succeeds and the stale token is accepted once more.

Fix the issues above, then finish. (crosscheck round 1/2)
```

## Install

crosscheck is a standard Claude Code plugin — a Stop hook plus a small Python package. It needs **at least one reviewer CLI** on your PATH (`codex`, `gemini`, or `claude`).

**As a plugin (recommended):** in Claude Code, add this repo as a plugin marketplace, then install:

```
/plugin marketplace add karhuzin-lgtm/crosscheck
/plugin install crosscheck@crosscheck
```

**Manual (any agent that supports hooks):** point a `Stop` hook at the gate in your `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "/abs/path/to/crosscheck/scripts/crosscheck-gate" } ] }
    ]
  }
}
```

That's it. Next time your agent finishes a change, the reviewer runs.

## Configure

Zero-config by default (`auto` reviewer, blocks on `warn`+). Override via `.crosscheck.json` in your project, or `CROSSCHECK_*` env vars.

| Key | Env | Default | What it does |
|---|---|---|---|
| `provider` | `CROSSCHECK_PROVIDER` | `auto` | `auto` (→ `codex`, write-sandboxed), `codex`, `gemini`, `claude`, or `command` |
| *(env-only)* | `CROSSCHECK_MODEL` | *(cli default)* | Pin a specific reviewer model. **Env-only** — a `model` in a project file is ignored |
| `threshold` | `CROSSCHECK_THRESHOLD` | `warn` | Minimum severity that blocks: `nit` \| `warn` \| `blocker` |
| `max_rounds` | `CROSSCHECK_MAX_ROUNDS` | `2` | How many times it will block+re-review before letting you through |
| `fail_open` | `CROSSCHECK_FAIL_OPEN` | `true` | If the reviewer errors, allow the turn (never wedge your session) |
| `timeout_sec` | `CROSSCHECK_TIMEOUT` | `120` | Reviewer time budget (a project file may only *raise* it) |
| `max_diff_bytes` | `CROSSCHECK_MAX_DIFF_BYTES` | `200000` | How much of the diff is reviewed (a project file may only *raise* it) |
| `enabled` | `CROSSCHECK_ENABLED` | `true` | Master switch |
| `include` | `CROSSCHECK_INCLUDE` | `*` | Comma-separated globs of paths to review (env/global only — see below) |
| `exclude` | `CROSSCHECK_EXCLUDE` | *(vendored dirs, lockfiles, binaries…)* | Comma-separated globs to skip; env value **extends** the defaults |
| *(opt-in)* | `CROSSCHECK_ALLOW_UNSANDBOXED` | *(off)* | Set `1` to permit the un-sandboxed `gemini`/`claude` reviewers at all |
| *(opt-in)* | `CROSSCHECK_ALLOW_COMMAND` | *(off)* | Set `1` to honor a `command` supplied by a project file |

Custom reviewer? Use the `command` provider — crosscheck pipes the prompt to stdin and reads your command's stdout. Supply it via the **env var** (trusted) so a cloned repo can't inject the command it runs:

```bash
CROSSCHECK_PROVIDER=command CROSSCHECK_COMMAND="my-reviewer --json"
```

### What a project `.crosscheck.json` may and may not do

A `.crosscheck.json` can arrive checked into a **cloned repo you don't control**, so crosscheck treats it as **untrusted**: it may only ever move the gate in the *strengthening* direction. Relative to your env/default baseline, a project file **can** turn the gate on, **lower** the threshold (stricter), **raise** `max_rounds`, **raise** `timeout_sec` (more time = less spurious fail-open), and **raise** `max_diff_bytes` (review more) — and it **cannot**:

- disable the gate (`enabled: false` is ignored),
- raise the threshold above your baseline or lower `max_rounds` below it,
- lower `timeout_sec` below your baseline (a hostile `timeout_sec: 1` that forces a fail-open bypass is **ignored**) or lower `max_diff_bytes` below it (a hostile `max_diff_bytes: 1000` that truncates the diff away is **ignored**),
- turn off fail-open (`fail_open: false` is ignored),
- pick the reviewer **model** — a `model` in the project file is **ignored entirely**; it comes only from `CROSSCHECK_MODEL`,
- supply the reviewer **command** — a `command` in the project file is **ignored entirely**; it comes only from `CROSSCHECK_COMMAND`,
- select `gemini`/`claude`/`command` — a project file may only request `auto` or `codex` (the recommended write-sandboxed reviewer); any other value is **ignored**, not an error,
- shrink review coverage: `include`/`exclude` from the project file are **ignored entirely**. Put vendored-dir excludes in `CROSSCHECK_EXCLUDE` / your global config, not the repo file.

`gemini`/`claude` (no OS sandbox here) require `CROSSCHECK_ALLOW_UNSANDBOXED=1` **and** an env-set provider; without the opt-in, a request for them from any source silently falls back to `auto` (codex-only).

## Security model

crosscheck processes an **untrusted diff** and an **untrusted `.crosscheck.json`** (both can arrive checked into a cloned repo you don't control). Here is precisely what it defends and what it does not — an honest threat model is a feature, not an apology.

- **Secrets are redacted on the way out *and* on the way back.** The diff is scanned and credential-shaped content (private keys, `sk-…`, GitHub/AWS/Google/Slack tokens, `KEY=…` assignments) is masked **before** it is sent to the reviewer. The reviewer's **output** — and any **error text** from a reviewer that crashes — is run through the same masks **before** it reaches your terminal or a block reason. If anything is redacted either way, you're told. Redaction is pattern-based: it's a strong filter for common secret shapes, not a guarantee for unusual ones.
- **The reviewer is a trusted, user-installed, authenticated CLI that crosscheck executes.** crosscheck runs it with a sanitized environment (ambient credentials dropped), a neutral working directory, and its binary resolved to an absolute path so a repo can't shadow it. `codex` additionally runs under its **`--sandbox read-only`** policy, so it cannot write.
- **crosscheck does NOT sandbox the reviewer's filesystem *reads*.** `codex` can still *read* local files, and `$HOME` is passed through so its own config/auth works. A crafted prompt-injection embedded in a diff you choose to review could therefore induce the reviewer to read a local file (e.g. `~/.ssh/…`) and try to surface it; the in+out redaction masks common secret shapes but is **not containment**. Treat the reviewer as processing untrusted input. For fully untrusted diffs, prefer `codex` (it at least runs write-sandboxed, so a compromised reviewer can't also modify your files) over the un-sandboxed `gemini`/`claude` ones (no OS isolation at all — gated behind an explicit `CROSSCHECK_ALLOW_UNSANDBOXED=1` opt-in for that reason). Neither path is a read-privacy boundary. **A real OS sandbox (bwrap / user-namespace / container) is intentionally out of scope for a stdlib-only, cross-platform v0.1 — it's unavailable and untestable on typical hosts.**
- **The untrusted project `.crosscheck.json` can only *strengthen* the gate.** It can enable it, lower the threshold, and raise `max_rounds` / `timeout_sec` / `max_diff_bytes`. It can **never** weaken or disable it, turn off fail-open, pick the reviewer **model** or **command**, or select an un-sandboxed provider (see the config section above).
- **Fail-open by default.** A missing CLI, a timeout, or unparseable output never blocks you — crosscheck warns and steps aside. Flip `fail_open: false` for a strict gate.
- **No telemetry, no network of its own.** The only outbound call is to the reviewer CLI you already use.
- **It can't loop forever.** After `max_rounds` it steps aside (and Claude Code independently caps consecutive blocks).

## FAQ

**Does it slow me down?** Only on turns that changed code, and only for as long as one review takes. Clean diffs pass silently.

**Won't it block on trivia?** The reviewer is told to be conservative and flag only real problems introduced by the diff. Tune `threshold` to taste (`blocker` = only the serious stuff).

**Can the reviewer be Claude too?** Yes, but `claude` (like `gemini`) runs without an OS sandbox, so it's gated behind `CROSSCHECK_ALLOW_UNSANDBOXED=1` and set via env — and you'll get a same-vendor warning, since the whole point is a *different* model. Install `codex` for a write-sandboxed, cross-model second opinion.

**Does it work outside Claude Code?** The core is a plain stdin/stdout hook. Any agent that supports a completion/Stop hook with the same contract can use it.

## Contributing

Issues and PRs welcome — new reviewer adapters especially. See [DESIGN.md](DESIGN.md) for the architecture.

## License

MIT © [Aleksei](https://github.com/karhuzin-lgtm)
