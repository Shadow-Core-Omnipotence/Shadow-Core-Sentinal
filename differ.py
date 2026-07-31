import difflib
from pathlib import Path
from typing import Optional


def diff_files(path_a: Path, path_b: Path, label_a: str = "before", label_b: str = "after") -> str:
    """Return a unified diff string between two files."""
    try:
        lines_a = path_a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines_b = path_b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError as e:
        return f"Error reading files for diff: {e}"

    diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=label_a, tofile=label_b, lineterm=""))
    if not diff:
        return "No differences found."
    return "\n".join(diff)


def diff_strings(before: str, after: str, label_a: str = "before", label_b: str = "after") -> str:
    """Return a unified diff between two strings."""
    lines_a = before.splitlines(keepends=True)
    lines_b = after.splitlines(keepends=True)
    diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=label_a, tofile=label_b, lineterm=""))
    if not diff:
        return "No differences found."
    return "\n".join(diff)


def snapshot_diff(snap_a: Path, snap_b: Path) -> str:
    """Diff two audit snapshot markdown files."""
    return diff_files(snap_a, snap_b, label_a=snap_a.name, label_b=snap_b.name)
