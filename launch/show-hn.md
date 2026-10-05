# Show HN

**Title** (≤ 80 chars, no hype words):

> Show HN: Crosscheck – a second AI model reviews your AI's code before you accept it

**Post:**

I kept accepting code from AI agents after the same model told me it "looks good". That's one model grading its own homework, and it shares its own blind spots.

crosscheck sends the diff to a *different* model (Codex/GPT, Gemini, or any CLI), or to a jury of them in parallel, and fails the commit, or blocks the agent's turn, if they find a real problem. In jury mode each finding shows which models flagged it. When two vendors independently flag the same line, it's rarely a false alarm, and `--quorum 2` blocks only on those.

It works with whatever wrote the code:

    crosscheck                  # uncommitted changes
    crosscheck --staged         # pre-commit
    crosscheck --base main      # the whole branch, like a PR
    git diff | crosscheck -

There's also a Claude Code Stop-hook plugin that won't let the agent finish until it fixes what the reviewer found.

Design choices people usually ask about:

- Local, zero dependencies (Python stdlib), no account, no telemetry. It shells out to reviewer CLIs you already have and are logged in to.
- Secrets are redacted from the diff before it leaves, and from the reviewer's output on the way back.
- A repo's `.crosscheck.json` is treated as hostile: it can only make the gate stricter, never weaker or more expensive.
- The threat model is in the README, including what it does *not* protect against (reviewer filesystem reads are not sandboxed).

Try it without installing anything (offline and simulated, no keys):

    uvx --from git+https://github.com/karhuzin-lgtm/crosscheck crosscheck demo

https://github.com/karhuzin-lgtm/crosscheck

I'd love feedback on the jury merging (how findings from different models are matched) and on which reviewers to support next. Local models via Ollama are high on my list.
