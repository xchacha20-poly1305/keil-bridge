"""Checks on the converted sources.

Two passes:

* include_shims: `#include` names that only match a file when ignoring case
  (Windows does not care, Linux does). For each one a forwarding header is
  generated instead of editing the source, so nothing needs backporting.
* check_sources: runs the GCC preprocessor on every C source, so that code in
  `#if defined(__CC_ARM)` branches is skipped, and looks for armcc-only
  constructs in what remains.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import Diagnostics
from .paths import rel

SHIM_DIR = "keil-bridge/include-shims"

# armcc keywords and intrinsics GCC does not know. Those with a direct
# equivalent are mapped in keil_compat.h and never reach this check.
ARMCC_ONLY = {
    "__packed": "armcc '__packed' qualifier; use __attribute__((packed)) on the type",
    "__svc": "armcc '__svc' function qualifier",
    "__svc_indirect": "armcc '__svc_indirect' function qualifier",
    "__value_in_regs": "armcc '__value_in_regs' qualifier",
    "__softfp": "armcc '__softfp' qualifier",
    "__pure": "armcc '__pure' qualifier",
    "__global_reg": "armcc '__global_reg' storage class",
    "__nop": "armcc intrinsic __nop(); CMSIS provides __NOP()",
    "__wfi": "armcc intrinsic __wfi(); CMSIS provides __WFI()",
    "__wfe": "armcc intrinsic __wfe(); CMSIS provides __WFE()",
    "__sev": "armcc intrinsic __sev(); CMSIS provides __SEV()",
    "__isb": "armcc intrinsic __isb(); CMSIS provides __ISB()",
    "__dsb": "armcc intrinsic __dsb(); CMSIS provides __DSB()",
    "__dmb": "armcc intrinsic __dmb(); CMSIS provides __DMB()",
    "__rev": "armcc intrinsic __rev(); CMSIS provides __REV()",
    "__clz": "armcc intrinsic __clz(); use __builtin_clz()",
    "__ldrex": "armcc intrinsic __ldrex(); CMSIS provides __LDREXW()",
    "__strex": "armcc intrinsic __strex(); CMSIS provides __STREXW()",
    "__breakpoint": "armcc intrinsic __breakpoint(); CMSIS provides __BKPT()",
    "__current_pc": "armcc intrinsic __current_pc()",
    "__current_sp": "armcc intrinsic __current_sp()",
    "__return_address": "armcc intrinsic __return_address(); use __builtin_return_address(0)",
    "__schedule_barrier": "armcc intrinsic __schedule_barrier()",
}
GNU_ASM_FOLLOWERS = {"(", "volatile", "__volatile__", "__volatile", "goto", "inline"}


# ---------------------------------------------------------------- comments


def strip_comments(text: str) -> str:
    """Replace C comments with spaces, keeping line breaks and string literals."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:end]))
            i = end
        elif c in "\"'":
            end = _literal_end(text, i)
            out.append(text[i:end])
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _literal_end(text: str, start: int) -> int:
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == quote or text[i] == "\n":
            return i + 1
        else:
            i += 1
    return len(text)


# ---------------------------------------------------------------- includes


@dataclass
class Include:
    line: int
    name: str
    angle: bool


def includes(text: str) -> list[Include]:
    result = []
    for number, line in enumerate(strip_comments(text).splitlines(), start=1):
        rest = line.lstrip()
        if not rest.startswith("#"):
            continue
        rest = rest[1:].lstrip()
        if not rest.startswith("include"):
            continue
        rest = rest[len("include") :].lstrip()
        if rest.startswith('"'):
            end = rest.find('"', 1)
            angle = False
        elif rest.startswith("<"):
            end = rest.find(">", 1)
            angle = True
        else:
            continue  # computed include, e.g. #include HEADER
        if end > 0:
            result.append(Include(number, rest[1:end], angle))
    return result


def include_shims(tree: Path, files: list[str], include_dirs: list[str], diag: Diagnostics) -> dict[str, str]:
    """Forwarding headers for includes that only resolve ignoring case.

    Returns {shim path relative to tree: content}.
    """
    shims: dict[str, str] = {}
    targets: dict[str, str] = {}
    for path in files:
        if not path.lower().endswith((".c", ".h", ".cpp", ".hpp", ".inc")):
            continue
        text = (tree / path).read_text(encoding="utf-8")
        own_dir = str(Path(path).parent)
        for inc in includes(text):
            if "\\" in inc.name:
                diag.warn(f'#include "{inc.name}" uses a backslash; GCC on Linux needs "/"', path, inc.line)
                continue
            search = include_dirs if inc.angle else [own_dir, *include_dirs]
            if any((tree / d / inc.name).is_file() for d in search):
                continue
            target = _find_nocase(tree, search, inc.name)
            if target is None:
                continue  # a system header, or genuinely missing: the compiler will say
            shim = f"{SHIM_DIR}/{inc.name}"
            if targets.get(shim, target) != target:
                diag.warn(
                    f'"{inc.name}" matches both {targets[shim]} and {target} ignoring case; using {targets[shim]}',
                    path,
                    inc.line,
                )
                continue
            if shim not in shims:
                diag.note(f'#include "{inc.name}" matches {target} only ignoring case; added {shim}', path, inc.line)
                relative = os.path.relpath(tree / target, (tree / shim).parent)
                shims[shim] = (
                    f'/* Generated by keil-bridge: sources include "{inc.name}", the file is {target}. */\n'
                    f'#include "{Path(relative).as_posix()}"\n'
                )
                targets[shim] = target
    return shims


def _find_nocase(tree: Path, search: list[str], name: str) -> str | None:
    parts = name.split("/")
    for directory in search:
        found = _walk_nocase(tree / directory, parts)
        if found is not None and found.is_file():
            return rel(found.resolve(), tree.resolve())
    return None


def _walk_nocase(current: Path, parts: list[str]) -> Path | None:
    for part in parts:
        if part == "..":
            current = current.parent
            continue
        if not current.is_dir():
            return None
        match = next((e for e in sorted(os.listdir(current)) if e.lower() == part.lower()), None)
        if match is None:
            return None
        current = current / match
    return current


# ---------------------------------------------------------------- preprocessed check


@dataclass
class ScanResult:
    fputc_defined: bool = False
    checked: bool = False


@dataclass
class _Token:
    text: str
    path: str
    line: int


def check_sources(
    tree: Path, sources: list[str], preprocess_args: list[str], compiler: str, diag: Diagnostics
) -> ScanResult:
    """Preprocess each C source with GCC and look for armcc-only code."""
    result = ScanResult()
    if shutil.which(compiler) is None:
        diag.warn(f"{compiler} not found; skipped checking sources for armcc-only code")
        return result
    result.checked = True
    reported: set[tuple[str, int, str]] = set()
    for source in sources:
        if not source.endswith(".c"):
            continue
        proc = subprocess.run(
            [compiler, "-E", *preprocess_args, source],
            cwd=tree,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            message = next((line for line in proc.stderr.splitlines() if "error" in line), proc.stderr.strip())
            diag.warn(f"preprocessing failed: {message}", source)
            continue
        tokens = _tokens(proc.stdout)
        for token, report, message in _armcc_constructs(tokens, diag):
            key = (token.path, token.line, message)
            if key not in reported and not Path(token.path).is_absolute() and not token.path.startswith("<"):
                reported.add(key)
                report(message, token.path, token.line)
        if _defines_fputc(tokens):
            result.fputc_defined = True
            diag.note(
                "defines fputc() to retarget printf (Keil style); "
                "keil-bridge/syscalls.c routes newlib output through it",
                source,
            )
    return result


def _tokens(preprocessed: str) -> list[_Token]:
    """Tokens of preprocessor output, tracking `# <line> "<file>"` markers."""
    tokens: list[_Token] = []
    path = ""
    line = 0
    for raw in preprocessed.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("# ") and stripped[2:3].isdigit():
            number, _, rest = stripped[2:].partition(" ")
            line = int(number)
            path = os.path.normpath(rest.split('"')[1]) if '"' in rest else path
            continue
        if stripped.startswith("#pragma"):
            words = stripped[len("#pragma") :].replace("(", " ").split()
            if words and words[0] == "import":
                tokens.append(_Token("#pragma import", path, line))
        else:
            tokens.extend(_Token(t, path, line) for t in _split_tokens(raw))
        line += 1
    return tokens


def _split_tokens(line: str) -> list[str]:
    result = []
    i = 0
    while i < len(line):
        c = line[i]
        if c.isspace():
            i += 1
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < len(line) and (line[j].isalnum() or line[j] == "_"):
                j += 1
            result.append(line[i:j])
            i = j
        elif c in "\"'":
            j = _literal_end(line, i)
            result.append(line[i:j])
            i = j
        else:
            result.append(c)
            i += 1
    return result


def _armcc_constructs(tokens: list[_Token], diag: Diagnostics):
    """Yield (token, diag.note or diag.warn, message) for armcc-only code."""
    for index, token in enumerate(tokens):
        if token.text == "#pragma import":
            yield token, diag.note, "'#pragma import' is armcc-only; GCC ignores it"
        elif token.text in ("__asm", "__asm__"):
            following = tokens[index + 1].text if index + 1 < len(tokens) else ""
            if following not in GNU_ASM_FOLLOWERS:
                yield (
                    token,
                    diag.warn,
                    (
                        "armcc embedded assembler is not supported by GCC; "
                        "rewrite it as GCC inline assembly or guard it with #if defined(__CC_ARM)"
                    ),
                )
        elif token.text in ARMCC_ONLY:
            yield token, diag.warn, ARMCC_ONLY[token.text]


def _defines_fputc(tokens: list[_Token]) -> bool:
    """`fputc ( ... ) {` at any position: a definition, not a declaration or call."""
    for index, token in enumerate(tokens):
        if token.text != "fputc" or index + 1 >= len(tokens) or tokens[index + 1].text != "(":
            continue
        depth = 0
        for end in range(index + 1, len(tokens)):
            if tokens[end].text == "(":
                depth += 1
            elif tokens[end].text == ")":
                depth -= 1
                if depth == 0:
                    if end + 1 < len(tokens) and tokens[end + 1].text == "{":
                        return True
                    break
    return False
