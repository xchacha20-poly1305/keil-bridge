"""Generating the GCC build files in a converted tree.

Used by both `convert` (on a freshly imported tree) and `regen` (on an
existing one). Everything under keil-bridge/ except the manifest is rewritten
each time; the root Makefile is only written when missing.
"""

import json
import shlex
import shutil
from dataclasses import fields
from importlib import resources
from pathlib import Path
from string import Template

from . import armasm_startup, options as opts, scan, toolchain
from .diagnostics import Diagnostics, Level
from .manifest import MANIFEST_PATH, Manifest
from .options import BuildOptions
from .uvproj import KeilProject

GENERATED_DIR = "keil-bridge"
COMPAT_HEADER = f"{GENERATED_DIR}/keil_compat.h"
SYSCALLS = f"{GENERATED_DIR}/syscalls.c"
PROJECT_MK = f"{GENERATED_DIR}/project.mk"
REPORT = "CONVERSION.md"
COMPILER = "arm-none-eabi-gcc"


def template(name: str) -> str:
    return resources.files("keil_bridge").joinpath("templates", name).read_text(encoding="utf-8")


def regenerate(
    tree: Path, project: KeilProject, manifest: Manifest, overrides: dict, diag: Diagnostics, *, final_tree: Path
):
    """Write the generated files into `tree`. `final_tree` is where the tree
    will be used from; it differs from `tree` while `convert` builds in a
    staging dir, and absolute paths in the output must point there."""
    sources, keil_startup = _keil_sources(tree, project, diag)
    startup_path = overrides.get("startup", keil_startup)
    startup = _parse_startup(tree, startup_path, diag) if startup_path else None
    defaults = toolchain.from_keil(project, startup, sources, diag)
    defaults.startup = keil_startup
    options = opts.apply(defaults, overrides)
    diag.drop_fields(opts.overridden_options(overrides))
    _check_paths(tree, options, diag)

    shims = scan.include_shims(tree, list(manifest.files), options.includes, diag)
    files = dict(shims)
    generated_sources = []
    if startup is not None:
        startup_c = f"{GENERATED_DIR}/{Path(options.startup).stem}.c"
        files[startup_c] = startup_source(options.startup, startup)
        generated_sources.append(startup_c)
    elif not options.startup:
        diag.note("no startup file is generated (startup is empty); one of the sources must provide the vector table")
    generated_sources.append(SYSCALLS)
    if options.linker_script:
        linker_script = options.linker_script
    else:
        linker_script = f"{GENERATED_DIR}/{(project.device or 'firmware').lower()}.ld"
        files[linker_script] = linker_script_source(project.device, options)
    files[COMPAT_HEADER] = template("keil_compat.h")
    include_dirs = options.includes + ([scan.SHIM_DIR] if shims else [])
    files[PROJECT_MK] = project_mk(project, options, include_dirs, generated_sources, linker_script)
    diag.check()

    _clear_generated(tree)
    _write(tree, files)
    preprocess_args = [*toolchain.cpu_flags(options), f"-std={options.c_std}", *_symbol_flags(options, include_dirs)]
    preprocess_args += ["-include", COMPAT_HEADER]
    result = scan.check_sources(tree, options.sources, preprocess_args, COMPILER, diag)
    syscalls = template("syscalls.c")
    if result.fputc_defined:
        syscalls = "#define KEIL_BRIDGE_WRITE_VIA_FPUTC 1\n" + syscalls
    _write(tree, {SYSCALLS: syscalls})

    commands = [
        compile_command(final_tree, options, include_dirs, source)
        for source in options.sources + generated_sources
        if source.endswith(".c")
    ]
    _write(tree, {"compile_commands.json": json.dumps(commands, indent=2, ensure_ascii=False) + "\n"})
    if not (tree / "Makefile").exists():
        _write(tree, {"Makefile": template("Makefile")})
    _write(tree, {REPORT: report(project, manifest, defaults, options, overrides, diag)})


def _keil_sources(tree: Path, project: KeilProject, diag: Diagnostics) -> tuple[list[str], str]:
    """Sources to build with GCC, and the armasm startup file to translate
    ("" if there is none)."""
    sources = []
    startup = ""
    where = project.uvprojx.name
    for source in project.sources:
        if not source.build:
            continue
        if source.kind == "c":
            sources.append(source.path)
        elif source.kind == "asm":
            text = (tree / source.path).read_text(encoding="utf-8")
            if not armasm_startup.is_armasm(text):
                sources.append(source.path)  # already GNU syntax
            elif startup:
                diag.error(
                    f"second armasm file {source.path}; only one startup file can be translated",
                    where,
                    option="startup",
                )
            else:
                startup = source.path
        elif source.kind in ("object", "library"):
            diag.error(
                f"{source.path}: armcc objects and libraries cannot be linked with GCC; "
                "build the code from source and add it with sources_add",
                where,
                option="sources",
            )
        elif source.kind == "cpp":
            diag.error(f"{source.path}: C++ sources are not supported", where, option="sources")
    if not startup:
        diag.error("the project has no armasm startup file in its build", where, option="startup")
    return sources, startup


def _parse_startup(tree: Path, path: str, diag: Diagnostics) -> armasm_startup.StartupInfo | None:
    if not (tree / path).is_file():
        diag.error(f"startup file {path!r} does not exist", option="startup")
        return None
    return armasm_startup.parse((tree / path).read_text(encoding="utf-8"), path, diag)


def _check_paths(tree: Path, options: BuildOptions, diag: Diagnostics):
    for source in options.sources:
        if not (tree / source).is_file():
            diag.error(f"source {source!r} does not exist in the converted tree", option="sources")
    for directory in options.includes:
        if not (tree / directory).is_dir():
            diag.error(f"include directory {directory!r} does not exist in the converted tree", option="includes")
    if options.linker_script and not (tree / options.linker_script).is_file():
        diag.error(f"linker script {options.linker_script!r} does not exist", option="linker_script")
    for path in options.sources + options.includes:
        if " " in path:
            diag.error(f"{path!r} contains a space, which make cannot handle; rename it", option="sources")
    for name in ("flash_size", "ram_size"):
        if getattr(options, name) <= 0:
            diag.error(f"{name} is not set", option=name)


def _clear_generated(tree: Path):
    directory = tree / GENERATED_DIR
    if not directory.exists():
        return
    for entry in directory.iterdir():
        if entry == tree / MANIFEST_PATH:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _write(tree: Path, files: dict[str, str]):
    for path, content in files.items():
        target = tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- file contents


def startup_source(armasm_path: str, info: armasm_startup.StartupInfo) -> str:
    handlers = list(dict.fromkeys(v for v in info.vectors[2:] if v is not None))
    lines = [
        f"/* Generated by keil-bridge from {armasm_path}; do not edit.",
        " * Stack and heap sizes are set in the linker script. */",
        "#include <stdint.h>",
        "",
        "extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;",
    ]
    if info.calls_system_init:
        lines.append("extern void SystemInit(void);")
    lines += [
        "extern void __libc_init_array(void);",
        "extern int main(void);",
        "",
        "void Reset_Handler(void) __attribute__((weak));",
        "void Default_Handler(void);",
    ]
    lines += [f'void {name}(void) __attribute__((weak, alias("Default_Handler")));' for name in handlers]
    lines += [
        "",
        '__attribute__((section(".isr_vector"), used))',
        "void (*const g_pfnVectors[])(void) = {",
        "    (void (*)(void))&_estack,",
        "    Reset_Handler,",
    ]
    lines += [f"    {name}," if name else "    0, /* reserved */" for name in info.vectors[2:]]
    lines += [
        "};",
        "",
        "void Reset_Handler(void)",
        "{",
        "    uint32_t *src = &_sidata;",
        "    uint32_t *dst = &_sdata;",
        "",
        "    while (dst < &_edata)",
        "        *dst++ = *src++;",
        "    for (dst = &_sbss; dst < &_ebss; dst++)",
        "        *dst = 0;",
    ]
    if info.calls_system_init:
        lines.append("    SystemInit();")
    lines += [
        "    __libc_init_array();",
        "    main();",
        "    for (;;)",
        "        ;",
        "}",
        "",
        "void Default_Handler(void)",
        "{",
        "    for (;;)",
        "        ;",
        "}",
    ]
    return "\n".join(lines) + "\n"


def linker_script_source(device: str, options: BuildOptions) -> str:
    return Template(template("linker.ld")).substitute(
        device=device or "this target",
        stack_size=hex(options.stack_size),
        heap_size=hex(options.heap_size),
        flash_origin=hex(options.flash_origin),
        flash_size=hex(options.flash_size),
        ram_origin=hex(options.ram_origin),
        ram_size=hex(options.ram_size),
    )


def _symbol_flags(options: BuildOptions, include_dirs: list[str]) -> list[str]:
    return (
        [f"-D{d}" for d in options.defines] + [f"-U{u}" for u in options.undefines] + [f"-I{i}" for i in include_dirs]
    )


def compile_flags(options: BuildOptions, include_dirs: list[str]) -> list[str]:
    # Keep in sync with CFLAGS in templates/Makefile.
    return [
        *toolchain.cpu_flags(options),
        f"-std={options.c_std}",
        options.optimize,
        "-g",
        *toolchain.language_flags(options),
        *_symbol_flags(options, include_dirs),
        "-include",
        COMPAT_HEADER,
        "-ffunction-sections",
        "-fdata-sections",
        *options.extra_cflags,
    ]


def compile_command(directory: Path, options: BuildOptions, include_dirs: list[str], source: str) -> dict:
    output = f"build/obj/{source}.o"
    return {
        "directory": str(directory),
        "file": source,
        "output": output,
        "arguments": [COMPILER, *compile_flags(options, include_dirs), "-c", source, "-o", output],
    }


def project_mk(
    project: KeilProject,
    options: BuildOptions,
    include_dirs: list[str],
    generated_sources: list[str],
    linker_script: str,
) -> str:
    def join(flags):
        return " ".join(shlex.quote(f) for f in flags)

    def listing(items):
        return "".join(f" \\\n\t{shlex.quote(item)}" for item in items)

    return "\n".join(
        [
            f"# Generated by keil-bridge from {project.uvprojx_rel} (target {project.target!r}); do not edit.",
            "# Change settings in keil-bridge.toml and run `keil-bridge regen`.",
            "",
            f"TARGET := {project.output_name.replace(' ', '_')}",
            f"DEVICE := {project.device}",
            "",
            f"CSTD ?= {options.c_std}",
            f"OPT ?= {options.optimize}",
            f"CPU_FLAGS := {join(toolchain.cpu_flags(options))}",
            f"LANG_FLAGS := {join(toolchain.language_flags(options))}",
            f"DEFINES := {join([f'-D{d}' for d in options.defines] + [f'-U{u}' for u in options.undefines])}",
            f"INCLUDES := {join([f'-I{i}' for i in include_dirs])}",
            f"EXTRA_CFLAGS ?= {join(options.extra_cflags)}",
            f"EXTRA_ASFLAGS ?= {join(options.extra_asflags)}",
            f"EXTRA_LDFLAGS ?= {join(options.extra_ldflags)}",
            f"LIBC_SPECS := {join([f'--specs={s}' for s in options.libc_specs])}",
            f"LDSCRIPT := {linker_script}",
            "",
            f"FLASH_ORIGIN := {hex(options.flash_origin)}",
            f"PYOCD_TARGET ?= {project.device.lower()}",
            "",
            f"SOURCES :={listing(options.sources)}",
            "",
            f"GENERATED_SOURCES :={listing(generated_sources)}",
            "",
        ]
    )


def report(
    project: KeilProject,
    manifest: Manifest,
    defaults: BuildOptions,
    options: BuildOptions,
    overrides: dict,
    diag: Diagnostics,
) -> str:
    overridden = opts.overridden_options(overrides)
    encodings: dict[str, int] = {}
    for record in manifest.files.values():
        encodings[record.encoding] = encodings.get(record.encoding, 0) + 1
    lines = [
        "# keil-bridge conversion report",
        "",
        f"- Keil project: `{project.uvprojx}`",
        f"- Target: `{project.target}`"
        + (f" (other targets: {', '.join(project.other_targets)})" if project.other_targets else ""),
        f"- Device: `{project.device}`, toolset: {'Arm Compiler 6' if project.toolset == 'ac6' else 'Arm Compiler 5'}",
        "",
        "This tree is the Keil project converted to UTF-8 with LF line endings, plus a GCC build.",
        "Generated files: `Makefile` (yours to edit), `keil-bridge/` (rewritten by `keil-bridge regen`),",
        "`compile_commands.json`, this report. Settings overrides go in `keil-bridge.toml`.",
        "",
        "## Build options",
        "",
        "| Option | Value | From Keil project |",
        "|---|---|---|",
    ]
    for name in (f.name for f in fields(BuildOptions)):
        if name == "sources":
            continue
        value = opts.format_value(getattr(options, name))
        keil = opts.format_value(getattr(defaults, name))
        mark = f"{keil} (overridden)" if name in overridden else "same"
        lines.append(f"| `{name}` | `{value}` | {mark} |")
    lines += ["", f"Sources built: {len(options.sources)} (see `keil-bridge/project.mk`).", ""]

    lines += ["## Files", ""]
    summary = ", ".join(f"{count} {encoding}" for encoding, count in sorted(encodings.items()))
    lines.append(f"{len(manifest.files)} source files converted to UTF-8 + LF ({summary}).")
    if manifest.skipped:
        lines += ["", f"Not copied ({len(manifest.skipped)}; build outputs, IDE state and other non-source files):", ""]
        lines += [f"- `{path}`" for path in manifest.skipped]
    lines += ["", "## Diagnostics", ""]
    shown = [d for d in diag.items if d.level is not Level.NOTE] + [d for d in diag.items if d.level is Level.NOTE]
    lines += [f"- {d}" for d in shown] or ["None."]
    lines += [
        "",
        "## Usage",
        "",
        "```sh",
        "make                                   # build build/*.elf, .hex, .bin",
        "make flash                             # st-flash; FLASHER=pyocd or FLASHER=openocd also work",
        "keil-bridge status                     # files changed here and in the Keil project",
        "keil-bridge backport --dry-run         # preview writing changes back (original encoding and line endings)",
        "keil-bridge backport",
        "keil-bridge regen --set c_std=gnu11    # change a setting and regenerate keil-bridge/",
        "```",
        "",
        "For clangd, start it with `--query-driver=/usr/bin/arm-none-eabi-*` so it finds the GCC system headers.",
        "",
    ]
    return "\n".join(lines)
