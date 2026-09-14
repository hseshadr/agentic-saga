from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_contract import (
    ReleaseReport,
    release_environment_errors,
    require_complete_report,
)
from scripts.release_runner import measure_release, render_report

_QUALITY_PROOF_ARGUMENTS: Final[int] = 2


def main(arguments: Sequence[str] = ()) -> int:
    proof = _quality_proof_argument(arguments)
    if proof is None and arguments:
        sys.stderr.write("usage: measure_release.py [--quality-proof PATH]\n")
        return 2
    return _run_measurement(proof)


def _run_measurement(quality_proof: Path | None = None) -> int:
    try:
        report = measure_release() if quality_proof is None else measure_release(quality_proof)
        require_complete_report(report)
    except (OSError, RuntimeError, ValueError) as error:
        sys.stderr.write(f"release measurement failed: {error}\n")
        return 2
    sys.stdout.write(render_report(report))
    return _exit_status(report)


def _exit_status(report: ReleaseReport) -> int:
    valid = not release_environment_errors(report.environment)
    passed = all(result.passed for result in report.results)
    return 0 if valid and passed else 1


def _quality_proof_argument(arguments: Sequence[str]) -> Path | None:
    if len(arguments) != _QUALITY_PROOF_ARGUMENTS or arguments[0] != "--quality-proof":
        return None
    return Path(arguments[1])


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
