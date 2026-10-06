# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [SemVer](https://semver.org/).

## [0.2.1]

### Fixed
- Reviewer CLIs installed outside the system dirs were reported as "not installed". This covered Homebrew on macOS (`/opt/homebrew/bin`), npm/nvm globals and `~/.local/bin`, so it affected most Mac users. crosscheck now also searches those dirs and your `$PATH`. It skips relative, repo-internal and world-writable dirs and ones owned by another user, so a repo still can't shadow a reviewer. The reviewer process gets the same PATH, so `#!/usr/bin/env node` CLIs can find node.

## [0.2.0]

### Added
- **Standalone `crosscheck` CLI** that reviews any git diff: uncommitted (default), `--staged`, `--base REF` (merge-base, like a PR), or stdin (`-`). Exit codes `0` pass / `1` blocking / `2` error.
- **Jury mode** (`--jury`, `CROSSCHECK_JURY`): several reviewers run in parallel, and findings are merged across models with per-finding agreement ("2/2 agree"). `--quorum N` blocks only on issues N jurors flagged.
- **Ollama provider** for fully local review. It is tool-less, so it needs no unsandboxed opt-in.
- **Per-reviewer models** with `name:model` (e.g. `ollama:qwen2.5-coder:7b`, `codex:gpt-5`).
- **SARIF 2.1.0 output** (`--sarif`) for GitHub code scanning, and `--json` for scripts.
- `crosscheck install-hook` (git pre-commit) and `.pre-commit-hooks.yaml` for the pre-commit framework.
- `crosscheck stats`: a local, counts-only tally shown as a terminal card, a README badge (`--badge`), or an SVG card (`--svg`).
- `crosscheck doctor` (which reviewers are installed and how they're isolated) and `crosscheck demo` (offline, simulated walkthrough).
- `pyproject.toml`: installable with pipx/uvx. The distribution is named `crosscheck-cli`; the command is `crosscheck`.

### Changed
- The Claude Code Stop hook now runs through the same `engine` as the CLI, so it supports jury mode too.
- In jury mode, `CROSSCHECK_MODEL` is no longer applied to every juror. Use `name:model` per juror.
- A juror that needs `CROSSCHECK_ALLOW_UNSANDBOXED=1` but lacks it is now reported, instead of being silently replaced by codex.

### Fixed
- The README claimed a project `.crosscheck.json` could raise `max_rounds`/`timeout_sec`/`max_diff_bytes`. The code ignores those keys from the project file (by design), and the docs now say so.

## [0.1.0]

- First release: a Claude Code Stop-hook plugin that sends the working-tree diff to an independent reviewer (codex, gemini, claude, or a custom command) and blocks the turn on findings at or above a threshold. Includes secret redaction in both directions, a strengthen-only project config, fail-open behavior, and a loop guard.
