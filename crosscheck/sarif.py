"""SARIF 2.1.0 export, so findings show up in GitHub code scanning and IDEs.

    crosscheck --base main --sarif > crosscheck.sarif
    # then upload with github/codeql-action/upload-sarif

One rule per category (bug, security, perf, logic, style). Severity maps to the
SARIF level: blocker -> error, warn -> warning, nit -> note. Each result keeps
the jurors that raised it under ``properties.reviewers``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from . import __version__
from .review import VALID_CATEGORIES, Finding

_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_LEVEL = {"blocker": "error", "warn": "warning", "nit": "note"}
_RULE_TEXT = {
    "bug": "Correctness bug introduced by the change",
    "security": "Security issue introduced by the change",
    "perf": "Performance or resource regression introduced by the change",
    "logic": "Broken or questionable logic introduced by the change",
    "style": "Maintainability or style issue introduced by the change",
}


def _uri(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path or "unknown"


def build(findings: Iterable[Finding], blocking_ids: Iterable[int] = ()) -> Dict[str, Any]:
    blocking = set(blocking_ids)
    rules = [
        {
            "id": "crosscheck/%s" % cat,
            "name": cat.capitalize(),
            "shortDescription": {"text": _RULE_TEXT[cat]},
            "helpUri": "https://github.com/karhuzin-lgtm/crosscheck#readme",
        }
        for cat in sorted(VALID_CATEGORIES)
    ]
    results: List[Dict[str, Any]] = []
    for f in findings:
        text = f.summary + ("\n\n" + f.detail if f.detail else "")
        if len(f.reviewers) > 1:
            text += "\n\nFlagged independently by: %s." % ", ".join(f.reviewers)
        location: Dict[str, Any] = {"artifactLocation": {"uri": _uri(f.file)}}
        if f.line and f.line > 0:
            location["region"] = {"startLine": f.line}
        results.append({
            "ruleId": "crosscheck/%s" % f.category,
            "level": _LEVEL.get(f.severity, "warning"),
            "message": {"text": text},
            "locations": [{"physicalLocation": location}],
            "properties": {
                "severity": f.severity,
                "reviewers": list(f.reviewers),
                "blocking": id(f) in blocking,
            },
        })
    return {
        "$schema": _SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "crosscheck",
                "version": __version__,
                "informationUri": "https://github.com/karhuzin-lgtm/crosscheck",
                "rules": rules,
            }},
            "results": results,
        }],
    }
