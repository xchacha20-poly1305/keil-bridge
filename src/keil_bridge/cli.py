"""Command line interface."""

import argparse
import sys
from pathlib import Path

from . import backport, convert
from .diagnostics import Diagnostics, KeilBridgeError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="keil-bridge", description="Convert a Keil MDK project to a GCC/Makefile tree and back."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("convert", help="create a converted tree from a .uvprojx")
    p.add_argument("uvprojx", type=Path)
    p.add_argument("outdir", type=Path)
    p.add_argument("--target", help="Keil target to convert (default: the first one)")
    p.add_argument("--config", type=Path, help="TOML file with option overrides")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override an option")
    p.add_argument("--no-git", action="store_true", help="do not create a git repository")

    p = commands.add_parser("regen", help="regenerate keil-bridge/ from the Keil project and keil-bridge.toml")
    p.add_argument("tree", type=Path, nargs="?", default=Path("."))
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override an option and save it in keil-bridge.toml",
    )
    p.add_argument(
        "--unset", action="append", default=[], metavar="KEY", help="remove an override from keil-bridge.toml"
    )

    p = commands.add_parser("status", help="show files changed here or in the Keil project")
    p.add_argument("tree", type=Path, nargs="?", default=Path("."))

    p = commands.add_parser("backport", help="write changed files back to the Keil project")
    p.add_argument("tree", type=Path, nargs="?", default=Path("."))
    p.add_argument("--dry-run", action="store_true", help="show the diff instead of writing")
    p.add_argument("--force", action="store_true", help="overwrite files that also changed in the Keil project")

    args = parser.parse_args(argv)
    diag = Diagnostics()
    try:
        if args.command == "convert":
            convert.convert(args.uvprojx, args.outdir, args.target, args.config, args.set, not args.no_git, diag)
            print(f"converted into {args.outdir}; see {args.outdir / 'CONVERSION.md'}")
        elif args.command == "regen":
            convert.regen(args.tree, args.set, args.unset, diag)
            print(f"regenerated {args.tree / 'keil-bridge'}")
        elif args.command == "status":
            lines = backport.status(args.tree.resolve())
            print("\n".join(lines) if lines else "no changes")
        elif args.command == "backport":
            for item in backport.backport(args.tree.resolve(), args.dry_run, args.force, diag):
                print(item if args.dry_run else f"written back: {item}")
    except KeilBridgeError as e:
        diag.print()
        print(f"keil-bridge: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        diag.print()
        print(f"keil-bridge: {e}", file=sys.stderr)
        return 1
    diag.print()
    return 1 if diag.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
