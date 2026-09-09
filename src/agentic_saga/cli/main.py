from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import NoReturn, cast

from agentic_saga import __version__
from agentic_saga.cli.demo import DemoArguments, configure_demo_parser, run_demo
from agentic_saga.demo.reference import ReferenceScenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-saga")
    parser.add_argument("--version", action="store_true")
    configure_demo_parser(parser.add_subparsers(dest="command"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.version:
        _write_version()
        return 0
    if arguments.command == "demo":
        return _run_demo(arguments)
    parser.print_help()
    return 0


def _write_version() -> None:
    sys.stdout.write(f"agentic-saga {__version__}\n")


def _run_demo(arguments: argparse.Namespace) -> int:
    demo = DemoArguments(
        scenario=cast(ReferenceScenario, arguments.scenario),
        open_browser=cast(bool, arguments.open_browser),
        port=cast(int, arguments.port),
    )
    return run_demo(demo)


def entrypoint() -> NoReturn:
    raise SystemExit(main())
