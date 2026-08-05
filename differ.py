"""Unified diffs between audit artifacts.

`diff_strings` is the whole module — `diff_snapshot` reads two artifacts
through the project's own ReportBuilder and diffs their contents. The
path-taking `diff_files`/`snapshot_diff` pair that used to sit here had no
callers and invited exactly the mistake this codebase keeps fixing: resolving
an artifact path outside the builder that owns the project's audit directory.
"""
import difflib


def diff_strings(before: str, after: str, label_a: str = "before", label_b: str = "after") -> str:
    """Return a unified diff between two strings."""
    lines_a = before.splitlines(keepends=True)
    lines_b = after.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        lines_a, lines_b, fromfile=label_a, tofile=label_b, lineterm=""))
    if not diff:
        return "No differences found."
    return "\n".join(diff)
