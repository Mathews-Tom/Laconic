from __future__ import annotations

import argparse
from pathlib import Path

from tools.controlled_spend.analysis import check_report, generate_report
from tools.controlled_spend.manifest import (
    validate_manifest_file,
    verify_completion_oracles,
)
from tools.controlled_spend.runner import preflight_environment, run_campaign


def _add_manifest_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate a frozen M20 variance pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("manifest", help="validate a frozen pilot manifest")
    manifest_commands = manifest.add_subparsers(dest="manifest_command", required=True)
    check = manifest_commands.add_parser("check", help="validate manifest and fixture digests")
    _add_manifest_argument(check)
    check.add_argument("--verify-oracles", action="store_true")
    pilot = commands.add_parser("pilot", help="preflight or operate a selected frozen pilot")
    pilot_commands = pilot.add_subparsers(dest="pilot_command", required=True)
    preflight = pilot_commands.add_parser("preflight", help="validate pinned local executables")
    _add_manifest_argument(preflight)
    run = pilot_commands.add_parser("run", help="execute every selected frozen cell once")
    _add_manifest_argument(run)
    run.add_argument("--artifact-root", type=Path, required=True)
    report = pilot_commands.add_parser("report", help="generate public pilot artifacts")
    _add_manifest_argument(report)
    report.add_argument("--artifact-root", type=Path, required=True)
    report.add_argument("--output-json", type=Path, required=True)
    report.add_argument("--output-markdown", type=Path, required=True)
    report_check = pilot_commands.add_parser("check", help="verify public pilot artifacts")
    _add_manifest_argument(report_check)
    report_check.add_argument("--artifact-root", type=Path, required=True)
    report_check.add_argument("--report-json", type=Path, required=True)
    report_check.add_argument("--report-markdown", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "manifest" and args.manifest_command == "check":
        manifest = validate_manifest_file(args.manifest)
        if args.verify_oracles:
            verify_completion_oracles(manifest)
        print(
            f"manifest={manifest.digest} tasks={len(manifest.tasks)} runs={len(manifest.run_order)}"
        )
        return 0
    if args.command == "pilot" and args.pilot_command == "preflight":
        manifest = validate_manifest_file(args.manifest)
        verify_completion_oracles(manifest)
        preflight_environment(manifest)
        print(
            f"manifest={manifest.digest} omp=headroom=ready "
            f"tasks={len(manifest.tasks)} runs={len(manifest.run_order)}"
        )
        return 0
    if args.command == "pilot" and args.pilot_command == "run":
        manifest = validate_manifest_file(args.manifest)
        state = run_campaign(args.artifact_root, manifest=manifest)
        print(
            f"status={state['status']} completed={len(state['completed_runs'])} "
            f"spend_usd={state['gateway_spent_usd']}"
        )
        return 0
    if args.command == "pilot" and args.pilot_command == "report":
        manifest = validate_manifest_file(args.manifest)
        report = generate_report(
            args.artifact_root,
            args.output_json,
            args.output_markdown,
            manifest=manifest,
        )
        print(
            f"verdict={report['verdict']} runs={report['run_count']}/"
            f"{report['expected_run_count']} spend_usd={report['gateway_spend_usd']}"
        )
        return 0
    if args.command == "pilot" and args.pilot_command == "check":
        manifest = validate_manifest_file(args.manifest)
        report = check_report(
            args.artifact_root,
            args.report_json,
            args.report_markdown,
            manifest=manifest,
        )
        print(
            f"verdict={report['verdict']} runs={report['run_count']}/"
            f"{report['expected_run_count']} report=verified"
        )
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
