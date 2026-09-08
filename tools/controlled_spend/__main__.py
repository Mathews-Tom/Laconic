from __future__ import annotations

import argparse
from pathlib import Path

from tools.controlled_spend.manifest import (
    DEFAULT_MANIFEST_PATH,
    manifest_digest,
    validate_manifest_file,
    verify_completion_oracles,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the frozen M20 variance pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("manifest", help="validate the frozen pilot manifest")
    manifest_commands = manifest.add_subparsers(dest="manifest_command", required=True)
    check = manifest_commands.add_parser("check", help="validate manifest and fixture digests")
    check.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    check.add_argument("--verify-oracles", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "manifest" and args.manifest_command == "check":
        manifest = validate_manifest_file(args.manifest)
        if args.verify_oracles:
            verify_completion_oracles(manifest)
        print(
            f"manifest={manifest_digest(args.manifest)} "
            f"tasks={len(manifest.tasks)} runs={len(manifest.run_order)}"
        )
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
