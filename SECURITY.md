# Security policy

crosscheck processes untrusted input by design: diffs and `.crosscheck.json` files from repositories you may not control. The threat model, including what is **not** defended, is in the README under [Security model](README.md#security-model).

## Reporting a vulnerability

Please report privately through GitHub's **Security → Report a vulnerability** on this repository, not in a public issue. Include a minimal reproduction (a diff or config file) and what it achieves.

These are especially interesting:
- a project `.crosscheck.json` that weakens or bypasses the gate,
- a diff that makes crosscheck (not the reviewer) leak a secret, execute a command, or crash the hook,
- redaction bypasses for common credential formats.

You can expect an acknowledgement within a few days. Fixes are released as soon as they're ready and credited in the changelog unless you'd rather not be named.
