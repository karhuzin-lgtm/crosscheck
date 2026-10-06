"""crosscheck — an independent second model reviews AI-written diffs.

Two ways in: a Claude Code Stop-hook plugin (block the agent's turn until the
issues are fixed) and a standalone ``crosscheck`` CLI that reviews any git diff —
uncommitted, staged, a branch, or stdin — from any AI tool. One reviewer, or a
jury of independent models whose agreement is shown per finding.

Local, cross-model, fail-open. Standard library only.
"""

__version__ = "0.2.0"
