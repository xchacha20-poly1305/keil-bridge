"""Mapping Keil (armcc / armclang) settings to GCC options and flags."""

import shlex

from .armasm_startup import StartupInfo
from .diagnostics import Diagnostics
from .options import BuildOptions
from .uvproj import KeilProject

CORES = {
    "Cortex-M0": "cortex-m0",
    "Cortex-M0+": "cortex-m0plus",
    "Cortex-M1": "cortex-m1",
    "Cortex-M3": "cortex-m3",
    "Cortex-M4": "cortex-m4",
    "Cortex-M7": "cortex-m7",
    "Cortex-M23": "cortex-m23",
    "Cortex-M33": "cortex-m33",
}

# (core, Keil FPU field) -> GCC -mfpu
FPUS = {
    ("cortex-m4", "FPU2"): "fpv4-sp-d16",
    ("cortex-m7", "FPU3(SFPU)"): "fpv5-sp-d16",
    ("cortex-m7", "FPU3(DFPU)"): "fpv5-d16",
    ("cortex-m33", "FPU3(SFPU)"): "fpv5-sp-d16",
}

AC5_OPTIMIZE = {"0": "-O2", "1": "-O0", "2": "-O1", "3": "-O2", "4": "-O3"}
AC6_OPTIMIZE = {"0": "-O0", "1": "-O0", "2": "-O1", "3": "-O2", "4": "-O3", "5": "-Ofast", "6": "-Os", "7": "-Oz"}
AC6_LANGUAGE = {"0": "gnu11", "1": "c90", "2": "gnu90", "3": "c99", "4": "gnu99", "5": "c11", "6": "gnu11"}
WARNING_LEVELS = {"1": "none", "2": "all"}

# armcc command-line options that have no effect worth carrying over.
HARMLESS_ARMCC_FLAGS = {"--gnu", "--no_multibyte_chars", "--diag_suppress", "--split_sections"}


def from_keil(project: KeilProject, startup: StartupInfo | None, sources: list[str], diag: Diagnostics) -> BuildOptions:
    """Build options equivalent to the Keil project settings.

    Problems are reported with the option they concern, so that an override
    of that option silences them.
    """
    where = project.uvprojx.name
    cads = project.cads
    options = BuildOptions(
        defines=list(project.defines),
        undefines=list(project.undefines),
        includes=list(project.includes),
        sources=sources,
    )

    core = CORES.get(project.cpu_type)
    if core is None:
        diag.error(f"CPU {project.cpu_type!r} is not supported", where, option="cpu")
    else:
        options.cpu = core
    if project.fpu and project.fpu_used and core is not None:
        fpu = FPUS.get((core, project.fpu))
        if fpu is None:
            diag.error(f"FPU {project.fpu} on {project.cpu_type} is not supported", where, option="fpu")
        else:
            options.fpu = fpu
            options.float_abi = "hard"

    if project.rom is not None:
        options.flash_origin, options.flash_size = project.rom.origin, project.rom.size
    if project.ram is not None:
        options.ram_origin, options.ram_size = project.ram.origin, project.ram.size
    if startup is not None:
        if startup.stack_size is not None:
            options.stack_size = startup.stack_size
        if startup.heap_size is not None:
            options.heap_size = startup.heap_size

    optim = cads.get("Optim", "0")
    if project.toolset == "ac6":
        options.c_std = AC6_LANGUAGE.get(cads.get("v6Lang", "0"), "gnu11")
        options.optimize = AC6_OPTIMIZE.get(optim, "-O0")
        if optim not in AC6_OPTIMIZE:
            diag.warn(f"unknown optimization level Optim={optim}", where, option="optimize")
    else:
        options.c_std = "gnu99" if cads.get("uC99") == "1" else "gnu90"
        options.optimize = AC5_OPTIMIZE.get(optim, "-O2")
        if optim not in AC5_OPTIMIZE:
            diag.warn(f"unknown optimization level Optim={optim}", where, option="optimize")
        if cads.get("oTime") == "1":
            diag.note("'Optimize for Time' has no GCC equivalent", where, option="optimize")
    options.warnings = WARNING_LEVELS.get(cads.get("wLevel", "0"), "default")
    options.signed_char = cads.get("PlainCh") == "1"
    options.short_enums = cads.get("EnumInt") != "1"

    if cads.get("Strict") == "1":
        diag.warn("'Strict ANSI C' is not converted", where, option="c_std")
    for tag, what in (("Ropi", "read-only position independence"), ("Rwpi", "read-write position independence")):
        if cads.get(tag) == "1":
            diag.error(f"{what} (--{tag.lower()}) is not supported", where)
    if cads.get("useXO") == "1":
        diag.warn("execute-only code is not converted", where, option="extra_cflags")
    _read_c_misc(options, cads.get("MiscControls", ""), diag, where)

    if project.aads.get("MiscControls"):
        diag.warn(
            f"assembler MiscControls {project.aads['MiscControls']!r} not converted", where, option="extra_asflags"
        )
    _read_linker(project.ldads, diag, where)
    return options


def _read_c_misc(options: BuildOptions, misc: str, diag: Diagnostics, where: str):
    for flag in shlex.split(misc, posix=False):
        if flag == "--c99":
            options.c_std = "gnu99"
        elif flag.startswith("-D") and len(flag) > 2:
            options.defines.append(flag[2:])
        elif flag.split("=")[0] in HARMLESS_ARMCC_FLAGS:
            diag.note(f"C compiler flag {flag!r} dropped", where)
        else:
            diag.warn(f"C compiler flag {flag!r} in MiscControls not converted", where, option="extra_cflags")


def _read_linker(ldads: dict[str, str], diag: Diagnostics, where: str):
    if ldads.get("umfTarg", "1") != "1":
        scatter = ldads.get("ScatterFile")
        if scatter:
            diag.warn(
                f"scatter file {scatter!r} is ignored; the linker script is generated from IROM1/IRAM1",
                where,
                option="linker_script",
            )
        else:
            diag.warn(
                "custom R/O and R/W base addresses are ignored; the linker script is generated from IROM1/IRAM1",
                where,
                option="linker_script",
            )
    for tag in ("IncludeLibs", "IncludeLibsPath"):
        if ldads.get(tag):
            diag.warn(f"linker {tag} {ldads[tag]!r} not converted", where, option="extra_ldflags")
    if ldads.get("noStLib") == "1":
        diag.warn("'Don't search Standard Libraries' is not converted", where, option="libc_specs")
    flags = shlex.split(ldads.get("Misc", ""), posix=False)
    while flags:
        flag = flags.pop(0)
        if flag == "--libpath" and flags:
            diag.note(f"linker flag '--libpath {flags.pop(0)}' dropped (armlink library path)", where)
        elif flag.startswith("--libpath="):
            diag.note(f"linker flag {flag!r} dropped (armlink library path)", where)
        else:
            diag.warn(f"linker flag {flag!r} not converted", where, option="extra_ldflags")


def cpu_flags(options: BuildOptions) -> list[str]:
    flags = [f"-mcpu={options.cpu}", "-mthumb"]
    if options.fpu != "none":
        flags.append(f"-mfpu={options.fpu}")
    flags.append(f"-mfloat-abi={options.float_abi}")
    return flags


def language_flags(options: BuildOptions) -> list[str]:
    """C flags derived from the options, except standard, optimization and
    the user's extra flags, which the Makefile keeps overridable."""
    flags = ["-fsigned-char" if options.signed_char else "-funsigned-char"]
    flags.append("-fshort-enums" if options.short_enums else "-fno-short-enums")
    flags += {"default": [], "none": ["-w"], "all": ["-Wall"]}[options.warnings]
    return flags
