"""Build the review prompt and parse the reviewer's findings.

The reviewer is asked to emit ONLY a JSON object matching the crosscheck findings
schema. Models routinely wrap JSON in prose or markdown fences, so we extract the
first balanced top-level JSON object rather than trusting the whole response to
be clean JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config

# Severity ordering: nit < warn < blocker.
SEVERITY_ORDER = {"nit": 0, "warn": 1, "blocker": 2}
VALID_CATEGORIES = {"bug", "security", "perf", "logic", "style"}

_PROMPT_TEMPLATE = """\
You are an INDEPENDENT senior code reviewer. Another AI wrote the change below and
is about to finish. Your job is to catch real problems it may have missed —
correctness bugs, security issues, broken logic, resource/perf regressions, and
clear violations of good practice — introduced BY THIS DIFF.

Rules:
- Review ONLY what the diff changes. Do not flag pre-existing code or ask for
  unrelated refactors.
- Be precise and conservative. Do not invent issues to look useful. If the change
  is fine, return an empty findings list with verdict "pass".
- Prefer the highest-impact issues. "blocker" = must fix before merging (bugs,
  security, data loss, crashes). "warn" = should fix. "nit" = minor/style.

Output format: respond with a SINGLE JSON object and NOTHING ELSE — no prose, no
markdown fences. Schema:

{{
  "findings": [
    {{
      "severity": "blocker" | "warn" | "nit",
      "category": "bug" | "security" | "perf" | "logic" | "style",
      "file": "path/to/file",
      "line": 123,
      "summary": "one-line description",
      "detail": "why it is a problem and how to fix it"
    }}
  ],
  "verdict": "pass" | "changes-requested"
}}

Here is the unified diff to review:

```diff
{diff}
```
"""


@dataclass
class Finding:
    severity: str
    category: str
    file: str
    line: Optional[int]
    summary: str
    detail: str

    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 0)


@dataclass
class ReviewResult:
    findings: List[Finding] = field(default_factory=list)
    verdict: str = "pass"
    parsed: bool = True  # False when the model output could not be parsed at all.

    def at_or_above(self, threshold: str) -> List[Finding]:
        """Findings whose severity is >= the configured threshold."""
        floor = SEVERITY_ORDER.get(threshold, SEVERITY_ORDER["warn"])
        return [f for f in self.findings if f.rank() >= floor]


def build_prompt(diff: str, cfg: Config) -> str:
    """Construct the reviewer prompt embedding the (already-redacted) diff."""
    return _PROMPT_TEMPLATE.format(diff=diff)


def extract_json_object(text: str) -> Optional[str]:
    """Return the first balanced top-level ``{...}`` JSON object substring.

    Brace matching is string/escape aware so braces inside string literals don't
    throw off the balance count.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        # Unbalanced from this start; try the next "{".
        start = text.find("{", start + 1)
    return None


def _coerce_line(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_finding(raw: Dict[str, Any]) -> Optional[Finding]:
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity", "")).strip().lower()
    if severity not in SEVERITY_ORDER:
        # Unknown/blank severity: default to the middle so it isn't silently lost.
        severity = "warn"
    category = str(raw.get("category", "")).strip().lower()
    if category not in VALID_CATEGORIES:
        category = "logic"
    summary = str(raw.get("summary", "")).strip() or "(no summary provided)"
    detail = str(raw.get("detail", "")).strip()
    file = str(raw.get("file", "")).strip() or "?"
    return Finding(
        severity=severity,
        category=category,
        file=file,
        line=_coerce_line(raw.get("line")),
        summary=summary,
        detail=detail,
    )


def parse(raw_text: str) -> ReviewResult:
    """Parse reviewer output into a ReviewResult.

    A result that cannot be parsed at all is marked ``parsed=False`` so the
    orchestrator can fail-open rather than block on garbage.
    """
    blob = extract_json_object(raw_text or "")
    if not blob:
        return ReviewResult(parsed=False)
    try:
        data = json.loads(blob)
    except ValueError:
        return ReviewResult(parsed=False)
    if not isinstance(data, dict):
        return ReviewResult(parsed=False)

    findings: List[Finding] = []
    for item in data.get("findings", []) or []:
        finding = _normalize_finding(item)
        if finding is not None:
            findings.append(finding)

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "changes-requested"):
        verdict = "changes-requested" if findings else "pass"

    return ReviewResult(findings=findings, verdict=verdict, parsed=True)
