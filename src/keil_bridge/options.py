"""GCC build options, and overriding them from keil-bridge.toml or --set.

Defaults come from the Keil project (toolchain.from_keil). Overrides are a
flat mapping of option name to value. Every list option X also accepts
X_add and X_remove, which edit the default list instead of replacing it.
"""

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .diagnostics import Diagnostics

C_STANDARDS = {"c89", "c90", "c99", "c11", "c17", "c23", "gnu89", "gnu90", "gnu99", "gnu11", "gnu17", "gnu23"}
OPTIMIZE_LEVELS = {"-O0", "-O1", "-O2", "-O3", "-Os", "-Og", "-Oz", "-Ofast"}
FLOAT_ABIS = {"soft", "softfp", "hard"}
WARNING_LEVELS = {"default", "none", "all"}


@dataclass
class BuildOptions:
    c_std: str = "gnu99"
    optimize: str = "-O2"
    warnings: str = "default"
    defines: list[str] = field(default_factory=list)
    undefines: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    cpu: str = ""
    fpu: str = "none"
    float_abi: str = "soft"
    signed_char: bool = False
    short_enums: bool = True
    flash_origin: int = 0
    flash_size: int = 0
    ram_origin: int = 0
    ram_size: int = 0
    stack_size: int = 0x400
    heap_size: int = 0x200
    startup: str = ""  # armasm startup file to translate; empty: none (a source provides the vectors)
    linker_script: str = ""  # empty: generate one from the memory layout
    extra_cflags: list[str] = field(default_factory=list)
    extra_asflags: list[str] = field(default_factory=list)
    extra_ldflags: list[str] = field(default_factory=list)
    libc_specs: list[str] = field(default_factory=lambda: ["nano.specs", "nosys.specs"])


CHOICES = {
    "c_std": C_STANDARDS,
    "optimize": OPTIMIZE_LEVELS,
    "float_abi": FLOAT_ABIS,
    "warnings": WARNING_LEVELS,
}

_TYPES = {f.name: f.type for f in fields(BuildOptions)}
_LIST_EDITS = {
    f"{name}{suffix}": name for name, t in _TYPES.items() if t == list[str] for suffix in ("_add", "_remove")
}


def option_names() -> list[str]:
    return list(_TYPES) + list(_LIST_EDITS)


def base_option(key: str) -> str:
    """The BuildOptions field an override key changes."""
    return _LIST_EDITS.get(key, key)


def parse_set(assignments: list[str], diag: Diagnostics) -> dict:
    """Parse --set KEY=VALUE strings into typed override values."""
    result = {}
    for assignment in assignments:
        key, sep, text = assignment.partition("=")
        key = key.strip()
        if not sep:
            diag.error(f"--set {assignment!r}: expected KEY=VALUE")
            continue
        if key not in _TYPES and key not in _LIST_EDITS:
            diag.error(f"--set {key}: unknown option; known options: {', '.join(option_names())}")
            continue
        value = _parse_text(_TYPES[base_option(key)], text.strip())
        if value is None:
            diag.error(f"--set {key}: cannot parse {text!r} as {_type_name(_TYPES[base_option(key)])}")
            continue
        result[key] = value
    return result


def _parse_text(t, text: str):
    if t is str:
        return text
    if t is bool:
        return {"true": True, "false": False, "1": True, "0": False}.get(text.lower())
    if t is int:
        try:
            return int(text, 0)
        except ValueError:
            return None
    return [item.strip() for item in text.split(",") if item.strip()]


def _type_name(t) -> str:
    return {str: "string", bool: "boolean", int: "integer"}.get(t, "list of strings")


def load_toml(path: Path, diag: Diagnostics) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        diag.error(f"invalid TOML: {e}", str(path))
        return {}


def dump_toml(overrides: dict) -> str:
    lines = [
        "# GCC-side overrides of the settings read from the Keil project.",
        "# Edit this file and run `keil-bridge regen`, or use `keil-bridge regen --set KEY=VALUE`.",
        f"# Options: {', '.join(_TYPES)}.",
        "# List options also accept <name>_add and <name>_remove.",
        "",
    ]
    for key in option_names():
        if key in overrides:
            lines.append(f"{key} = {_toml_value(overrides[key])}")
    return "\n".join(lines) + "\n"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return hex(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_string(v) for v in value) + "]"
    return _toml_string(value)


def _toml_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate(overrides: dict, diag: Diagnostics, where: str) -> dict:
    """Drop and report unknown keys and values of the wrong type."""
    valid = {}
    for key, value in overrides.items():
        if key not in _TYPES and key not in _LIST_EDITS:
            diag.error(f"unknown option {key!r}; known options: {', '.join(option_names())}", where)
            continue
        t = _TYPES[base_option(key)]
        if t == list[str]:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = type(value) is t
        if not ok:
            diag.error(f"option {key!r} must be a {_type_name(t)}, got {value!r}", where)
            continue
        if key in CHOICES and value not in CHOICES[key]:
            diag.error(f"option {key!r} must be one of {', '.join(sorted(CHOICES[key]))}, got {value!r}", where)
            continue
        valid[key] = value
    return valid


def apply(defaults: BuildOptions, overrides: dict) -> BuildOptions:
    changes = {}
    for key, value in overrides.items():
        if key in _TYPES:
            changes[key] = value
    result = replace(defaults, **changes)
    for key, value in overrides.items():
        name = _LIST_EDITS.get(key)
        if name is None:
            continue
        current = getattr(result, name)
        if key.endswith("_add"):
            current = current + [v for v in value if v not in current]
        else:
            current = [v for v in current if v not in value]
        setattr(result, name, current)
    return result


def overridden_options(overrides: dict) -> set[str]:
    return {base_option(key) for key in overrides}


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return hex(value)
    if isinstance(value, list):
        return " ".join(value) if value else "(empty)"
    return value or "(empty)"
