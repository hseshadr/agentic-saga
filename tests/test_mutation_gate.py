from scripts.run_kernel_mutation_gate import _PATTERNS, _MutationStats, _passed, _summary


def _stats(*, killed: int, skipped: int = 0) -> _MutationStats:
    return _MutationStats(
        killed=killed,
        survived=0,
        total=11_927,
        no_tests=0,
        skipped=skipped,
        suspicious=0,
        timeout=0,
        check_was_interrupted_by_user=0,
        segfault=0,
    )


def test_skipped_selected_mutant_is_reported_and_blocks_release() -> None:
    stats = _stats(killed=447, skipped=1)

    assert stats.executed == 448
    assert not _passed(stats)
    assert "skipped=1" in _summary(stats)
    assert "generated_total=11927" in _summary(stats)


def test_clean_bounded_selection_passes_and_reports_exact_score() -> None:
    stats = _stats(killed=447)

    assert _passed(stats)
    assert "selected safety-critical mutation score: 100.00% (447/447)" in _summary(stats)


def test_identity_mutation_selection_tracks_authoritative_framed_digest() -> None:
    # Given
    selected = frozenset(_PATTERNS)

    # When / Then
    assert "agentic_saga.kernel.identity.x_framed_sha256__mutmut_*" in selected
    assert "agentic_saga.kernel.identity.x__canonical_bytes__mutmut_*" not in selected
