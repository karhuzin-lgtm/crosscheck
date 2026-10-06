# Architecture

crosscheck is about 3,000 lines of standard-library Python plus about 1,600 lines of tests. This guide explains how it is put together and why each design decision was made.

## The idea in one paragraph

A model reviewing its own output shares its own blind spots. crosscheck takes a git diff, sends it to a **different** model (or several in parallel), parses structured findings, and turns them into a decision: block the agent's turn, fail the commit, or pass. Everything runs locally through reviewer CLIs the user has already installed and logged in to. crosscheck itself holds no keys and makes no network calls.

## Data flow

```
              entry points                          core                              output
 ┌───────────────────────────────┐
 │ gate.py   Claude Code Stop    │──┐
 │           hook (stdin JSON)   │  │   diff.py      what changed?  (worktree / staged /
 └───────────────────────────────┘  ├─▶              branch range / stdin) → filter → cap
 ┌───────────────────────────────┐  │      │
 │ cli.py    `crosscheck ...`    │──┘      ▼
 │           pre-commit hook     │      engine.py    redact ─▶ resolve juror(s) ─▶ run in
 └───────────────────────────────┘                   parallel ─▶ redact output ─▶ parse ─▶
                                                     merge across jurors ─▶ Verdict
                                           │
                                           ▼
                                   Verdict.blocking(threshold, quorum)
                                           │
              ┌────────────────────────────┼─────────────────────────────┐
              ▼                            ▼                             ▼
  gate: {"decision":"block"}     cli: terminal / --json / --sarif   stats.py: counts-only
  JSON for Claude Code           + exit code 0 / 1 / 2              local tally
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Builds a `Config` from defaults, then env vars (trusted), then `.crosscheck.json` (**untrusted**, can only make the gate stricter). Clamps every budget. |
| `diff.py` | Gets the change set from git: `HEAD` + untracked files, `--cached`, or `base...HEAD`. Filters by include/exclude globs and caps it at a byte budget without splitting UTF-8 characters. |
| `redact.py` | Masks credential-shaped strings (PEM keys, `sk-…`, GitHub/AWS/Google/Slack tokens, `KEY=value`). Used on the diff going out *and* on everything coming back. |
| `providers.py` | Turns a provider spec (`codex`, `ollama:qwen2.5-coder:7b`, …) into a `Reviewer` and runs it as a subprocess, hardened: absolute binary path from a fixed PATH, scrubbed environment, private temp working dir, stdin fed on a thread, capped stdout/stderr, and the whole process group killed on timeout. |
| `review.py` | The prompt, and a parser that pulls the first balanced JSON object out of chatty model output, then normalizes and caps every field. |
| `engine.py` | One review path shared by the hook and the CLI: run one reviewer or a jury in parallel, merge findings, and return a `Verdict`. |
| `gate.py` | The Claude Code Stop hook. Always exits 0 and never raises. Keeps a per-session round counter so the block → fix → block loop ends. |
| `cli.py` / `ui.py` | The standalone command (`review`, `stats`, `doctor`, `install-hook`, `demo`) and its terminal rendering. |
| `sarif.py` | SARIF 2.1.0 export for GitHub code scanning. |
| `stats.py` / `state.py` | Counts-only tally of what was caught, and the private-directory helpers both state files use. |

## Design decisions

**1. Shell out to CLIs instead of calling model APIs.**
The user already has `codex`/`gemini`/`claude`/`ollama` installed and logged in. Reusing them means no API keys, no SDKs, no dependency on any one vendor, and no billing setup. The cost is fragile argv details, so they all live in one table (`providers._ARGV`), and the `command` provider is an escape hatch.

**2. Zero dependencies.**
A hook runs on every agent turn and every commit, often in someone else's environment. Stdlib-only means it can't break on a dependency conflict, it installs instantly, and the whole code you have to trust is this repo. That's why the JSON extraction, glob filtering and SARIF output are written by hand.

**3. Fail-open by default.**
A code reviewer that wedges your editor when the network blips gets uninstalled. Any reviewer failure (missing CLI, timeout, garbage output) lets the turn through with a visible warning. `--strict` / `CROSSCHECK_FAIL_OPEN=0` flips it for people who want a hard gate.

**4. The repository is an attacker.**
crosscheck reads two things a cloned repo controls: the diff and `.crosscheck.json`. So:
- The config file can only make the gate **stricter**. Every field has an explicit rule in `config._apply_file`. Budgets, model, command, jury and coverage globs are trusted fields that only env vars and flags can set, because moving them in either direction is an attack: a hostile `timeout_sec: 1` forces fail-open, and a big jury multiplies spend.
- Reviewer binaries are resolved against a fixed PATH and refused if they resolve inside the repo, so a repo can't ship its own `codex`.
- The diff is prompt-injection input, so reviewer *output* is untrusted too. It gets redacted, control characters are stripped, and the number and length of findings are capped before anything is shown to the user or handed back to the agent.
- What is **not** defended is written down in the README's security model: reviewer filesystem *reads* aren't OS-sandboxed. Saying so honestly is better than implying a guarantee that doesn't exist.

**5. Jury merging is deliberately simple.**
Two findings are "the same issue" when they're in the same file, within ±3 lines, and share a category or enough wording (word-overlap ≥ 0.25). Without line numbers, only the wording counts. A juror never merges with itself. This misses some paraphrases, and that's the safe failure: the worst outcome is a duplicate shown as "only gemini", never two different bugs collapsed into one. `quorum` is capped at the number of jurors that actually answered, so a crashed juror can't make the rest unable to block.

**6. One engine, two front doors.**
Without a shared engine, the hook and the CLI would drift apart. `engine.run_review` owns redaction, resolution, execution and merging, so every security property holds identically for `git commit`, `crosscheck --base main`, and a Claude Code turn.

**7. Stats store numbers only.**
The tally powers the shareable card and badge, which only work if people are comfortable sharing them. So the file holds counts (reviews, severities, categories, reviewer names) and never code, paths or finding text.

## Testing

`python -m unittest discover -s test` runs about 130 tests in a few seconds with no network or model calls:

- **Pure logic:** redaction patterns, JSON extraction from messy output, finding normalization and caps, jury merging and quorum, SARIF shape.
- **Trust boundary:** one test per hostile `.crosscheck.json` field, to prove it is ignored or can only strengthen the gate.
- **Subprocess hardening:** real child processes that flood output, hang, or exit nonzero with secrets in stderr.
- **End to end:** throwaway git repos for staged and branch-range diffs, the CLI exit codes, and the pre-commit installer. Reviewers are faked by patching `providers.resolve` / `providers.run`.

CI runs the suite on Python 3.8 through 3.12, then installs the package and smoke-tests the CLI.

## Adding a reviewer

1. Add its argv to `providers._ARGV` (prompt on stdin) and its model flag to `_MODEL_FLAG`.
2. Decide its trust level in `_build_cli`. Does it have tools that can touch the filesystem (`tools`)? Is it OS-sandboxed (`sandboxed`)? Does it need the `CROSSCHECK_ALLOW_UNSANDBOXED` opt-in?
3. Add it to `cli.cmd_doctor` and the README table.
4. Test it with `fake_binaries()` in `test/test_cli.py`.
