"""crosscheck — an independent second model reviews AI-written diffs.

A Claude Code Stop-hook plugin. When an agent finishes a turn that changed code,
crosscheck sends the working-tree diff to a *different* reviewer model and blocks
turn completion if issues at or above a severity threshold are found.

Local, cross-model, fail-open. Standard library only.
"""

__version__ = "0.1.0"
