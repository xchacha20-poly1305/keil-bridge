# Commands

```sh
uv run pytest                                              # all tests
uv run pytest tests/test_units.py::test_gbk_crlf_round_trip   # one test
uv run ruff check . && uv run ruff format --check . && uv run mypy   # lint, as CI runs it
uv run keil-bridge convert <x.uvprojx> <outdir> [--no-git] [--set KEY=VALUE]
uv run --project /path/to/keil-bridge keil-bridge regen|status|backport <tree>   # when cwd is a converted tree
```

The package uses only the Python standard library (dev dependencies: pytest, ruff, mypy). CI (`.github/workflows/ci.yml`) runs the lint job and the tests on Python 3.11–3.14. The end-to-end test runs `make` with `arm-none-eabi-gcc` when both are on PATH, and skips the build step otherwise.

# What the tool does

Converts a Keil MDK project (ARM-ADS toolset only) into a separate GCC/Makefile tree with sources in UTF-8 + LF, and writes edits made there back to the Keil project in each file's original encoding and line endings. The design rule throughout is to report anything unrecognised as a warning or error, with `file:line`, rather than guessing. The README documents the user-facing behaviour and the override keys.

# Architecture

Pipeline (`convert.py` → `generate.py`):

1. `uvproj.load` parses the .uvprojx into a `KeilProject`. Every Windows path is resolved on disk, matching case-insensitively if needed, and made relative to the project root. The root is the common ancestor of the uvprojx directory, all sources and all include dirs. Raw `Cads`/`Aads`/`LDads` elements are kept as dicts for later interpretation.
2. `convert.import_tree` copies the source-suffixed files under the root into a **staging dir** next to the output via `textcodec`, and records each file's encoding, EOL and hashes in a `Manifest`. If any error is recorded, the staging dir is deleted and no output appears. Only on success is it renamed to the output dir.
3. `generate.regenerate` is shared by `convert` and `regen` and works on a tree that already contains the sources:
   - It parses the armasm startup file (`armasm_startup`; strict whitelist parser).
   - `toolchain.from_keil` maps Keil settings to `BuildOptions` defaults, then `options.apply` merges the overrides on top.
   - It plans every generated file, calls `diag.check()`, then clears `keil-bridge/` (all of it except `manifest.json`) and writes the new files.
   - Only after writing does it run `scan.check_sources`. That step preprocesses the sources with `arm-none-eabi-gcc -E`, so code under `#if defined(__CC_ARM)` is not flagged, and it needs the compat header and include shims on disk. Its result decides `syscalls.c`: whether `_write` forwards to the project's own `fputc`.
4. `backport.py` compares each file against the manifest hashes: `conv_sha256` tells whether the GCC tree changed it, `orig_sha256` whether the Keil side did. A file changed on both sides is a conflict and is refused unless `--force`. After a successful write-back both hashes are updated.

Key conventions:
- **Diagnostics carry an `option` tag.** A Keil-mapping warning or error is tagged with the `BuildOptions` field it concerns. `Diagnostics.drop_fields` removes it once the user overrides that field, which is how overrides are able to silence errors (for example `startup = ""`, `flash_size`). Untagged errors are always fatal.
- **Override precedence:** uvprojx defaults < `keil-bridge.toml` < `--set`. Every list option `X` also accepts `X_add` and `X_remove` (derived automatically in `options.py`). Adding a field to `BuildOptions` makes it overridable automatically, and validation, TOML output and the report table all follow from it. Enumerated values are listed in `options.CHOICES`.
- **Generated vs user-owned files:**
  - `keil-bridge/*` is regenerated every time.
  - The root `Makefile` is written only when it is missing.
  - `compile_commands.json` and `CONVERSION.md` are rewritten on every regen.
  - The Makefile's `CFLAGS` composition must stay in sync with `generate.compile_flags()` (the same flags feed `compile_commands.json`). Both places carry a comment saying so.
- Source code is never edited by the converter: armcc keywords with an exact GCC equivalent go into the force-included `templates/keil_compat.h`, and include case mismatches become forwarding headers in `keil-bridge/include-shims/`. That way nothing generated ever needs backporting.
- Templates in `src/keil_bridge/templates/` are package data. Only `linker.ld` goes through `string.Template`; the Makefile is copied verbatim because `$$` would break substitution.

