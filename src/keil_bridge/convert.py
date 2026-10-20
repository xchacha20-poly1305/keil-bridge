"""`convert` and `regen`: building and refreshing a converted tree."""

import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from . import generate, options as opts, textcodec, uvproj
from .diagnostics import Diagnostics, KeilBridgeError
from .manifest import OVERRIDES_PATH, FileRecord, Manifest, sha256
from .paths import rel

SOURCE_SUFFIXES = {".c", ".h", ".s", ".cpp", ".hpp", ".inc"}
SKIPPED_DIRS = {".git", ".svn", ".hg"}


def convert(
    uvprojx: Path,
    outdir: Path,
    target: str | None,
    config: Path | None,
    sets: list[str],
    use_git: bool,
    diag: Diagnostics,
):
    outdir = outdir.resolve()
    if outdir.exists() and (not outdir.is_dir() or any(outdir.iterdir())):
        diag.fatal(f"{outdir} already exists and is not empty")
    project = uvproj.load(uvprojx, target, diag)
    if outdir.is_relative_to(project.root) or project.root.is_relative_to(outdir):
        diag.error(f"output directory {outdir} must be outside the Keil project root {project.root}")
    overrides = _collect_overrides(
        opts.load_toml(config, diag) if config else {}, sets, diag, str(config) if config else "--set"
    )
    diag.check()

    outdir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{outdir.name}.", dir=outdir.parent))
    try:
        manifest = import_tree(project, staging, diag)
        diag.check()
        generate.regenerate(staging, project, manifest, overrides, diag, final_tree=outdir)
        manifest.save(staging)
        (staging / OVERRIDES_PATH).write_text(opts.dump_toml(overrides), encoding="utf-8")
        (staging / ".editorconfig").write_text(generate.template("editorconfig"), encoding="utf-8")
        (staging / ".gitignore").write_text(generate.template("gitignore"), encoding="utf-8")
        if outdir.exists():
            outdir.rmdir()
        staging.rename(outdir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if use_git:
        _git_init(outdir, project, diag)


def regen(tree: Path, sets: list[str], unsets: list[str], diag: Diagnostics):
    tree = tree.resolve()
    manifest = _load_manifest(tree, diag)
    project = uvproj.load(manifest.keil_root_path / manifest.uvprojx, manifest.target, diag)
    if project.root != manifest.keil_root_path:
        diag.error(
            f"the Keil project root moved from {manifest.keil_root} to {project.root} "
            "(an include directory or source outside the old root?); convert the project again"
        )
    toml_path = tree / OVERRIDES_PATH
    overrides = _collect_overrides(opts.load_toml(toml_path, diag), sets, diag, OVERRIDES_PATH)
    for key in unsets:
        if overrides.pop(key, None) is None:
            diag.warn(f"--unset {key}: not set in {OVERRIDES_PATH}")
    diag.check()
    generate.regenerate(tree, project, manifest, overrides, diag, final_tree=tree)
    if sets or unsets:
        toml_path.write_text(opts.dump_toml(overrides), encoding="utf-8")


def _load_manifest(tree: Path, diag: Diagnostics) -> Manifest:
    try:
        return Manifest.load(tree)
    except (OSError, ValueError) as e:
        diag.error(f"{tree} is not a keil-bridge tree: {e}")
        raise KeilBridgeError(str(e)) from None


def _collect_overrides(base: dict, sets: list[str], diag: Diagnostics, where: str) -> dict:
    overrides = opts.validate(base, diag, where)
    return overrides | opts.validate(opts.parse_set(sets, diag), diag, "--set")


def import_tree(project: uvproj.KeilProject, tree: Path, diag: Diagnostics) -> Manifest:
    """Copy the project's source files into `tree` as UTF-8 + LF."""
    root = project.root
    used = {s.path for s in project.sources}
    manifest = Manifest(keil_root=str(root), uvprojx=project.uvprojx_rel, target=project.target, default_eol="crlf")
    eols: Counter[str] = Counter()
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for filename in sorted(filenames):
            source = Path(directory) / filename
            path = rel(source, root)
            if source.is_symlink() or source.suffix.lower() not in SOURCE_SUFFIXES:
                manifest.skipped.append(path)
                continue
            data = source.read_bytes()
            try:
                fmt = textcodec.detect(data)
            except textcodec.EncodingError as e:
                if path in used:
                    diag.error(f"cannot convert: {e}", path)
                else:
                    diag.warn(f"not copied: {e}", path)
                    manifest.skipped.append(path)
                continue
            converted = textcodec.to_converted(data, fmt)
            if fmt.mixed_eol:
                diag.warn("mixed CRLF and LF line endings; backport will write CRLF on every line", path)
            if b"\r" in converted:
                diag.warn("contains a carriage return that is not part of CRLF; kept as is", path)
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(converted)
            manifest.files[path] = FileRecord(fmt.encoding, fmt.eol, sha256(data), sha256(converted))
            if fmt.eol != "none":
                eols[fmt.eol] += 1
    if eols:
        manifest.default_eol = eols.most_common(1)[0][0]
    return manifest


def _git_init(tree: Path, project: uvproj.KeilProject, diag: Diagnostics):
    if shutil.which("git") is None:
        diag.note("git not found; the tree is not under version control")
        return
    message = f"Convert {project.uvprojx_rel} (target {project.target}) with keil-bridge"
    for command in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", message]):
        proc = subprocess.run(command, cwd=tree, capture_output=True, text=True)
        if proc.returncode != 0:
            diag.warn(f"'{' '.join(command[:2])}' failed: {proc.stderr.strip()}")
            return
