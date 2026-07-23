"""Redact secrets from diff text before it leaves the machine.

This is the trust backbone: the reviewer call is the only outbound action, so any
credential-shaped content is masked *before* the prompt is built. File-level
skipping (``.env``, key files, etc.) is handled by diff.py's exclude globs; this
module handles inline values that slip through.

Redaction preserves diff structure (line prefixes, key names) so the reviewer can
still reason about the change — only the secret value is replaced with a marker.
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

MASK = "[REDACTED]"

# --- Whole-token patterns: replace the entire matched credential. ---------------
# Each entry is (compiled_regex, replacement). Order matters only for readability.
_TOKEN_PATTERNS: List[Tuple[Pattern[str], str]] = [
    # PEM private key blocks (any type: RSA, EC, OPENSSH, PGP, ...).
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    # OpenAI-style keys: sk-..., sk-proj-..., etc.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), MASK),
    # GitHub tokens: personal, oauth, server, refresh, and fine-grained PATs.
    (re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), MASK),
    # Slack tokens.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), MASK),
    # AWS access key id.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    # Google API key.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), MASK),
    # Slack incoming-webhook / generic bearer-ish long hex secrets are left to the
    # assignment patterns below to avoid over-masking ordinary hashes.
]

# --- Assignment patterns: mask the value, keep the key name. --------------------
# Group 1 = key + operator (kept), the value is masked.
_ASSIGNMENT_PATTERNS: List[Pattern[str]] = [
    # key: "value" / key = 'value' / key=value in code (api_key, secret, token...).
    # Quote-aware: consume through the matching closing quote so a multi-word
    # quoted secret ("correct horse battery staple") is masked in full, not just
    # up to the first space. Fallback is an unquoted, whitespace-free value.
    re.compile(
        r"""(?ix)
        ( (?:api[_-]?key|apikey|secret|client[_-]?secret|password|passwd|pwd
            |access[_-]?token|auth[_-]?token|refresh[_-]?token|token|bearer)
          \s*[:=]\s* )
        (?:
            " (?!\[REDACTED) (?P<dqv>[^"]{6,}) "
          | ' (?!\[REDACTED) (?P<sqv>[^']{6,}) '
          |   (?!\[REDACTED) (?P<uqv>[^\s'"]{6,})
        )
        """,
    ),
    # .env / shell style: UPPER_SNAKE names containing KEY/TOKEN/SECRET/PASS/etc.
    re.compile(
        r"""(?mx)
        ^([+\-]?\s*[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASS|PASSWORD|CREDENTIAL|API)[A-Z0-9_]*\s*=\s*)
        ((?!\[REDACTED)\S{4,})
        """,
    ),
]


def redact(text: str) -> Tuple[str, int]:
    """Return ``(redacted_text, redaction_count)``.

    ``redaction_count`` is the total number of secrets masked; a non-zero value
    should be surfaced to the user as a loud notice.
    """
    if not text:
        return text, 0

    count = 0

    for pattern, replacement in _TOKEN_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n

    # Assignment 1: key + value -> keep key (and quotes, if any), mask value.
    def _mask_kv(match: "re.Match[str]") -> str:
        key = match.group(1)
        if match.group("dqv") is not None:
            return key + '"' + MASK + '"'
        if match.group("sqv") is not None:
            return key + "'" + MASK + "'"
        return key + MASK

    text, n = _ASSIGNMENT_PATTERNS[0].subn(_mask_kv, text)
    count += n

    # Assignment 2: env-style KEY=value -> keep "KEY=", mask value.
    def _mask_env(match: "re.Match[str]") -> str:
        return match.group(1) + MASK

    text, n = _ASSIGNMENT_PATTERNS[1].subn(_mask_env, text)
    count += n

    return text, count
