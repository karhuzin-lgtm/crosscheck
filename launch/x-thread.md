# X / Bluesky thread

**1/** (attach docs/demo.gif)
Your AI writes the code. Then the same AI reviews it and says "looks good ✅".

That's one model grading its own homework.

I built crosscheck: a *different* model reviews the diff before you accept it. Open source, local, zero deps.

**2/**
It works with whatever wrote the code (Cursor, Copilot, Claude Code, Aider, you):

crosscheck --staged → before every commit
crosscheck --base main → the whole branch, like a PR
git diff | crosscheck - → anything

**3/**
Jury mode: Codex + Gemini review in parallel.

Every finding shows who flagged it. When two vendors independently flag the same line, it's almost never a false alarm.

--quorum 2 = block only on those.

**4/**
Inside Claude Code it's a Stop hook: the agent literally can't finish its turn until it fixes what the other model found.

Secrets are redacted both ways, and a hostile repo config can only make it stricter. The threat model is in the README.

**5/**
Try it in 10 seconds. Offline demo, no keys:

uvx --from git+https://github.com/karhuzin-lgtm/crosscheck crosscheck demo

⭐ github.com/karhuzin-lgtm/crosscheck
