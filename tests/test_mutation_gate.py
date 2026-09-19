from scripts.run_mutation_gate import _PATTERNS, _MutationStats, _passed, _summary


def _stats(**changes: int) -> _MutationStats:
    values = {
        "killed": 120,
        "survived": 0,
        "total": 120,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }
    return _MutationStats(**(values | changes))


def test_selected_mutants_must_all_be_killed() -> None:
    assert _passed(_stats())
    assert not _passed(_stats(survived=1))
    assert not _passed(_stats(skipped=1))


def test_mutation_scope_targets_temporal_safety_code() -> None:
    selected = "\n".join(_PATTERNS)

    assert "agentic_saga.temporal" in selected
    assert "agentic_saga.contracts.redaction" in selected
    assert "agentic_saga.kernel" not in selected
    assert "agentic_saga.storage" not in selected


def test_summary_is_actionable() -> None:
    assert "120/120" in _summary(_stats())
