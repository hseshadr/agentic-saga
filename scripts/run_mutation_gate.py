"""Run the bounded safety-critical Temporal mutation gate."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_REDACTION = "agentic_saga.contracts.redaction"
_OUTCOMES = "agentic_saga.contracts.outcomes"
_JOURNAL = "agentic_saga.temporal.journal"
_WORKFLOW = "agentic_saga.temporal.workflow"
_MINIMUM_SCORE = 0.85
_MINIMUM_MUTANTS = 100
_MAXIMUM_MUTANTS = 400
_PATTERNS = (
    f"{_REDACTION}.x__secret_boundary__mutmut_*",
    f"{_REDACTION}.x__is_secret_reference_key__mutmut_*",
    f"{_OUTCOMES}.x_normalize_effect_outcome__mutmut_*",
    f"{_OUTCOMES}.x_normalize_reconciliation_outcome__mutmut_*",
    f"{_JOURNAL}.xǁCompensationJournalǁrecord_forward_success__mutmut_*",
    f"{_JOURNAL}.xǁCompensationJournalǁnext_request__mutmut_*",
    f"{_JOURNAL}.xǁCompensationJournalǁrecord_compensation__mutmut_*",
    f"{_JOURNAL}.xǁCompensationJournalǁresolve_unresolved__mutmut_*",
    f"{_WORKFLOW}.x_creates_obligation__mutmut_*",
    f"{_WORKFLOW}.x_reconciled_tool_result__mutmut_*",
)


class _MutationStats(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    killed: int = Field(strict=True, ge=0)
    survived: int = Field(strict=True, ge=0)
    total: int = Field(strict=True, ge=0)
    no_tests: int = Field(strict=True, ge=0)
    skipped: int = Field(strict=True, ge=0)
    suspicious: int = Field(strict=True, ge=0)
    timeout: int = Field(strict=True, ge=0)
    check_was_interrupted_by_user: int = Field(strict=True, ge=0)
    segfault: int = Field(strict=True, ge=0)

    @property
    def executed(self) -> int:
        return self.killed + sum(self.blockers)

    @property
    def blockers(self) -> tuple[int, ...]:
        return (
            self.survived,
            self.no_tests,
            self.skipped,
            self.suspicious,
            self.timeout,
            self.check_was_interrupted_by_user,
            self.segfault,
        )

    @property
    def score(self) -> float:
        return self.killed / self.executed if self.executed else 0.0


def _mutmut() -> str:
    executable = shutil.which("mutmut")
    if executable is None:
        raise RuntimeError("mutmut is unavailable; run uv sync --group dev")
    return executable


def _run(*arguments: str) -> None:
    subprocess.run((_mutmut(), *arguments), check=True)  # noqa: S603


def _load_stats() -> _MutationStats:
    path = Path("mutants/mutmut-cicd-stats.json")
    return _MutationStats.model_validate_json(path.read_bytes(), strict=True)


def _passed(stats: _MutationStats) -> bool:
    count_ok = _MINIMUM_MUTANTS <= stats.executed <= _MAXIMUM_MUTANTS
    return count_ok and not any(stats.blockers) and stats.score >= _MINIMUM_SCORE


def _summary(stats: _MutationStats) -> str:
    score = f"selected safety-critical mutation score: {stats.score:.2%}"
    counts = f"({stats.killed}/{stats.executed}); skipped={stats.skipped}"
    generated = f"generated_total={stats.total}"
    return f"{score} {counts}; {generated}"


def main() -> int:
    shutil.rmtree("mutants", ignore_errors=True)
    _run("run", "--max-children", "4", *_PATTERNS)
    _run("export-cicd-stats")
    stats = _load_stats()
    print(_summary(stats))
    return 0 if _passed(stats) else 1


if __name__ == "__main__":
    raise SystemExit(main())
