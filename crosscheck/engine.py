"""Review engine shared by the Stop hook and the standalone CLI.

Takes an already-computed diff and runs it past one reviewer — or, in jury mode,
several independent reviewers in parallel — and returns merged findings.

Jury mode: each juror reviews the same redacted diff on its own. Findings that
point at the same place and describe the same problem are merged, and the merged
finding records every juror that raised it. Agreement between models from
different vendors is a strong signal; ``quorum`` decides how many jurors must
agree before a finding blocks (default 1: any juror can block).

The same trust rules as the single-reviewer path apply to every juror: the diff is
redacted before it leaves, each juror's output is redacted before it is used, and
un-sandboxed providers still need the explicit env opt-in (``providers.resolve``).
"""

from __future__ import annotations

import dataclasses
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import providers, redact, review
from .config import Config
from .providers import ProviderError, Reviewer
from .review import Finding

Notify = Callable[[str], None]

# Two findings on lines this close are treated as "the same place".
_LINE_SLACK = 3
# Minimum word-overlap (Jaccard) for two summaries to count as the same issue.
_SIMILARITY = 0.25
_WORD = re.compile(r"[a-z0-9_]{3,}")


@dataclass
class Verdict:
    """Outcome of one review (single reviewer or a whole jury)."""

    reviewers: List[str] = field(default_factory=list)  # jurors that answered
    failures: List[str] = field(default_factory=list)  # redacted error per failed juror
    findings: List[Finding] = field(default_factory=list)
    same_vendor: bool = False

    @property
    def jury_size(self) -> int:
        return len(self.reviewers)

    def blocking(self, threshold: str, quorum: int = 1) -> List[Finding]:
        """Findings at/above ``threshold`` raised by at least ``quorum`` jurors.

        The quorum is capped at the number of jurors that actually answered, so a
        juror that crashed can't make the rest unable to block.
        """
        need = max(1, min(quorum, self.jury_size or 1))
        floor = review.SEVERITY_ORDER.get(threshold, review.SEVERITY_ORDER["warn"])
        return [
            f for f in self.findings if f.rank() >= floor and len(f.reviewers) >= need
        ]


def juror_names(cfg: Config) -> List[str]:
    """The provider names to run: the jury if one is configured, else ``provider``."""
    return list(cfg.jury) if cfg.jury else [cfg.provider or "auto"]


def resolve_jury(cfg: Config, notify: Notify) -> Tuple[List[Reviewer], List[str]]:
    """Resolve every juror. Returns (reviewers, failure messages).

    Duplicates are dropped by resolved name, so ``auto,codex`` runs codex once.
    """
    reviewers: List[Reviewer] = []
    failures: List[str] = []
    seen = set()
    for name in juror_names(cfg):
        try:
            rev = providers.resolve(dataclasses.replace(cfg, provider=name))
        except ProviderError as exc:
            failures.append("%s: %s" % (name, exc))
            continue
        if rev.name in seen:
            continue
        seen.add(rev.name)
        reviewers.append(rev)
    return reviewers, failures


def _run_one(rev: Reviewer, prompt: str, cfg: Config, notify: Notify) -> List[Finding]:
    """Run one juror and return its parsed findings. Raises ProviderError."""
    raw = providers.run(rev, prompt, cfg.timeout_sec)
    # The reviewer is not filesystem-sandboxed, so a prompt injection could
    # induce it to surface a local secret. Redact its OUTPUT before use.
    raw, n_out = redact.redact(raw)
    if n_out:
        notify(
            "redacted %d secret(s) from %s's OUTPUT before use." % (n_out, rev.display)
        )
    result = review.parse(raw)
    if not result.parsed:
        raise ProviderError(
            "reviewer '%s' output could not be parsed as findings JSON" % rev.display
        )
    for f in result.findings:
        f.reviewers = [rev.display]
    return result.findings


def run_review(diff_text: str, cfg: Config, notify: Notify) -> Verdict:
    """Review ``diff_text`` with the configured reviewer(s).

    Raises ProviderError only when NO juror produced a usable answer; partial
    jury failures are reported in ``Verdict.failures`` instead.
    """
    redacted, n_in = redact.redact(diff_text)
    if n_in:
        notify("redacted %d secret(s) from the diff before review." % n_in)

    reviewers, failures = resolve_jury(cfg, notify)
    if not reviewers:
        raise ProviderError("; ".join(failures) or "no reviewer available")

    # Be honest about isolation. codex --sandbox read-only blocks WRITES only;
    # gemini/claude/command (reachable only behind an explicit env opt-in) have
    # not even that. NO reviewer is isolated from filesystem READS.
    for rev in reviewers:
        if not rev.sandboxed:
            notify(
                "reviewer '%s' has no write-sandbox (codex uses --sandbox read-only, "
                "which blocks writes). No reviewer is isolated from filesystem reads: "
                "a prompt-injecting diff could induce it to read local files. "
                "Redaction of the diff and the output is the mitigation." % rev.display
            )

    prompt = review.build_prompt(redacted, cfg)
    results: Dict[str, List[Finding]] = {}
    errors: Dict[str, str] = {}

    def work(rev: Reviewer) -> None:
        try:
            results[rev.display] = _run_one(rev, prompt, cfg, notify)
        except ProviderError as exc:
            errors[rev.display] = str(exc)
        except Exception as exc:  # noqa: BLE001 - one juror must not sink the rest
            errors[rev.display] = "reviewer '%s' crashed: %s" % (rev.display, exc)

    if len(reviewers) == 1:
        work(reviewers[0])
    else:
        threads = [threading.Thread(target=work, args=(r,), daemon=True) for r in reviewers]
        for t in threads:
            t.start()
        for t in threads:
            # providers.run enforces its own timeout; this is a safety net.
            t.join(timeout=cfg.timeout_sec + 30)

    for rev in reviewers:
        if rev.display not in results and rev.display not in errors:
            errors[rev.display] = "reviewer '%s' did not finish" % rev.display
    for name, msg in errors.items():
        failures.append(redact.redact(msg)[0])

    answered = [r for r in reviewers if r.display in results]
    if not answered:
        raise ProviderError("; ".join(failures) or "no reviewer produced a result")

    return Verdict(
        reviewers=[r.display for r in answered],
        failures=failures,
        findings=merge([(r.display, results[r.display]) for r in answered]),
        same_vendor=any(r.warn_same_vendor for r in answered),
    )


def _words(text: str) -> set:
    return set(_WORD.findall(text.lower()))


def _similar(a: Finding, b: Finding) -> bool:
    wa, wb = _words(a.summary + " " + a.detail), _words(b.summary + " " + b.detail)
    if not wa or not wb:
        return False
    return len(wa & wb) / float(len(wa | wb)) >= _SIMILARITY


def _norm_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    for prefix in ("./", "a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return path


def _same_issue(a: Finding, b: Finding) -> bool:
    if _norm_path(a.file) != _norm_path(b.file):
        return False
    if a.line is not None and b.line is not None:
        if abs(a.line - b.line) > _LINE_SLACK:
            return False
        # Same spot: same category or a similar description is enough.
        return a.category == b.category or _similar(a, b)
    # Missing line on either side: only the description can tie them together.
    return _similar(a, b)


def merge(per_reviewer: List[Tuple[str, List[Finding]]]) -> List[Finding]:
    """Merge findings across jurors; most-agreed, most-severe first.

    A merged finding keeps the wording of its most severe report, takes the
    highest severity any juror gave it, and lists every juror that raised it.
    A juror is never merged with itself.
    """
    merged: List[Finding] = []
    for name, findings in per_reviewer:
        for f in findings:
            target: Optional[Finding] = None
            for m in merged:
                if name not in m.reviewers and _same_issue(m, f):
                    target = m
                    break
            if target is None:
                merged.append(dataclasses.replace(f, reviewers=[name]))
                continue
            target.reviewers.append(name)
            if f.rank() > target.rank():
                target.severity, target.summary, target.detail = f.severity, f.summary, f.detail
                target.category = f.category
            if target.line is None and f.line is not None:
                target.line = f.line
    merged.sort(key=lambda m: (-len(m.reviewers), -m.rank(), m.file, m.line or 0))
    return merged
