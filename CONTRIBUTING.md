# Contributing

Thanks for helping. Most contributions are small and fall into a few kinds: a new reviewer adapter, a better redaction pattern, sharper prompt wording, or a bug fix.

## Setup

```bash
git clone https://github.com/karhuzin-lgtm/crosscheck && cd crosscheck
python -m unittest discover -s test     # ~130 tests, a few seconds, no network
./scripts/crosscheck demo               # run the CLI from the checkout
```

There's nothing to install. crosscheck is standard-library only and supports Python 3.8+.

## Ground rules

- **No runtime dependencies.** It runs inside other people's hooks, so stdlib only.
- **Never raise out of the hook.** `gate.main()` must always exit 0, and reviewer problems go through the fail-open/strict policy.
- **Treat the repo as hostile.** Anything read from the diff or `.crosscheck.json` is untrusted. New config keys need an explicit rule in `config._apply_file` (usually: ignored from the project file) and a test.
- **Anything the reviewer returns is untrusted too.** Redact it and cap it before showing it.
- **Every behavior change gets a test.** Fake reviewers with `fake_providers()` / `fake_binaries()` in `test/test_cli.py`. Tests must never call a real model.

## Adding a reviewer

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#adding-a-reviewer). In short: an argv entry, a trust decision (tools? sandbox? opt-in?), a `doctor` line, and tests.

## Pull requests

Keep them focused, describe the *why*, and run the test suite before pushing. CI runs it on Python 3.8 through 3.12.
