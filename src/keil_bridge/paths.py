"""Resolving Windows-style paths from Keil files on a case-sensitive file system."""

import os
from pathlib import Path


def is_absolute_windows(text: str) -> bool:
    return (len(text) >= 2 and text[1] == ":") or text.startswith(("\\", "/"))


def split_windows(text: str) -> list[str]:
    return [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]


def resolve_nocase(base: Path, parts: list[str]) -> tuple[Path | None, bool]:
    """Walk `parts` from `base`, matching each component ignoring case when
    there is no exact match.

    Returns (resolved path or None if missing, whether any component needed a
    case-insensitive match).
    """
    current = base
    case_differs = False
    for part in parts:
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if candidate.exists():
            current = candidate
            continue
        match = _find_nocase(current, part)
        if match is None:
            return None, case_differs
        current = match
        case_differs = True
    return current, case_differs


def _find_nocase(directory: Path, name: str) -> Path | None:
    if not directory.is_dir():
        return None
    lowered = name.lower()
    for entry in sorted(os.listdir(directory)):
        if entry.lower() == lowered:
            return directory / entry
    return None


def common_ancestor(paths: list[Path]) -> Path:
    return Path(os.path.commonpath([str(p) for p in paths]))


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
