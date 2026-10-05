# Launch kit

Everything needed to launch crosscheck v0.2. Nothing here ships in the package.

| File | What it is |
|---|---|
| `demo.tape` | VHS script that records `docs/demo.gif` (`vhs launch/demo.tape`) |
| `show-hn.md` | Show HN title and post |
| `x-thread.md` | X / Bluesky thread (5 posts) |
| `reddit.md` | Short posts for r/programming, r/LocalLLaMA, r/ChatGPTCoding, r/ClaudeAI |

## Before launch day

- [ ] Record the GIF: `vhs launch/demo.tape`. Swap `docs/demo.png` for `docs/demo.gif` at the top of the README (the GIF is the main thing people share).
- [ ] **Run crosscheck for real** on a few of your own AI-written changes for a week. Collect 2–3 *real* catches (a screenshot of each, with the bug explained). Real catches persuade people; the simulated demo only shows what the output looks like.
- [ ] Put your real `crosscheck stats --svg` card in the README.
- [ ] Publish to PyPI as `crosscheck-cli` (`python -m build && twine upload dist/*`) so `pipx install crosscheck-cli` / `uvx --from crosscheck-cli crosscheck` work. Then update the README install lines.
- [ ] Tag `v0.2.0` and write a GitHub release with the GIF.
- [ ] Repo settings: description = "One model shouldn't grade its own homework. Cross-model AI code review, local, zero deps." Topics: `ai-code-review`, `llm`, `codex`, `gemini`, `claude-code`, `pre-commit`, `developer-tools`. Upload a social preview image (the SVG card or a GIF frame).
- [ ] Turn on GitHub Discussions, and open 3–4 `good first issue`s (new reviewer adapters: ollama/local models, aider, cursor-agent; a GitHub Action).

## Launch day

1. **Show HN** first thing on a weekday, US morning (about 8–10am ET). Stay in the thread for the first 3 hours and answer everything, especially the security questions. The README's threat model is your best answer.
2. **X thread** an hour later, with the GIF on post 1. Tag nobody unless you have a real relationship with them.
3. **Reddit**: one subreddit per day, not all at once. Read each sub's self-promo rules first.
4. Submit to newsletters / lists: Console.dev, TLDR, Changelog News, awesome-claude-code, awesome-ai-devtools.

## What tends to make dev tools spread

- **Under 10 seconds to "wow".** `crosscheck demo` needs no install and no keys, so lead with it everywhere.
- **A shareable artifact.** The stats card and badge mean every user's README advertises the tool.
- **A sharp, contrarian line.** "One model shouldn't grade its own homework" is a claim people repeat and argue with.
- **Honesty.** The public threat model and "this demo is simulated" disclaimer earn trust with HN readers. Never overclaim.
