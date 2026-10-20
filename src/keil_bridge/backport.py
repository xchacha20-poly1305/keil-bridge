"""`status` and `backport`: comparing the converted tree with the Keil project
and writing edited files back in their original encoding and line endings."""

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from . import textcodec
from .convert import SKIPPED_DIRS, SOURCE_SUFFIXES
from .diagnostics import Diagnostics
from .generate import GENERATED_DIR
from .manifest import Manifest, sha256
from .paths import rel


@dataclass
class FileState:
    path: str
    changed_here: bool  # edited in the converted tree
    changed_in_keil: bool  # edited in the Keil project since the last sync
    missing_here: bool = False
    missing_in_keil: bool = False


def file_states(tree: Path, manifest: Manifest) -> list[FileState]:
    states = []
    keil_root = manifest.keil_root_path
    for path, record in sorted(manifest.files.items()):
        here = tree / path
        keil = keil_root / path
        state = FileState(path, False, False, not here.exists(), not keil.exists())
        state.changed_here = state.missing_here or sha256(here.read_bytes()) != record.conv_sha256
        state.changed_in_keil = state.missing_in_keil or sha256(keil.read_bytes()) != record.orig_sha256
        states.append(state)
    return states


def untracked(tree: Path, manifest: Manifest) -> list[str]:
    """Source files in the converted tree that the Keil project does not have."""
    result = []
    for directory, dirnames, filenames in os.walk(tree):
        if Path(directory) == tree:
            dirnames[:] = [d for d in dirnames if d not in (GENERATED_DIR, "build")]
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for filename in sorted(filenames):
            path = rel(Path(directory) / filename, tree)
            if Path(filename).suffix.lower() in SOURCE_SUFFIXES and path not in manifest.files:
                result.append(path)
    return result


def status(tree: Path) -> list[str]:
    manifest = Manifest.load(tree)
    lines = []
    for state in file_states(tree, manifest):
        if state.missing_here:
            lines.append(f"D  {state.path}  (deleted here)")
        elif state.changed_here and state.changed_in_keil:
            lines.append(f"C  {state.path}  (changed here and in the Keil project)")
        elif state.changed_here:
            lines.append(f"M  {state.path}")
        elif state.changed_in_keil:
            lines.append(f"K  {state.path}  (changed in the Keil project)")
    lines += [f"?  {path}  (not in the Keil project)" for path in untracked(tree, manifest)]
    return lines


def backport(tree: Path, dry_run: bool, force: bool, diag: Diagnostics) -> list[str]:
    """Write changed files back to the Keil project. Returns the written paths
    (or, with dry_run, a unified diff per file)."""
    manifest = Manifest.load(tree)
    keil_root = manifest.keil_root_path
    output = []
    for state in file_states(tree, manifest):
        if not state.changed_here:
            if state.changed_in_keil:
                diag.note("changed only in the Keil project; not imported into this tree", state.path)
            continue
        if state.missing_here:
            diag.warn("deleted in the converted tree; the Keil file is left alone", state.path)
            continue
        if state.changed_in_keil and not force:
            diag.error(
                "changed both here and in the Keil project; merge by hand or use --force to overwrite the Keil version",
                state.path,
            )
            continue
        record = manifest.files[state.path]
        converted = (tree / state.path).read_bytes()
        try:
            data, encoding = textcodec.to_original(converted, record.format, manifest.default_eol)
        except (textcodec.EncodingError, UnicodeDecodeError) as e:
            diag.error(f"not written back: {e}", state.path)
            continue
        if encoding != record.encoding:
            diag.warn(f"now contains non-ASCII text; written back as {encoding.upper()}", state.path)
        keil_file = keil_root / state.path
        if dry_run:
            output.append(_diff(keil_file, data, encoding, state.path))
            continue
        keil_file.write_bytes(data)
        record.encoding = encoding
        record.orig_sha256 = sha256(data)
        record.conv_sha256 = sha256(converted)
        output.append(state.path)
    for path in untracked(tree, manifest):
        diag.warn("new file is not copied to the Keil project (add it there by hand)", path)
    if not dry_run:
        manifest.save(tree)
    return output


def _diff(keil_file: Path, new: bytes, encoding: str, path: str) -> str:
    old = keil_file.read_bytes().decode(encoding, errors="replace") if keil_file.exists() else ""
    diff = difflib.unified_diff(
        old.replace("\r\n", "\n").splitlines(keepends=True),
        new.decode(encoding).replace("\r\n", "\n").splitlines(keepends=True),
        f"keil/{path}",
        f"converted/{path}",
    )
    return "".join(diff)
